"""TASK-21：真实临时 Backend + 脚本模型，保存逐轮状态，不使用演示库。"""
import json
import os
from pathlib import Path
import unittest

from agent_runtime import AgentRuntime, ModelTurn
from evaluation.fixture import snapshot_database
import test_context_management as helpers
from test_multiturn_dialogue import FakeKnowledgeIndex


def proposed(intent, relation="supplement", target="current_task", **values):
    return ModelTurn(None, [helpers.tool("propose_task_state", {
        "intent": intent, "dialogue_relation": relation, "target_object": target, **values,
    })])


class ClarificationIncrementTests(unittest.TestCase):
    real_model = False
    call = helpers.ContextManagementTests.call
    fixture = helpers.ContextManagementTests.fixture
    trace_events = helpers.ContextManagementTests.trace_events

    def setUp(self):
        helpers.ContextManagementTests.setUp(self)
        self.steps = []
        self.models = []

    def tearDown(self):
        try:
            if os.getenv("TASK21_EVIDENCE_DIR"):
                path = Path(os.environ["TASK21_EVIDENCE_DIR"]) / self._testMethodName
                path.mkdir(parents=True, exist_ok=False)
                events = [json.loads(line) for file in (Path(self.tmp.name) / "traces").glob("*.jsonl")
                          for line in file.read_text(encoding="utf-8").splitlines()]
                path.joinpath("evidence.json").write_text(json.dumps({
                    "real_model": self.real_model, "test": self.id(), "steps": self.steps,
                    "model_inputs": [m.received for m in self.models], "trace": events,
                    "final_business_state": snapshot_database(Path(self.tmp.name) / "context.sqlite"),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            helpers.ContextManagementTests.tearDown(self)

    def runtime(self, turns, **kwargs):
        model = helpers.CapturingModel(turns)
        self.models.append(model)
        return AgentRuntime(model, self.api, self.trace, model_name="offline-task21", **kwargs), model

    def keyboard_catalog(self):
        # 澄清用例需要真实同名目录夹具，不依赖“键盘”被错误自动认成键鼠套装。
        self.app.store.create_product({'code':'FIX-KEYBOARD','name':'键盘','category':'测试',
            'brand_model':'隔离夹具','specification':'合成数据','unit':'个','reference_price':'100',
            'status':'active','supplier_name':'启明办公'},('M02','senior_approver'))

    def send(self, runtime, state, text):
        before = snapshot_database(Path(self.tmp.name) / "context.sqlite")
        response = runtime.handle_message(state, text)
        after = snapshot_database(Path(self.tmp.name) / "context.sqlite")
        self.steps.append({"input": text, "response": response, "before": before, "after": after,
                           "clarification": json.loads(json.dumps(getattr(state, "clarification", None))),
                           "session": runtime.public_session(state), "slots": dict(state.slots)})
        self.assertEqual({k: v for k, v in before["tables"].items() if k != "events"},
                         {k: v for k, v in after["tables"].items() if k != "events"})
        events = self.trace_events(response["trace_id"])
        self.assertEqual(sum(e["event_type"] == "FINAL_RESPONSE" for e in events), 1)
        self.assertEqual(sum(e["event_type"] == "ROUTE_DECIDED" for e in events), 1)
        self.assertFalse(any(e.get("tool_name") in {"create_request", "submit_request", "update_request"}
                             for e in events))
        return response

    def test_c01_unclear_direction_then_default_history_query(self):
        runtime, model = self.runtime([
            ModelTurn("请说明要办理还是查询什么？", []),
            proposed("consult", "new_task", "purchase_history", question="要限定物品吗，也可以不限定。"),
            ModelTurn(None, [helpers.tool("search_purchase_history", {})]),
            proposed("consult", "supplement", "purchase_history"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "ewre")
        self.send(runtime, state, "历史成交价")
        response = self.send(runtime, state, "不限定物品")
        for word in ("未限定物品", "当前", "20"):
            self.assertIn(word, response["message"])
        self.assertNotIn("全部历史", response["message"])
        self.assertIn("要限定物品", model.received[2][0]["content"])
        self.assertIsNone(state.clarification)

    def test_c02_direct_text_ask_and_short_answer_keep_question(self):
        runtime, model = self.runtime([
            ModelTurn("您想查哪类商品目录？", []),
            ModelTurn(None, [helpers.tool("search_products", {"query": "显示器"})]),
            proposed("consult", "follow_up", "product"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "我想查商品")
        self.assertEqual(state.clarification["question"], "您想查哪类商品目录？")
        self.send(runtime, state, "显示器")
        self.assertIn("您想查哪类商品目录", model.received[1][0]["content"])
        self.assertIsNone(state.clarification)

    def test_c03_consultation_conditions_preserve_topic_for_retrieval(self):
        index = FakeKnowledgeIndex()
        runtime, _ = self.runtime([
            proposed("consult", "new_task", "knowledge", question="请补充申请金额。"),
            proposed("consult", "supplement", "knowledge", question="请补充采购对象。"),
            ModelTurn(None, [helpers.tool("search_knowledge", {"query": "显示器", "use_previous_query": True})]),
            proposed("consult", "follow_up", "knowledge"),
            ModelTurn("达到一万元由 M02 审批。", []),
        ], knowledge_index=index)
        state = runtime.new_session(self.id())
        self.send(runtime, state, "采购审批流程有什么要求？")
        self.send(runtime, state, "金额20000元")
        result = self.send(runtime, state, "采购对象是显示器")
        self.assertEqual(result["route_reason_code"], "READ_VERIFIED")
        query = index.calls[0][0]
        for phrase in ("采购审批流程", "20000", "显示器"):
            self.assertIn(phrase, query)
        self.assertTrue(result["result"]["citations"])
        self.assertIsNone(state.clarification)

    def test_c04_partial_fields_accumulate_and_only_missing_fields_are_asked(self):
        self.keyboard_catalog()
        runtime, _ = self.runtime([
            proposed("create", "new_task", item="键盘"),
            proposed("create", quantity=2), proposed("create", unit_price="100"),
            proposed("create", purpose="办公"),
            ModelTurn(None, [helpers.tool("match_supplier", {"name": "启明办公"})]),
            proposed("create", supplier_name="启明办公"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "创建键盘采购申请")
        self.send(runtime, state, "数量2个")
        response = self.send(runtime, state, "单价100元")
        self.assertNotIn("单价", response["message"])
        self.assertEqual(state.clarification["known_slots"]["quantity"], 2)
        self.send(runtime, state, "用于办公")
        final = self.send(runtime, state, "供应商启明办公")
        self.assertEqual(final["route_action"], "CONFIRM")
        self.assertIsNone(state.clarification)
        self.assertEqual(state.pending_action.args["quantity"], 2)

    def test_c05_unmatched_answer_does_not_invent_fields(self):
        runtime, _ = self.runtime([
            proposed("create", "new_task", item="键盘"),
            proposed("ambiguous", "unclear", "unknown", question="请提供数量。"),
            proposed("create", quantity=2),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "创建键盘采购申请")
        result = self.send(runtime, state, "asdfgh")
        self.assertEqual(result["route_action"], "ASK")
        self.assertEqual(state.slots, {"item": "键盘"})
        self.send(runtime, state, "数量2个")
        self.assertEqual(state.slots, {"item": "键盘", "quantity": 2})
        self.assertIsNone(state.pending_action)

    def test_c06_same_type_new_task_clears_old_fields_even_if_model_carries_them(self):
        self.keyboard_catalog()
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("match_supplier", {"name": "启明办公"})]),
            proposed("create", "new_task", item="键盘", quantity=1, unit_price="100", supplier_name="启明办公", purpose="办公"),
            proposed("create", "switch_task", item="显示器", quantity=2, unit_price="100", supplier_name="启明办公", purpose="办公"),
        ])
        state = runtime.new_session(self.id())
        first = self.send(runtime, state, "创建键盘申请，数量1个，单价100，供应商启明办公，用于办公")
        self.assertEqual(first["route_action"], "CONFIRM")
        result = self.send(runtime, state, "换一笔新的申请：创建显示器采购，数量2台")
        self.assertEqual(state.slots, {"item": "显示器", "quantity": 2})
        self.assertEqual(result["route_action"], "ASK")
        self.assertEqual(state.candidate_request_ids, [])
        self.assertEqual(runtime.confirm(state, first["confirmation"]["action_id"], "confirm")["route_reason_code"],
                         "CONFIRMATION_NOT_FOUND")

    def test_c07_value_then_object_keeps_value_and_reads_latest_version(self):
        self.fixture("A001"); self.fixture("A002")
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("list_requests", {})]),
            proposed("update", "new_task", "request", quantity=3),
            proposed("update", "supplement", "request", request_id="A001"),
        ])
        state = runtime.new_session(self.id())
        result = self.send(runtime, state, "把申请数量改成3")
        self.assertEqual(result["route_action"], "ASK")
        self.assertEqual(state.slots["quantity"], 3)
        latest = self.call("GET", "/requests/A001")[1]
        result = self.send(runtime, state, "A001")
        self.assertEqual(result["route_action"], "CONFIRM")
        self.assertEqual(state.pending_action.args, {"quantity": 3})
        self.assertEqual(state.pending_action.expected_version, latest["version"])

    def test_c08_abandon_collection_preserves_database_and_other_objects(self):
        self.fixture("KEEP-01")
        runtime, _ = self.runtime([proposed("create", "new_task", item="键盘")])
        state = runtime.new_session(self.id())
        state.known_requests["KEEP-01"] = self.call("GET", "/requests/KEEP-01")[1]
        self.send(runtime, state, "创建键盘采购申请")
        result = self.send(runtime, state, "不办了")
        self.assertEqual(result["route_action"], "STOP")
        self.assertIsNone(state.active_task_type)
        self.assertEqual(state.slots, {})
        self.assertIsNone(state.clarification)
        self.assertIn("KEEP-01", state.known_requests)

    def test_c09_abandon_confirmation_consumes_old_action(self):
        self.keyboard_catalog()
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("match_supplier", {"name": "启明办公"})]),
            proposed("create", "new_task", item="键盘", quantity=2, unit_price="100", supplier_name="启明办公", purpose="办公"),
        ])
        state = runtime.new_session(self.id())
        first = self.send(runtime, state, "创建键盘采购，数量2，单价100，供应商启明办公，用于办公")
        result = self.send(runtime, state, "取消当前任务")
        self.assertEqual(result["route_action"], "STOP")
        self.assertIsNone(state.pending_action)
        replay = runtime.confirm(state, first["confirmation"]["action_id"], "confirm")
        self.assertNotEqual(replay["route_reason_code"], "WRITE_VERIFIED")
        self.assertEqual(self.call("GET", "/requests")[1]["requests"], [])

    def test_c10_suspended_task_can_resume_or_be_abandoned(self):
        self.fixture("A001")
        runtime, _ = self.runtime([
            proposed("create", "new_task", item="键盘"),
            ModelTurn(None, [helpers.tool("get_request", {"request_id": "A001"})]),
            proposed("query", "switch_task", "request", request_id="A001"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "创建键盘采购")
        self.send(runtime, state, "先查询 A001")
        self.assertIsNotNone(state.suspended_task)
        self.send(runtime, state, "继续")
        self.assertEqual(state.slots["item"], "键盘")
        self.send(runtime, state, "取消当前任务")
        self.assertIsNone(state.suspended_task)
        self.assertIsNone(state.active_task_type)

    def test_c11_selected_new_object_does_not_reuse_other_objects_changes(self):
        self.fixture("A001"); self.fixture("A002")
        runtime, _ = self.runtime([
            proposed("update", "new_task", "request", request_id="A001", quantity=3),
            proposed("update", "switch_task", "request", request_id="A002"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "把 A001 数量改成3")
        result = self.send(runtime, state, "改为修改 A002")
        self.assertEqual(result["route_action"], "ASK")
        self.assertEqual(result["route_reason_code"], "NO_UPDATE_FIELDS")
        self.assertNotIn("quantity", state.slots)

    def test_c12_clarification_completion_still_requires_write_confirmation(self):
        runtime, _ = self.runtime([
            proposed("create", "new_task", item="键盘", quantity=2, unit_price="100", purpose="办公"),
            ModelTurn(None, [helpers.tool("match_supplier", {"name": "启明办公"})]),
            proposed("create", supplier_name="启明办公"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "创建2个键盘申请，单价100，用于办公")
        pending = self.send(runtime, state, "供应商启明办公")
        self.assertEqual(pending["route_action"], "CONFIRM")
        self.assertEqual(self.call("GET", "/requests")[1]["requests"], [])
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(result["result"]["state"], "SUBMITTED")
        self.assertEqual(len(self.call("GET", "/requests")[1]["requests"]), 1)

    def test_c13_repeated_clarification_is_bounded_and_keeps_origin(self):
        runtime, model = self.runtime([ModelTurn("请明确咨询范围。", []) for _ in range(12)], model_context_messages=4)
        state = runtime.new_session(self.id())
        self.send(runtime, state, "想了解采购审批")
        for number in range(10):
            self.send(runtime, state, f"不明确的回答{number}")
        self.assertLessEqual(len(state.task_messages), 4)
        self.assertEqual(state.clarification["topic"], "想了解采购审批")
        self.assertIn("想了解采购审批", model.received[-1][0]["content"])

    def test_c14_consultation_insufficient_evidence_remains_safe(self):
        class InsufficientIndex(FakeKnowledgeIndex):
            def search(self, query, top_k=4):
                self.calls.append((query, top_k))
                return {"query": query, "evidence_sufficient": False, "hits": []}
        runtime, _ = self.runtime([
            proposed("consult", "new_task", "knowledge", question="请说明金额。"),
            ModelTurn(None, [helpers.tool("search_knowledge", {"query": "20000", "use_previous_query": True})]),
            proposed("consult", "follow_up", "knowledge"),
        ], knowledge_index=InsufficientIndex())
        state = runtime.new_session(self.id())
        self.send(runtime, state, "报销是否有特定审批规则？")
        result = self.send(runtime, state, "20000元")
        self.assertIn("没有足够证据", result["message"])
        self.assertIsNone(result["confirmation"])

    def test_c15_plain_supplement_cannot_become_new_task_by_model_label_only(self):
        runtime, _ = self.runtime([
            proposed("create", "new_task", item="键盘", quantity=2, purpose="办公"),
            proposed("create", "new_task", unit_price="100"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "创建2个键盘采购，用于办公")
        self.send(runtime, state, "单价100元")
        self.assertEqual(state.slots, {"item": "键盘", "quantity": 2, "purpose": "办公", "unit_price": "100"})

    def test_c16_direct_consultation_ask_can_supply_semantic_retrieval_topic(self):
        index = FakeKnowledgeIndex()
        runtime, _ = self.runtime([
            ModelTurn("请说明申请金额。", []),
            ModelTurn(None, [helpers.tool("search_knowledge", {"query": "20000", "use_previous_query": True})]),
            proposed("consult", "follow_up", "knowledge"), ModelTurn("由 M02 审批。", []),
        ], knowledge_index=index)
        state = runtime.new_session(self.id())
        self.send(runtime, state, "采购的审批流程是什么？")
        self.send(runtime, state, "20000元")
        self.assertIn("采购的审批流程", index.calls[0][0])
        self.assertIn("20000", index.calls[0][0])

    def test_c17_abandon_does_not_erase_partial_success_or_unknown_guard(self):
        self.fixture("SYNTHETIC-RETAIN")
        runtime, _ = self.runtime([])
        state = runtime.new_session(self.id())
        state.active_task_type = "submit"
        state.retry_submit_request_id = "SYNTHETIC-RETAIN"
        state.uncertain_request_id = "SYNTHETIC-RETAIN"
        state.target_request_id = "SYNTHETIC-RETAIN"
        result = self.send(runtime, state, "取消当前任务")
        self.assertEqual(result["outcome_status"], "UNKNOWN")
        self.assertIn("仍保留", result["message"])
        self.assertEqual(state.uncertain_request_id, "SYNTHETIC-RETAIN")
        self.assertIsNotNone(self.call("GET", "/requests/SYNTHETIC-RETAIN")[1])

    def test_c18_resume_prompt_is_also_an_explicit_clarification(self):
        self.fixture("A001")
        runtime, _ = self.runtime([
            proposed("create", "new_task", item="键盘"),
            ModelTurn(None, [helpers.tool("get_request", {"request_id": "A001"})]),
            proposed("query", "switch_task", "request", request_id="A001"),
        ])
        state = runtime.new_session(self.id())
        self.send(runtime, state, "创建键盘申请")
        response = self.send(runtime, state, "先查询 A001")
        self.assertIn("是否继续", state.clarification["question"])
        self.assertNotIn("当前状态", state.clarification["question"])
        self.assertEqual(state.clarification["known_slots"], {"item": "键盘"})
        self.send(runtime, state, "不办了")
        self.assertIsNone(state.suspended_task)
        self.assertIn("A001", state.known_requests)

    def test_c19_default_history_uses_backend_twenty_record_limit(self):
        from test_multiturn_dialogue import MultiturnDialogueTests
        for number in range(21):
            rid = f"SYNTHETIC-HISTORY-{number:02d}"
            call = lambda method, path, data=None, token="demo-u01": MultiturnDialogueTests.call(self, method, path, data, token)
            self.assertEqual(call("POST", "/requests", {"request_id": rid, "item": f"测试品{number}",
                "quantity": 1, "unit_price": "1", "supplier_name": "启明办公", "purpose": "测试"})[0], 201)
            self.assertEqual(call("POST", f"/requests/{rid}/submit", {})[0], 200)
            self.assertEqual(call("POST", f"/requests/{rid}/approve", {}, "demo-m01")[0], 200)
            self.assertEqual(call("POST", f"/requests/{rid}/po", {"idempotency_key": rid})[0], 200)
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("search_purchase_history", {})]),
            proposed("consult", "new_task", "purchase_history"),
        ])
        response = self.send(runtime, runtime.new_session(self.id()), "查询最近的历史成交记录，不限定物品")
        self.assertEqual(len(response["result"]["history"]), 20)
        self.assertEqual(response["result"]["query_scope"]["limit"], 20)
        ids = [r["source"]["request_id"] for r in response["result"]["history"]]
        self.assertEqual(ids[0], "SYNTHETIC-HISTORY-20")
        self.assertNotIn("SYNTHETIC-HISTORY-00", ids)


    def test_c20_verified_default_query_does_not_require_unnecessary_item_clarification(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("search_purchase_history", {})]),
            proposed("consult", "new_task", "purchase_history", question="请指定物品。"),
        ])
        result = self.send(runtime, runtime.new_session(self.id()), "查询历史成交价")
        self.assertEqual(result["route_reason_code"], "READ_VERIFIED")
        self.assertIn("未限定物品", result["message"])
        self.assertNotIn("请指定物品", result["message"])


@unittest.skipUnless(os.getenv("RUN_TASK21_REAL_MODEL") == "1", "NOT_RUN: TASK-21 真实模型需显式开启")
class RealClarificationTests(unittest.TestCase):
    real_model = True
    call = ClarificationIncrementTests.call
    fixture = ClarificationIncrementTests.fixture
    trace_events = ClarificationIncrementTests.trace_events
    setUp = ClarificationIncrementTests.setUp
    tearDown = ClarificationIncrementTests.tearDown
    send = ClarificationIncrementTests.send

    def real_runtime(self):
        from agent_runtime import OpenAICompatibleModel
        from evaluation.rag_baseline import RecordingModel
        from evaluation.runner import sanitized_base_url
        path = Path(os.environ["TASK21_EVIDENCE_DIR"]) / (self._testMethodName + "-model")
        path.mkdir(parents=True, exist_ok=False)
        model = OpenAICompatibleModel.from_env()
        self.addCleanup(model.client.close)
        path.joinpath("config.json").write_text(json.dumps({
            "real_model": True, "model": os.environ["AGENT_MODEL_NAME"],
            "base_url": sanitized_base_url(os.environ["AGENT_MODEL_BASE_URL"]),
            "fixture": "synthetic-only temporary context.sqlite, never demo_test.sqlite",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return AgentRuntime(RecordingModel(model, path / "model_calls"), self.api, self.trace,
                            model_name=os.environ["AGENT_MODEL_NAME"])

    def test_real_r01_unclear_then_default_history(self):
        runtime = self.real_runtime()
        state = runtime.new_session(self.id())
        first = self.send(runtime, state, "ewre")
        self.assertEqual(first["route_action"], "ASK")
        self.assertIsNotNone(state.clarification)
        result = self.send(runtime, state, "历史成交价")
        self.assertEqual(result["route_reason_code"], "READ_VERIFIED", result)
        self.assertIn("未限定物品", result["message"])
        self.assertIn("20", result["message"])
        self.assertNotIn("办公鼠标", result["message"])
        calls = [e for e in self.trace_events(result["trace_id"]) if e.get("tool_name") == "search_purchase_history"
                 and e["event_type"] == "TOOL_CALL_STARTED"]
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["sanitized_args"].get("item"))

    def test_real_r02_create_switch_supplement_confirm_actual_business_state(self):
        runtime = self.real_runtime()
        state = runtime.new_session(self.id())
        first = self.send(runtime, state, "帮我创建键盘采购申请，数量1个，单价100元，供应商启明办公，用于测试办公。")
        self.assertEqual(first["route_action"], "CONFIRM", first)
        second = self.send(runtime, state, "换一笔新的采购申请：创建显示器申请，数量2台。")
        self.assertEqual(second["route_action"], "ASK", second)
        self.assertEqual(state.slots, {"item": "显示器", "quantity": 2})
        pending = self.send(runtime, state, "单价600元，供应商启明办公，用于测试显示。")
        self.assertEqual(pending["route_action"], "CONFIRM", pending)
        self.assertEqual(state.pending_action.args["purpose"], "测试显示")
        before = snapshot_database(Path(self.tmp.name) / "context.sqlite")
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        after = snapshot_database(Path(self.tmp.name) / "context.sqlite")
        self.steps.append({"input": "explicit confirmation", "response": result, "before": before, "after": after})
        self.assertEqual(result["result"]["state"], "SUBMITTED", result)
        rows = self.call("GET", "/requests")[1]["requests"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["item"], rows[0]["quantity"], rows[0]["unit_price"]), ("显示器", 2, "600.00"))
        self.assertIsNone(state.clarification)

    def test_real_r03_value_then_object_and_abandon_confirmation(self):
        self.fixture("SYNTH-A001"); self.fixture("SYNTH-A002")
        runtime = self.real_runtime()
        state = runtime.new_session(self.id())
        first = self.send(runtime, state, "把申请数量改成3。")
        self.assertEqual(first["route_action"], "ASK", first)
        self.assertEqual(state.slots.get("quantity"), 3)
        selected = self.send(runtime, state, "SYNTH-A001")
        self.assertEqual(selected["route_action"], "CONFIRM", selected)
        self.assertEqual(state.pending_action.args, {"quantity": 3})
        self.assertEqual(state.pending_action.target_request_id, "SYNTH-A001")
        self.assertEqual(state.pending_action.expected_version, self.call("GET", "/requests/SYNTH-A001")[1]["version"])
        stopped = self.send(runtime, state, "不办了")
        self.assertEqual(stopped["route_action"], "STOP")
        self.assertIsNone(state.pending_action)
        self.assertIsNone(state.clarification)
        self.assertEqual(self.call("GET", "/requests/SYNTH-A001")[1]["quantity"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
