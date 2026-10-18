"""共享合同先于聊天路径接入；纯结构化测试不调用真实模型。"""
import unittest

from agent_runtime import TaskContract
from agent_runtime import AgentRuntime, ModelTurn
import test_context_management as helpers


def proposal(intent, relation="supplement", target="current_task", **fields):
    return ModelTurn(None, [helpers.tool("propose_task_state", {
        "intent": intent, "dialogue_relation": relation, "target_object": target, **fields,
    })])


class TaskContractTests(unittest.TestCase):
    def test_legacy_proposal_is_single_and_does_not_grant_authority(self):
        contract = TaskContract.from_proposal({"intent": "update", "quantity": 3})
        self.assertEqual(contract.query_scope, "single")
        self.assertFalse(hasattr(contract, "authorized"))

    def test_collection_is_only_for_request_query(self):
        for intent in ("create", "update", "submit", "consult"):
            with self.subTest(intent=intent), self.assertRaisesRegex(ValueError, "INVALID_QUERY_SCOPE"):
                TaskContract.from_proposal({"intent": intent, "target_object": "request", "query_scope": "collection"})
        self.assertEqual(TaskContract.from_proposal({
            "intent": "query", "target_object": "request", "query_scope": "collection",
        }).query_scope, "collection")

    def test_invalid_structure_is_rejected(self):
        for extra in ({"dialogue_relation": "guess"}, {"target_object": "all"},
                      {"query_scope": "any"}, {"answer_status": "done"}, {"intent": "approve"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                TaskContract.from_proposal({"intent": "query", **extra})

    def test_same_structured_switch_is_independent_of_action_words(self):
        contract = TaskContract("create", "switch_task", "current_task")
        self.assertEqual(contract.create_slot_policy(), "clarify")
        self.assertEqual(contract.create_slot_policy(accepted_operation="switch"), "replace")

    def test_label_only_does_not_clear_partial_task(self):
        contract = TaskContract("create", "new_task")
        self.assertEqual(contract.create_slot_policy(), "clarify")
        self.assertEqual(contract.create_slot_policy(accepted_operation="supplement"), "merge")

    def test_ungrounded_new_object_cannot_replace_task(self):
        contract = TaskContract("create", "switch_task")
        self.assertEqual(contract.create_slot_policy(), "clarify")

    def test_same_object_new_task_is_ambiguous_not_silent_reuse(self):
        self.assertEqual(TaskContract("create", "new_task").create_slot_policy(), "clarify")

    def test_evidence_matching_proposal_does_not_override_user_goal(self):
        contract = TaskContract("consult", "follow_up", "purchase_history")
        goal = {"user_goal": "我所有申请的审批进度", "interpretation": {"target_object": "request"}}
        self.assertEqual(contract.read_completion_issue(goal, {"purchase_history"}), "READ_GOAL_DOMAIN_CONFLICT")

    def test_scope_mismatch_does_not_complete_collection_goal(self):
        contract = TaskContract("query", "follow_up", "request")
        self.assertEqual(contract.read_completion_issue({"interpretation": {"query_scope": "collection"}},
                                                       {"request"}), "READ_GOAL_SCOPE_CONFLICT")

    def test_model_needs_input_even_with_matching_evidence(self):
        for status in ("needs_input", "evidence_insufficient"):
            self.assertEqual(TaskContract("consult", target_object="knowledge", answer_status=status)
                             .read_completion_issue({}, {"knowledge"}), "READ_GOAL_NEEDS_INPUT")

    def test_no_evidence_cannot_complete_read_goal(self):
        self.assertEqual(TaskContract("consult", target_object="knowledge")
                         .read_completion_issue({}, set()), "CONSULTATION_SOURCE_REQUIRED")


@unittest.skip("NOT_IMPLEMENTED: 阶段2接入因 REQ-05 安全回归已回退，不作为通过项")
class SharedStateIntegrationTests(unittest.TestCase):
    setUp = helpers.ContextManagementTests.setUp
    tearDown = helpers.ContextManagementTests.tearDown
    runtime = helpers.ContextManagementTests.runtime
    trace_events = helpers.ContextManagementTests.trace_events

    def test_correct_switch_does_not_need_action_keywords(self):
        for text in ("下一单要两台显示器", "另一个需求是显示器，数量2台"):
            with self.subTest(text=text):
                runtime, _ = self.runtime([
                    proposal("create", "new_task", item="键盘", quantity=1, unit_price="100", purpose="办公"),
                    proposal("create", "switch_task", item="显示器", quantity=2),
                ])
                state = runtime.new_session()
                runtime.handle_message(state, "办理键盘申请，数量1个，单价100元，用于办公")
                result = runtime.handle_message(state, text)
                self.assertEqual(state.slots, {"item": "显示器", "quantity": 2})
                self.assertEqual(result["route_action"], "ASK")
                self.assertIsNone(state.pending_action)

    def test_consultation_does_not_get_reclassified_by_active_create(self):
        runtime, model = self.runtime([
            proposal("create", "new_task", item="键盘", quantity=2),
            ModelTurn(None, [helpers.tool("get_procurement_rules", {})]),
            proposal("consult", "pause", "knowledge"),
        ])
        state = runtime.new_session()
        runtime.handle_message(state, "办理键盘申请，数量2个")
        result = runtime.handle_message(state, "顺带讲讲采购门槛")
        self.assertEqual(result["route_reason_code"], "INTERRUPTED_TASK_WAITING_RESUME")
        self.assertEqual(state.suspended_task.slots, {"item": "键盘", "quantity": 2})
        self.assertFalse(any(e["event_type"] == "TASK_INTENT_RETAINED" for e in self.trace_events(result["trace_id"])))
        self.assertEqual(len(model.received), 3)

    def test_goal_origin_and_last_answer_survive_without_writable_slots(self):
        runtime, model = self.runtime([
            ModelTurn(None, [helpers.tool("search_purchase_history", {"item": "键盘"})]),
            proposal("consult", "new_task", "purchase_history"),
            proposal("consult", "correction", "purchase_history"),
        ])
        state = runtime.new_session()
        runtime.handle_message(state, "键盘历史成交")
        self.assertIsNone(state.task_goal)
        self.assertEqual(state.last_task_goal["user_goal"], "键盘历史成交")
        response = runtime.handle_message(state, "不是要价格，问的是成交记录")
        self.assertEqual(response["route_reason_code"], "READ_VERIFIED")
        self.assertIn("键盘历史成交", model.received[-1][0]["content"])
        self.assertEqual(state.slots, {})


if __name__ == "__main__":
    unittest.main()
