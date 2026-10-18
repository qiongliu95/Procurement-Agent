"""已确认业务缺陷的正式入口回归；脚本模型和临时 Backend，不使用合同原型。"""
import unittest
from pathlib import Path

from agent_runtime import ModelTurn
from evaluation.fixture import snapshot_database
import test_context_management as helpers
import test_multiturn_dialogue as dialogue_helpers
from test_clarification_increment import FakeKnowledgeIndex


def proposed(intent, relation="new_task", target="current_task", **fields):
    return ModelTurn(None, [helpers.tool("propose_task_state", {
        "intent": intent, "dialogue_relation": relation, "target_object": target, **fields,
    })])


class CollectionQueryTests(unittest.TestCase):
    setUp = helpers.ContextManagementTests.setUp
    tearDown = helpers.ContextManagementTests.tearDown
    runtime = helpers.ContextManagementTests.runtime
    call = helpers.ContextManagementTests.call
    fixture = helpers.ContextManagementTests.fixture
    trace_events = helpers.ContextManagementTests.trace_events

    def test_collection_keeps_full_authorized_list_without_selecting_write_target(self):
        for index in range(10):
            self.fixture(f"A{index:03}")
        self.fixture("HIDDEN")
        with self.app.store.connect() as db:
            db.execute("UPDATE requests SET applicant='U02' WHERE id='HIDDEN'")
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("list_requests", {})]),
            proposed("query", target="request", query_scope="collection"),
            proposed("update", target="request", quantity=3),
        ])
        state = runtime.new_session()
        state.target_request_id, state.last_read_version = "A000", 1
        before = snapshot_database(Path(self.app.store.path))["tables"]
        response = runtime.handle_message(state, "列出我的全部采购申请和进度")
        records = response["result"]["requests"]
        self.assertEqual(len(records), 10)
        self.assertNotIn("HIDDEN", response["message"])
        for record in records:
            self.assertIn(record["request_id"], response["message"])
            self.assertIn(record["state"], response["message"])
        self.assertEqual(response["route_reason_code"], "READ_VERIFIED")
        self.assertIsNone(state.target_request_id)
        self.assertIsNone(state.last_read_version)
        self.assertIsNone(state.pending_action)
        self.assertEqual(state.candidate_request_ids, [r["request_id"] for r in records[:8]])
        calls = [e["tool_name"] for e in self.trace_events(response["trace_id"])
                 if e["event_type"] == "TOOL_CALL_STARTED"]
        self.assertEqual(calls, ["list_requests"])
        self.assertEqual(snapshot_database(Path(self.app.store.path))["tables"], before)
        follow = runtime.handle_message(state, "把数量改成3")
        self.assertEqual(follow["route_reason_code"], "REQUEST_OBJECT_NOT_UNIQUE")
        self.assertIsNone(follow["confirmation"])

    def test_collection_without_model_read_fetches_current_authorized_list(self):
        self.fixture("A001")
        runtime, _ = self.runtime([proposed("query", target="request", query_scope="collection")])
        response = runtime.handle_message(runtime.new_session(), "我提交的单子都怎样了")
        self.assertEqual(response["result"]["requests"][0]["request_id"], "A001")
        self.assertEqual(response["route_reason_code"], "READ_VERIFIED")

    def test_empty_collection_is_a_completed_read(self):
        runtime, _ = self.runtime([proposed("query", target="request", query_scope="collection")])
        response = runtime.handle_message(runtime.new_session(), "查询全部申请")
        self.assertEqual(response["result"]["requests"], [])
        self.assertIn("没有可查看", response["message"])
        self.assertIsNone(response["confirmation"])

    def test_collection_never_bypasses_single_write_object_boundary(self):
        for intent in ("create", "update", "submit"):
            with self.subTest(intent=intent):
                runtime, _ = self.runtime([proposed(intent, target="request", query_scope="collection", quantity=3)])
                state = runtime.new_session()
                response = runtime.handle_message(state, "修改所有申请数量为3")
                self.assertEqual(response["route_reason_code"], "INVALID_QUERY_SCOPE")
                self.assertIsNone(state.active_task_type)
                self.assertEqual(state.slots, {})
                self.assertIsNone(response["confirmation"])


