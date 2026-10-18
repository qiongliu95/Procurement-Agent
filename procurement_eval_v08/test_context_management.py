"""上下文管理 T01-T14；同时校验状态与实际传给模型的消息。"""
import copy
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
    return ModelToolCall(f"context-call-{index}", name, arguments)


class CapturingModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.received = []

    def complete(self, messages, tools):
        self.received.append(copy.deepcopy(messages))
        if not self.turns:
            raise AssertionError("CapturingModel exhausted")
        return self.turns.pop(0)


class ContextManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(("127.0.0.1", 0), Store(Path(self.tmp.name) / "context.sqlite"))
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

    def call(self, method, path, data=None):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer demo-u01", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def fixture(self, request_id):
        status, _ = self.call("POST", "/requests", {
            "request_id": request_id, "item": "显示器", "quantity": 2,
            "unit_price": "800.00", "supplier_name": "启明办公", "purpose": "办公",
        })
        self.assertEqual(status, 201)
        self.call("POST", f"/requests/{request_id}/submit", {})

    def runtime(self, turns, **kwargs):
        model = CapturingModel(turns)
        runtime = AgentRuntime(model, self.api, self.trace, model_name="context-script", **kwargs)
        return runtime, model

    def trace_events(self, run_id):
        events = []
        for path in (Path(self.tmp.name) / "traces").glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("run_id") == run_id:
                    events.append(event)
        return events

    @staticmethod
    def partial_create_turn(item="显示器"):
        return ModelTurn(None, [tool("propose_task_state", {
            "intent": "create", "item": item,
            "question": "还需要数量、单价、供应商和用途。",
        })])

    @staticmethod
    def query_turns(request_id, start=1):
        return [
            ModelTurn(None, [tool("get_request", {"request_id": request_id}, start)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "query", "request_id": request_id,
            }, start + 1)]),
        ]

    def test_t01_t02_create_interrupted_then_resumed(self):
        self.fixture("CTX-T01")
        runtime, _ = self.runtime([
            self.partial_create_turn(), *self.query_turns("CTX-T01", 2),
        ])
        state = runtime.new_session("T01")
        runtime.handle_message(state, "帮我创建显示器采购申请")
        queried = runtime.handle_message(state, "先查一下 CTX-T01")
        self.assertEqual(queried["route_reason_code"], "INTERRUPTED_TASK_WAITING_RESUME")
        self.assertEqual(state.suspended_task.slots["item"], "显示器")
        events = self.trace_events(queried["trace_id"])
        routes = [event for event in events if event["event_type"] == "ROUTE_DECIDED"]
        finals = [event for event in events if event["event_type"] == "FINAL_RESPONSE"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(len(finals), 1)
        self.assertEqual(routes[0]["route_reason_code"], "INTERRUPTED_TASK_WAITING_RESUME")
        self.assertIn("CTX-T01 当前状态", finals[0]["final_response"])
        self.assertIn("是否继续刚才的采购任务", finals[0]["final_response"])
        resumed = runtime.handle_message(state, "继续")
        self.assertEqual(resumed["route_reason_code"], "INTERRUPTED_TASK_RESUMED")
        self.assertEqual((state.active_task_type, state.slots["item"]), ("create", "显示器"))

    def test_t03_abandon_removes_interrupted_task(self):
        self.fixture("CTX-T03")
        runtime, _ = self.runtime([
            self.partial_create_turn(), *self.query_turns("CTX-T03", 2),
        ])
        state = runtime.new_session("T03")
        runtime.handle_message(state, "创建显示器采购申请")
        runtime.handle_message(state, "先查询 CTX-T03")
        result = runtime.handle_message(state, "不继续")
        self.assertEqual(result["route_reason_code"], "INTERRUPTED_TASK_ABANDONED")
        self.assertIsNone(state.suspended_task)
        self.assertEqual(state.slots, {})

    def test_t04_queries_do_not_accumulate_in_model_context(self):
        self.fixture("CTX-Q1")
        self.fixture("CTX-Q2")
        runtime, model = self.runtime([
            self.partial_create_turn(),
            *self.query_turns("CTX-Q1", 2),
            *self.query_turns("CTX-Q2", 4),
        ], model_context_messages=4)
        state = runtime.new_session("T04")
        runtime.handle_message(state, "创建显示器采购申请")
        runtime.handle_message(state, "查询 CTX-Q1")
        runtime.handle_message(state, "再查询 CTX-Q2")
        latest_initial = model.received[-2]
        self.assertLessEqual(len(latest_initial), 6)
        self.assertNotIn("CTX-Q1 当前状态", json.dumps(latest_initial, ensure_ascii=False))
        self.assertIsNotNone(state.suspended_task)

    def test_t05_query_then_update_pronoun_uses_reference_and_rereads(self):
        self.fixture("CTX-A001")
        runtime, model = self.runtime([
            *self.query_turns("CTX-A001"),
            ModelTurn(None, [tool("get_request", {"request_id": "CTX-A001"}, 3)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "update", "request_id": "CTX-A001", "quantity": 3,
            }, 4)]),
        ])
        state = runtime.new_session("T05")
        queried = runtime.handle_message(state, "查一下 CTX-A001")
        query_events = self.trace_events(queried["trace_id"])
        self.assertEqual(
            len([event for event in query_events if event["event_type"] == "ROUTE_DECIDED"]), 1
        )
        self.assertEqual(
            len([event for event in query_events if event["event_type"] == "FINAL_RESPONSE"]), 1
        )
        self.assertEqual(queried["route_reason_code"], "READ_VERIFIED")
        pending = runtime.handle_message(state, "把它的数量改成3")
        self.assertEqual(pending["route_action"], "CONFIRM")
        self.assertIn("CTX-A001", model.received[-2][0]["content"])
        self.assertEqual(state.last_read_version, 1)

    def test_t06_multiple_candidates_are_clarified(self):
        self.fixture("CTX-C1")
        self.fixture("CTX-C2")
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("list_requests", {})]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "update", "question": "请指定要修改的申请编号。",
            }, 2)]),
        ])
        state = runtime.new_session("T06")
        response = runtime.handle_message(state, "修改第二个之前先看看候选")
        self.assertEqual(response["route_reason_code"], "REQUEST_OBJECT_NOT_UNIQUE")
        self.assertEqual(len(state.candidate_request_ids), 2)

    def test_old_target_does_not_override_new_ambiguous_candidates(self):
        self.fixture("CTX-OLD")
        self.fixture("CTX-NEW-1")
        self.fixture("CTX-NEW-2")
        runtime, _ = self.runtime([
            *self.query_turns("CTX-OLD"),
            ModelTurn(None, [tool("list_requests", {}, 3)]),
            ModelTurn(None, [tool("propose_task_state", {
                # 即使模型受历史引用影响再次提出旧 ID，Runtime 也必须以当前歧义为准。
                "intent": "update", "request_id": "CTX-OLD", "quantity": 9,
                "question": "请指定要修改的申请编号。",
            }, 4)]),
        ])
        state = runtime.new_session("T06-OLD-TARGET")
        runtime.handle_message(state, "查询 CTX-OLD")
        response = runtime.handle_message(state, "把刚才候选中的申请数量改成9")

        self.assertEqual(response["route_reason_code"], "REQUEST_OBJECT_NOT_UNIQUE")
        self.assertIsNone(response["confirmation"])
        self.assertGreater(len(state.candidate_request_ids), 1)
        for request_id in ("CTX-OLD", "CTX-NEW-1", "CTX-NEW-2"):
            self.assertEqual(self.call("GET", f"/requests/{request_id}")[1]["quantity"], 2)
        events = self.trace_events(response["trace_id"])
        write_tools = {"create_request", "update_request", "submit_request"}
        self.assertFalse(any(
            event.get("event_type") == "TOOL_CALL_STARTED"
            and event.get("tool_name") in write_tools
            for event in events
        ))

    def test_multiple_candidates_allow_explicit_selection(self):
        self.fixture("CTX-SELECT-1")
        self.fixture("CTX-SELECT-2")
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("list_requests", {})]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "update", "question": "请指定要修改的申请编号。",
            }, 2)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "update", "request_id": "CTX-SELECT-2", "quantity": 5,
            }, 3)]),
        ])
        state = runtime.new_session("T06-EXPLICIT")
        first = runtime.handle_message(state, "列出候选后修改其中一条")
        self.assertEqual(first["route_reason_code"], "REQUEST_OBJECT_NOT_UNIQUE")

        selected = runtime.handle_message(state, "修改 CTX-SELECT-2，数量改成5")
        self.assertEqual(selected["route_action"], "CONFIRM")
        self.assertEqual(state.pending_action.target_request_id, "CTX-SELECT-2")
        self.assertIn("CTX-SELECT-2", selected["message"])

    def test_t07_public_session_restores_history_and_task(self):
        runtime, _ = self.runtime([self.partial_create_turn()])
        state = runtime.new_session("T07")
        runtime.handle_message(state, "创建显示器采购申请")
        restored = runtime.public_session(state)
        self.assertEqual(restored["active_task_type"], "create")
        self.assertEqual([item["role"] for item in restored["messages"]], ["user", "assistant"])

    def test_t08_pending_confirmation_is_restored_and_revalidated(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"})]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create", "item": "键盘", "quantity": 1,
                "unit_price": "100.00", "supplier_name": "启明办公", "purpose": "办公",
            }, 2)]),
        ])
        state = runtime.new_session("T08")
        pending = runtime.handle_message(state, "采购1个键盘，单价100元，供应商启明办公，用于办公")
        restored = runtime.public_session(state)
        self.assertEqual(restored["pending_confirmation"]["action_id"], pending["confirmation"]["action_id"])
        done = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(done["route_reason_code"], "WRITE_VERIFIED")

    def test_t09_clear_resets_every_derived_state_and_old_confirmation(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"})]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create", "item": "鼠标", "quantity": 1,
                "unit_price": "50.00", "supplier_name": "启明办公", "purpose": "办公",
            }, 2)]),
        ])
        state = runtime.new_session("T09")
        pending = runtime.handle_message(state, "采购1个鼠标，单价50元，供应商启明办公，用于办公")
        old_action = pending["confirmation"]["action_id"]
        state.trusted_read_contexts["product"] = {"data": {"products": [{"code": "P001"}]}}
        state.candidate_request_ids = ["OLD-CANDIDATE"]
        runtime.clear_session(state)
        self.assertIsNone(state.pending_action)
        self.assertEqual((state.slots, state.recent_request_ids, state.known_requests), ({}, [], {}))
        self.assertEqual((state.trusted_read_contexts, state.candidate_request_ids), ({}, []))
        self.assertEqual(state.conversation_tail, [])
        denied = runtime.confirm(state, old_action, "confirm")
        self.assertEqual(denied["route_reason_code"], "CONFIRMATION_NOT_FOUND")

    def test_t10_new_create_after_clear_does_not_inherit_slots(self):
        runtime, _ = self.runtime([
            self.partial_create_turn("显示器"), self.partial_create_turn("电脑"),
        ])
        state = runtime.new_session("T10")
        runtime.handle_message(state, "创建显示器采购")
        runtime.clear_session(state)
        runtime.handle_message(state, "创建电脑采购")
        self.assertEqual(state.slots, {"item": "电脑"})

    def test_t11_completed_task_is_removed_from_next_model_context(self):
        runtime, model = self.runtime([
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"})]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create", "item": "键盘", "quantity": 1,
                "unit_price": "100.00", "supplier_name": "启明办公", "purpose": "办公",
            }, 2)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "ambiguous", "question": "请说明新的目标。",
            }, 3)]),
        ])
        state = runtime.new_session("T11")
        pending = runtime.handle_message(state, "采购1个键盘，单价100元，供应商启明办公，用于办公")
        runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        runtime.handle_message(state, "再聊一个新问题")
        initial = model.received[-1]
        self.assertEqual(len(initial), 2)
        self.assertIn('"active_task": null', initial[0]["content"])

    def test_t12_long_history_keeps_structured_task_not_full_history(self):
        runtime, model = self.runtime([self.partial_create_turn()], model_context_messages=4)
        state = runtime.new_session("T12")
        for number in range(40):
            state.conversation_tail.append({"role": "user", "content": f"无关历史 {number}"})
        runtime.handle_message(state, "创建显示器采购申请")
        sent = model.received[0]
        self.assertEqual(len(sent), 2)
        self.assertNotIn("无关历史 39", json.dumps(sent, ensure_ascii=False))
        self.assertEqual(state.slots["item"], "显示器")

    def test_t13_explicit_new_object_overrides_old_reference(self):
        self.fixture("CTX-OLD")
        self.fixture("CTX-NEW")
        runtime, _ = self.runtime([
            *self.query_turns("CTX-OLD"),
            ModelTurn(None, [tool("get_request", {"request_id": "CTX-NEW"}, 3)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "update", "request_id": "CTX-NEW", "quantity": 4,
            }, 4)]),
        ])
        state = runtime.new_session("T13")
        runtime.handle_message(state, "查询 CTX-OLD")
        pending = runtime.handle_message(state, "把 CTX-NEW 数量改成4")
        self.assertEqual(state.target_request_id, "CTX-NEW")
        self.assertIn("CTX-NEW", pending["message"])

    def test_t14_abandon_invalidates_confirmation_without_write(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"})]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create", "item": "耳机", "quantity": 1,
                "unit_price": "80.00", "supplier_name": "启明办公", "purpose": "办公",
            }, 2)]),
        ])
        state = runtime.new_session("T14")
        pending = runtime.handle_message(state, "采购1个耳机，单价80元，供应商启明办公，用于办公")
        action_id = pending["confirmation"]["action_id"]
        runtime.confirm(state, action_id, "cancel")
        denied = runtime.confirm(state, action_id, "confirm")
        self.assertEqual(denied["route_reason_code"], "CONFIRMATION_NOT_FOUND")
        self.assertEqual(self.call("GET", f"/requests/{state.target_request_id}")[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
