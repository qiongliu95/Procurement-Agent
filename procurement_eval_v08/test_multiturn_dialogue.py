"""多轮对话 M01-M16；离线脚本模型，不冒充真实模型端到端结果。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_runtime import AgentRuntime, ModelToolCall, ModelTurn, TraceWriter
from agent_tools import ProcurementApiClient
from backend import App, Store


def tool(name, arguments, index=1):
    return ModelToolCall(f"multi-call-{index}", name, arguments)


def proposal(intent, relation, target, **values):
    return tool("propose_task_state", {
        "intent": intent, "dialogue_relation": relation, "target_object": target, **values,
    })


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.received = []

    def complete(self, messages, tools):
        self.received.append(json.loads(json.dumps(messages, ensure_ascii=False)))
        if not self.turns:
            raise AssertionError("ScriptedModel exhausted")
        return self.turns.pop(0)


class FakeKnowledgeIndex:
    def __init__(self):
        self.calls = []

    def search(self, query, top_k=4):
        self.calls.append((query, top_k))
        return {
            "query": query, "model_id": "fake-bge", "index_fingerprint": "fake-index",
            "evidence_sufficient": True, "timings_ms": {"total": 5},
            "hits": [{
                "chunk_id": "KB-003#002", "doc_id": "KB-003", "title": "审批规则",
                "heading": "审批阈值", "class": "A", "source": "正式PRD",
                "source_url": None, "text": "金额达到一万元由 M02 审批。", "score": 0.8,
            }],
        }


class MultiturnDialogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(("127.0.0.1", 0), Store(Path(self.tmp.name) / "multi.sqlite"))
        self.base = f"http://127.0.0.1:{self.app.server_port}"
        self.thread = threading.Thread(target=self.app.serve_forever, daemon=True)
        self.thread.start()
        self.api = ProcurementApiClient(self.base, "demo-agent", 2)
        self.trace = TraceWriter(Path(self.tmp.name) / "traces")

    def tearDown(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def runtime(self, turns, **kwargs):
        model = ScriptedModel(turns)
        return AgentRuntime(model, self.api, self.trace, model_name="offline-script", **kwargs), model

    def call(self, method, path, data=None, token="demo-u01"):
        request = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def fixture(self, request_id, *, item="显示器", quantity=2, price="800.00", po=False):
        status, _ = self.call("POST", "/requests", {
            "request_id": request_id, "item": item, "quantity": quantity,
            "unit_price": price, "supplier_name": "启明办公", "purpose": "办公",
        })
        self.assertEqual(status, 201)
        self.call("POST", f"/requests/{request_id}/submit", {})
        if po:
            self.call("POST", f"/requests/{request_id}/approve", {}, token="demo-m01")
            self.call("POST", f"/requests/{request_id}/po", {"idempotency_key": "K-" + request_id})

    def events(self, run_id):
        result = []
        for path in (Path(self.tmp.name) / "traces").glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("run_id") == run_id:
                    result.append(event)
        return result

    def history_turns(self, item="显示器", start=1):
        return [
            ModelTurn(None, [tool("search_purchase_history", {"item": item}, start)]),
            ModelTurn(None, [proposal("consult", "new_task", "purchase_history", index=start + 1)]),
        ]

    def test_m01_history_follow_up_uses_trusted_record(self):
        self.fixture("M01-H", quantity=3, price="900.00", po=True)
        runtime, model = self.runtime([
            *self.history_turns(),
            ModelTurn(None, [proposal("consult", "follow_up", "purchase_history", reference_index=1)]),
        ])
        state = runtime.new_session("M01")
        runtime.handle_message(state, "查询显示器历史成交")
        follow = runtime.handle_message(state, "那次的供应商、数量和申请编号是什么？")
        self.assertIn("启明办公", follow["message"])
        self.assertIn("3 件", follow["message"])
        self.assertIn("M01-H", follow["message"])
        self.assertIn('"object_type": "purchase_history"', model.received[-1][0]["content"])

    def test_m02_multiple_history_records_require_disambiguation(self):
        self.fixture("M02-A", po=True)
        self.fixture("M02-B", quantity=4, po=True)
        runtime, _ = self.runtime([
            *self.history_turns(),
            ModelTurn(None, [proposal(
                "ambiguous", "follow_up", "purchase_history",
                question="有两条历史成交记录，请说明第一条还是第二条。",
            )]),
        ])
        state = runtime.new_session("M02")
        runtime.handle_message(state, "查询显示器历史成交")
        response = runtime.handle_message(state, "那次是哪家供应商？")
        self.assertEqual(response["route_reason_code"], "AMBIGUOUS")
        self.assertIn("第一条还是第二条", response["message"])

    def test_m03_previous_history_query_can_be_reused_for_reread(self):
        self.fixture("M03-H", po=True)
        runtime, _ = self.runtime([
            *self.history_turns(),
            ModelTurn(None, [tool("search_purchase_history", {"item": "", "use_previous_query": True}, 3)]),
            ModelTurn(None, [proposal("consult", "follow_up", "purchase_history", index=4)]),
        ])
        state = runtime.new_session("M03")
        runtime.handle_message(state, "查询显示器历史成交")
        response = runtime.handle_message(state, "再查一次，数量呢？")
        self.assertIn("M03-H", response["message"])
        self.assertTrue(any(e["event_type"] == "TRUSTED_QUERY_REUSED" for e in self.events(response["trace_id"])))

    def test_m04_product_follow_up_uses_product_context(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("search_products", {"query": "显示器", "status": "active"})]),
            ModelTurn(None, [proposal("consult", "new_task", "product", index=2)]),
            ModelTurn(None, [proposal("consult", "follow_up", "product", reference_index=1, index=3)]),
        ])
        state = runtime.new_session("M04")
        runtime.handle_message(state, "查商品目录里的显示器")
        response = runtime.handle_message(state, "第一个是什么型号和参考价？")
        self.assertEqual(response["result"]["result_type"], "products")
        self.assertIn("P-IT-001", response["message"])
        self.assertEqual(runtime.public_session(state)["messages"][-1]["result"]["result_type"], "products")
        self.assertNotIn("request_id", state.slots)

    def test_m05_knowledge_follow_up_keeps_citations(self):
        index = FakeKnowledgeIndex()
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("search_knowledge", {"query": "审批规则"})]),
            ModelTurn(None, [proposal("consult", "new_task", "knowledge", index=2)]),
            ModelTurn("达到一万元由 M02 审批。", []),
            ModelTurn(None, [proposal("consult", "follow_up", "knowledge", reference_index=1, index=4)]),
            ModelTurn("该规则来自已确认的审批规则文档。", []),
        ], knowledge_index=index)
        state = runtime.new_session("M05")
        runtime.handle_message(state, "审批规则是什么？")
        response = runtime.handle_message(state, "它来自哪份制度？")
        self.assertEqual(response["result"]["result_type"], "knowledge")
        self.assertEqual(response["result"]["citations"][0]["doc_id"], "KB-003")

    def test_m06_switch_history_to_unrelated_knowledge_is_isolated(self):
        self.fixture("M06-H", po=True)
        index = FakeKnowledgeIndex()
        runtime, _ = self.runtime([
            *self.history_turns(),
            ModelTurn(None, [tool("search_knowledge", {"query": "审批规则"}, 3)]),
            ModelTurn(None, [proposal("consult", "switch_task", "knowledge", index=4)]),
            ModelTurn("审批规则说明。", []),
        ], knowledge_index=index)
        state = runtime.new_session("M06")
        runtime.handle_message(state, "查询显示器历史成交")
        response = runtime.handle_message(state, "换个话题，审批制度是什么？")
        self.assertEqual(response["result"]["history"], [])
        self.assertTrue(response["result"]["knowledge"])

    def test_m07_history_does_not_fill_new_create_fields(self):
        self.fixture("M07-H", po=True)
        runtime, _ = self.runtime([
            *self.history_turns(),
            ModelTurn(None, [proposal("create", "switch_task", "current_task", item="键盘", index=3)]),
        ])
        state = runtime.new_session("M07")
        runtime.handle_message(state, "查询显示器历史成交")
        response = runtime.handle_message(state, "现在创建键盘采购申请")
        self.assertEqual(response["route_reason_code"], "MISSING_REQUIRED_SLOTS")
        self.assertEqual(state.slots, {"item": "键盘"})

    def test_m08_create_fields_accumulate_across_turns(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [proposal("create", "new_task", "current_task", item="键盘")]),
            ModelTurn(None, [proposal("create", "supplement", "current_task", quantity=2, unit_price="100.00", purpose="办公", index=2)]),
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"}, 3)]),
            ModelTurn(None, [proposal("create", "supplement", "current_task", supplier_name="启明办公", index=4)]),
        ])
        state = runtime.new_session("M08")
        runtime.handle_message(state, "创建键盘采购")
        runtime.handle_message(state, "数量2，单价100元，用于办公")
        response = runtime.handle_message(state, "供应商启明办公")
        self.assertEqual(response["route_action"], "CONFIRM")
        self.assertEqual(state.slots["quantity"], 2)

    def test_m09_correction_invalidates_old_confirmation(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"})]),
            ModelTurn(None, [proposal("create", "new_task", "current_task", item="键盘", quantity=1, unit_price="100.00", supplier_name="启明办公", purpose="办公", index=2)]),
            ModelTurn(None, [proposal("create", "correction", "current_task", quantity=2, index=3)]),
        ])
        state = runtime.new_session("M09")
        first = runtime.handle_message(state, "采购1个键盘，单价100元，供应商启明办公，用于办公")
        second = runtime.handle_message(state, "数量改成2")
        self.assertNotEqual(first["confirmation"]["action_id"], second["confirmation"]["action_id"])
        self.assertEqual(runtime.confirm(state, first["confirmation"]["action_id"], "confirm")["route_reason_code"], "CONFIRMATION_NOT_FOUND")

    def test_m10_switch_and_resume_restores_original_task_only(self):
        self.fixture("M10-Q")
        runtime, _ = self.runtime([
            ModelTurn(None, [proposal("create", "new_task", "current_task", item="键盘")]),
            ModelTurn(None, [tool("get_request", {"request_id": "M10-Q"}, 2)]),
            ModelTurn(None, [proposal("query", "switch_task", "request", request_id="M10-Q", index=3)]),
        ])
        state = runtime.new_session("M10")
        runtime.handle_message(state, "创建键盘采购")
        runtime.handle_message(state, "先查 M10-Q")
        runtime.handle_message(state, "继续")
        self.assertEqual((state.active_task_type, state.slots), ("create", {"item": "键盘"}))

    def test_m11_plain_confirmation_does_not_authorize_without_pending_action(self):
        self.fixture("M11-H", po=True)
        runtime, _ = self.runtime(self.history_turns())
        state = runtime.new_session("M11")
        runtime.handle_message(state, "查询显示器历史成交")
        response = runtime.handle_message(state, "确认")
        self.assertEqual(response["route_reason_code"], "NO_PENDING_CONFIRMATION")
        self.assertIsNone(response["confirmation"])

    def test_m12_old_request_does_not_override_new_ambiguity(self):
        for rid in ("M12-OLD", "M12-A", "M12-B"):
            self.fixture(rid)
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("get_request", {"request_id": "M12-OLD"})]),
            ModelTurn(None, [proposal("query", "new_task", "request", request_id="M12-OLD", index=2)]),
            ModelTurn(None, [tool("list_requests", {}, 3)]),
            ModelTurn(None, [proposal("update", "switch_task", "request", quantity=5, index=4)]),
        ])
        state = runtime.new_session("M12")
        runtime.handle_message(state, "查询 M12-OLD")
        response = runtime.handle_message(state, "修改刚才列表中的一个，数量改成5")
        self.assertEqual(response["route_reason_code"], "REQUEST_OBJECT_NOT_UNIQUE")
        self.assertIsNone(response["confirmation"])

    def test_m13_failed_query_keeps_suspended_task_consistent(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [proposal("create", "new_task", "current_task", item="键盘")]),
            ModelTurn(None, [tool("get_request", {"request_id": "M13-NOT-FOUND"}, 2)]),
            ModelTurn(None, [proposal("query", "switch_task", "request", request_id="M13-NOT-FOUND", index=3)]),
        ])
        state = runtime.new_session("M13")
        runtime.handle_message(state, "创建键盘采购")
        failed = runtime.handle_message(state, "先查 M13-NOT-FOUND")
        self.assertEqual(failed["route_reason_code"], "INTERRUPTED_TASK_WAITING_RESUME")
        self.assertEqual(state.suspended_task.slots, {"item": "键盘"})
        runtime.handle_message(state, "继续")
        self.assertEqual(state.slots, {"item": "键盘"})

    def test_m14_completed_write_replay_returns_same_result(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"})]),
            ModelTurn(None, [proposal("create", "new_task", "current_task", item="键盘", quantity=1, unit_price="100.00", supplier_name="启明办公", purpose="办公", index=2)]),
        ])
        state = runtime.new_session("M14")
        pending = runtime.handle_message(state, "采购1个键盘，单价100元，供应商启明办公，用于办公")
        action = pending["confirmation"]["action_id"]
        first = runtime.confirm(state, action, "confirm")
        second = runtime.confirm(state, action, "confirm")
        self.assertEqual(first, second)
        self.assertEqual(self.call("GET", "/requests")[1]["requests"].__len__(), 1)

    def test_m15_explicit_current_request_overrides_history_reference(self):
        self.fixture("M15-OLD")
        self.fixture("M15-NEW")
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("get_request", {"request_id": "M15-OLD"})]),
            ModelTurn(None, [proposal("query", "new_task", "request", request_id="M15-OLD", index=2)]),
            ModelTurn(None, [tool("get_request", {"request_id": "M15-NEW"}, 3)]),
            ModelTurn(None, [proposal("update", "switch_task", "request", request_id="M15-NEW", quantity=5, index=4)]),
        ])
        state = runtime.new_session("M15")
        runtime.handle_message(state, "查询 M15-OLD")
        response = runtime.handle_message(state, "把 M15-NEW 的数量改成5")
        self.assertEqual(state.target_request_id, "M15-NEW")
        self.assertIn("M15-NEW", response["message"])

    def test_m16_follow_up_without_trusted_context_clarifies(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [proposal("consult", "follow_up", "purchase_history")]),
        ])
        response = runtime.handle_message(runtime.new_session("M16"), "那次的供应商是谁？")
        self.assertEqual(response["route_reason_code"], "TRUSTED_READ_CONTEXT_UNAVAILABLE")
        self.assertIsNone(response["confirmation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