class IntentRetentionTests(unittest.TestCase):
    setUp = helpers.ContextManagementTests.setUp
    tearDown = helpers.ContextManagementTests.tearDown
    runtime = helpers.ContextManagementTests.runtime
    call = helpers.ContextManagementTests.call
    trace_events = helpers.ContextManagementTests.trace_events

    def test_correct_consultation_preserves_create_and_can_resume(self):
        for text in ("顺带讲讲采购门槛", "补充解释下采购流程", "供应商准入规则也说说"):
            with self.subTest(text=text):
                index = FakeKnowledgeIndex()
                runtime, _ = self.runtime([
                    proposed("create", item="键盘", quantity=2, unit_price="100", purpose="办公"),
                    ModelTurn(None, [helpers.tool("search_knowledge", {"query": text})]),
                    proposed("consult", "pause", "knowledge"),
                    ModelTurn("请以已确认采购规则为准。", []),
                ], knowledge_index=index)
                state = runtime.new_session()
                runtime.handle_message(state, "办理2个键盘申请，单价100元，用于办公")
                old = dict(state.slots)
                response = runtime.handle_message(state, text)
                self.assertEqual(response["route_reason_code"], "INTERRUPTED_TASK_WAITING_RESUME")
                self.assertEqual(state.suspended_task.slots, old)
                self.assertEqual(index.calls, [(text, 4)])
                self.assertIsNone(state.pending_action)
                self.assertFalse(any(e["event_type"] in {"TASK_INTENT_RETAINED", "READ_TOOL_REJECTED"}
                                     for e in self.trace_events(response["trace_id"])))
                runtime.handle_message(state, "继续")
                self.assertEqual(state.active_task_type, "create")
                self.assertEqual(state.slots, old)
                self.assertEqual(self.call("GET", "/requests")[1]["requests"], [])

    def test_correct_same_type_switch_drops_old_fields_and_confirmation(self):
        for text in ("这笔先留着，下一单要两台显示器", "另一个需求是显示器，数量2台", "下一单要两台PC"):
            with self.subTest(text=text):
                new_item = "办公电脑" if "PC" in text else "显示器"
                runtime, _ = self.runtime([
                    ModelTurn(None, [helpers.tool("match_supplier", {"name": "启明办公"})]),
                    proposed("create", item="键盘", quantity=1, unit_price="100", supplier_name="启明办公", purpose="办公"),
                    proposed("create", "switch_task", item=new_item, quantity=2,
                             unit_price="100", supplier_name="启明办公", purpose="办公"),
                ])
                state = runtime.new_session()
                first = runtime.handle_message(state, "办理1个键盘申请，单价100元，供应商启明办公，用于办公")
                self.assertEqual(first["route_action"], "CONFIRM")
                response = runtime.handle_message(state, text)
                self.assertEqual(state.slots, {"item": new_item, "quantity": 2})
                self.assertEqual(response["route_action"], "ASK")
                self.assertIsNone(state.pending_action)
                replay = runtime.confirm(state, first["confirmation"]["action_id"], "confirm")
                self.assertEqual(replay["route_reason_code"], "CONFIRMATION_NOT_FOUND")
                self.assertEqual(self.call("GET", "/requests")[1]["requests"], [])

    def test_new_task_label_with_same_item_keeps_current_supplement(self):
        runtime, _ = self.runtime([
            proposed("create", item="键盘", quantity=2, purpose="办公"),
            proposed("create", item="键盘", unit_price="100"),
        ])
        state = runtime.new_session()
        runtime.handle_message(state, "办理2个键盘申请，用于办公")
        runtime.handle_message(state, "单价100元")
        self.assertEqual(state.slots, {"item": "键盘", "quantity": 2, "purpose": "办公", "unit_price": "100"})
        self.assertIsNone(state.pending_action)

    def test_switch_quantity_equal_to_old_price_does_not_inherit_price(self):
        runtime, _ = self.runtime([
            proposed("create", item="键盘", quantity=2, unit_price="100", purpose="办公"),
            proposed("create", "switch_task", item="显示器", quantity=100, unit_price="100", purpose="办公"),
        ])
        state = runtime.new_session()
        runtime.handle_message(state, "办理2个键盘申请，单价100元，用于办公")
        runtime.handle_message(state, "下一单要100台显示器")
        self.assertEqual(state.slots, {"item": "显示器", "quantity": 100})
        self.assertIsNone(state.pending_action)


class CorrectionContextTests(unittest.TestCase):
    setUp = dialogue_helpers.MultiturnDialogueTests.setUp
    tearDown = dialogue_helpers.MultiturnDialogueTests.tearDown
    runtime = helpers.ContextManagementTests.runtime
    call = dialogue_helpers.MultiturnDialogueTests.call
    fixture = dialogue_helpers.MultiturnDialogueTests.fixture
    trace_events = helpers.ContextManagementTests.trace_events

    def test_history_correction_projects_trusted_fields_without_new_read(self):
        self.fixture("H001", item="办公鼠标", quantity=3, price="50", po=True)
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("search_purchase_history", {"item": "办公鼠标"})]),
            proposed("consult", target="purchase_history"),
            proposed("consult", "correction", "purchase_history", history_fields=["supplier_name", "quantity"]),
        ])
        state = runtime.new_session()
        runtime.handle_message(state, "查询办公鼠标历史成交价")
        before = snapshot_database(Path(self.app.store.path))["tables"]
        response = runtime.handle_message(state, "不是问价格，是供应商和数量")
        self.assertEqual(response["route_reason_code"], "READ_VERIFIED")
        self.assertIn("启明办公", response["message"])
        self.assertIn("3 件", response["message"])
        events = self.trace_events(response["trace_id"])
        self.assertTrue(any(e["event_type"] == "TRUSTED_READ_CONTEXT_REUSED" for e in events))
        self.assertFalse(any(e["event_type"] == "TOOL_CALL_STARTED" for e in events))
        self.assertEqual(snapshot_database(Path(self.app.store.path))["tables"], before)
        self.assertIsNone(state.active_task_type)
        self.assertIsNone(state.pending_action)

    def test_collection_goal_survives_wrong_domain_answer_and_correction_refreshes_list(self):
        self.fixture("A001")
        runtime, model = self.runtime([
            proposed("query", target="request", query_scope="collection"),
            ModelTurn(None, [helpers.tool("search_purchase_history", {})]),
            proposed("consult", "switch_task", "purchase_history"),
            proposed("query", "correction", "request"),
            proposed("query", "correction", "request", request_id="A001"),
            proposed("query", "correction", "request"),
        ])
        state = runtime.new_session()
        runtime.handle_message(state, "查看我的全部采购申请进度")
        runtime.handle_message(state, "我历史申请的采购单")
        self.fixture("A002")
        response = runtime.handle_message(state, "答偏了，我问的是申请进度")
        self.assertEqual(response["result"]["query_scope"], "collection")
        self.assertEqual({r["request_id"] for r in response["result"]["requests"]}, {"A001", "A002"})
        self.assertIn("查看我的全部采购申请进度", model.received[-1][0]["content"])
        self.assertEqual(state.trusted_read_contexts["request"]["query"]["user_goal"], "查看我的全部采购申请进度")
        self.assertIsNone(state.target_request_id)
        single = runtime.handle_message(state, "只看 A001")
        self.assertEqual(single["result"]["request_id"], "A001")
        self.assertNotEqual(single["result"]["result_type"], "requests")
        corrected_single = runtime.handle_message(state, "不是问金额，是当前状态")
        self.assertEqual(corrected_single["result"]["request_id"], "A001")
        self.assertEqual(corrected_single["result"]["result_type"], "request")

    def test_correction_does_not_mix_domains_or_invent_missing_context(self):
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("search_products", {"query": "显示器"})]),
            proposed("consult", target="product"),
            proposed("consult", "correction", "purchase_history", history_fields=["supplier_name"]),
        ])
        state = runtime.new_session()
        runtime.handle_message(state, "查询显示器商品目录")
        response = runtime.handle_message(state, "不是目录，我问历史供应商")
        self.assertEqual(response["route_reason_code"], "TRUSTED_READ_CONTEXT_UNAVAILABLE")
        self.assertIsNone(response["confirmation"])
        self.assertEqual(state.slots, {})

    def test_explicit_list_selection_in_correction_has_priority_over_old_collection_scope(self):
        self.fixture("A001")
        self.fixture("A002")
        runtime, model = self.runtime([proposed("query", target="request", query_scope="collection")])
        state = runtime.new_session()
        listing = runtime.handle_message(state, "查看全部采购申请")
        selected = listing["result"]["requests"][1]["request_id"]
        model.turns.append(proposed("query", "correction", "request", request_id=selected))
        response = runtime.handle_message(state, "不是全部，是第二个")
        self.assertEqual(response["result"]["result_type"], "request")
        self.assertEqual(response["result"]["request_id"], selected)
        self.assertIsNone(response["confirmation"])

    def test_knowledge_correction_reuses_same_domain_evidence(self):
        index = FakeKnowledgeIndex()
        runtime, _ = self.runtime([
            ModelTurn(None, [helpers.tool("search_knowledge", {"query": "一万元审批"})]),
            proposed("consult", target="knowledge"), ModelTurn("由 M02 审批。", []),
            proposed("consult", "correction", "knowledge"), ModelTurn("审核角色是 M02。", []),
        ], knowledge_index=index)
        state = runtime.new_session()
        runtime.handle_message(state, "一万元对应哪一级审批")
        response = runtime.handle_message(state, "不是问材料，问审核角色")
        self.assertEqual(response["route_reason_code"], "READ_VERIFIED")
        self.assertIn("M02", response["message"])
        self.assertEqual(len(index.calls), 1)
        self.assertIsNone(state.pending_action)
