"""任务来源门禁隔离验证：真实Session/只读工具/临时Backend，无生产聊天接入。"""
import copy
import json
import unittest
import uuid
from dataclasses import asdict
from unittest.mock import patch

import test_context_management as helpers
from agent_runtime import AgentRuntime, TaskContract


def proposed(intent="create", relation="new_task", target="current_task", **fields):
    return {"intent": intent, "dialogue_relation": relation, "target_object": target, **fields}


class TaskSourceBoundaryTests(unittest.TestCase):
    setUp = helpers.ContextManagementTests.setUp
    tearDown = helpers.ContextManagementTests.tearDown
    trace_events = helpers.ContextManagementTests.trace_events

    def runtime(self):
        return AgentRuntime(helpers.CapturingModel([]), self.api, self.trace, model_name="no-model-contract")

    def user_turn(self, state, text):
        state.current_run_id = "run-source-" + uuid.uuid4().hex
        state.conversation_tail.append({"role": "user", "content": text})

    def stage(self, state, text, proposal):
        self.user_turn(state, text)
        before = asdict(state)
        contract = TaskContract.from_proposal(proposal)
        result = contract.stage_candidate(state, proposal)
        after = asdict(state)
        # 暂存只允许修改澄清，不允许“尚未写库但已建立待办理任务”。
        before.pop("clarification")
        after.pop("clarification")
        self.assertEqual(before, after)
        self.assertEqual(result["route_action"], "ASK")
        self.assertIn("尚未接受", result["message"])
        self.assertFalse(result["task_accepted"])
        self.assertFalse(result["business_write_authorized"])
        self.assertIsNone(result["confirmation"])
        return contract, result

    def choose(self, state, contract, result, operation):
        # 模拟可信宿主接收人的绑定选择；不是调用模型识别“是/继续”。
        response = contract.commit_candidate(state, result["candidate_id"], operation)
        self.assertFalse(response["business_write_authorized"])
        self.assertIsNone(response["confirmation"])
        self.assertIsNone(state.pending_action)
        self.assertIsNone(response["user_goal_satisfied"])
        return response

    def start(self, state, text="办理键盘申请", **fields):
        contract, result = self.stage(state, text, proposed(**{"item": "键盘", **fields}))
        self.assertTrue(self.choose(state, contract, result, "start")["task_accepted"])

    def assert_no_business_writes(self, runtime, state):
        events = self.trace_events(state.current_run_id)
        self.assertFalse(any(e.get("tool_name") in {"create_request", "update_request", "submit_request"} for e in events))
        with self.app.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_history_query_wrong_create_never_creates_pending_task(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.user_turn(state, "查询电脑历史价格")
        safe = TaskContract.read_attempt(runtime, state, helpers.tool("search_purchase_history", {"item": "电脑"}),
                                         "查询电脑历史价格")
        self.assertIn("records", safe)
        contract, result = self.stage(state, "查询电脑历史价格", proposed(item="电脑", quantity=1, unit_price="3000"))
        self.assertIsNone(state.active_task_type)
        self.assertEqual(state.slots, {})
        self.assertIsNone(state.target_request_id)
        declined = self.choose(state, contract, result, "reject")
        self.assertIn("原任务未改变", declined["message"])
        self.assertIsNone(state.active_task_type)
        self.assert_no_business_writes(runtime, state)

    def test_knowledge_query_wrong_update_and_submit_stay_candidates(self):
        for intent in ("update", "submit"):
            state = self.runtime().new_session()
            contract, response = self.stage(state, "采购制度有什么要求？", proposed(
                intent, target="request", request_id="A001", quantity=3, confirmed=True, source="user"))
            self.assertIsNone(state.active_task_type)
            self.assertEqual(state.slots, {})
            self.assertIsNone(state.task_origin)
            self.assertNotIn("confirmed", state.clarification["candidate"]["proposal"])
            self.choose(state, contract, response, "reject")

    def test_explicit_create_with_read_first_is_not_lost_or_auto_authorized(self):
        runtime = self.runtime()
        state = runtime.new_session()
        text = "请办理电脑采购申请"
        self.user_turn(state, text)
        result = TaskContract.read_attempt(runtime, state, helpers.tool("list_requests", {}), text)
        self.assertIn("requests", result)
        contract, response = self.stage(state, text, proposed(item="电脑", quantity=2))
        self.assertIsNone(state.active_task_type)
        accepted = self.choose(state, contract, response, "start")
        self.assertEqual(state.slots, {"item": "电脑", "quantity": 2})
        self.assertEqual(state.active_task_type, "create")
        self.assertIn("具体操作确认", accepted["message"])
        self.assert_no_business_writes(runtime, state)

    def test_mixed_query_and_create_requires_task_choice_not_tool_order(self):
        for text in ("先查电脑历史价，再发起两台电脑申请", "办理两台电脑申请，同时查过去价格"):
            runtime = self.runtime()
            state = runtime.new_session()
            self.user_turn(state, text)
            TaskContract.read_attempt(runtime, state, helpers.tool("search_purchase_history", {"item": "电脑"}), text)
            contract, response = self.stage(state, text, proposed(item="电脑", quantity=2))
            self.assertEqual(state.slots, {})
            self.assertTrue(self.choose(state, contract, response, "start")["task_accepted"])
            self.assertEqual(state.slots, {"item": "电脑", "quantity": 2})
            self.assert_no_business_writes(runtime, state)

    def test_supplement_fields_only_after_choice_and_keeps_prior_fields(self):
        state = self.runtime().new_session()
        self.start(state, quantity=1)
        for text, fields in (("需要两个", {"quantity": 2}), ("每件一百", {"unit_price": "100"}),
                             ("供货选启明办公", {"supplier_name": "启明办公"})):
            before = dict(state.slots)
            contract, response = self.stage(state, text, proposed(relation="supplement", **fields))
            self.assertEqual(state.slots, before)
            self.assertTrue(self.choose(state, contract, response, "supplement")["task_accepted"])
            self.assertEqual(state.slots, {**before, **fields})

    def test_temporary_consultation_pause_and_resume_preserves_origin(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(state, quantity=2, unit_price="100")
        old_slots, old_source = copy.deepcopy(state.slots), copy.deepcopy(state.task_origin)
        text = "顺带说说采购制度是什么？"
        self.user_turn(state, text)
        TaskContract.read_attempt(runtime, state, helpers.tool("get_procurement_rules", {}), text)
        self.assertEqual(state.slots, old_slots)
        self.assertEqual(state.task_origin, old_source)
        contract, response = self.stage(state, text, proposed("consult", "pause", "knowledge"))
        self.assertEqual(state.active_task_type, "create")
        self.choose(state, contract, response, "pause")
        self.assertEqual(state.suspended_task.slots, old_slots)
        self.assertEqual(state.suspended_task.task_origin, old_source)
        contract, response = self.stage(state, "接着办理原申请", proposed(relation="resume"))
        self.choose(state, contract, response, "resume")
        self.assertEqual(state.slots, old_slots)
        self.assertIsNone(state.last_read_version)
        self.assertIsNone(state.suspended_task)
        self.assert_no_business_writes(runtime, state)

    def test_synonym_switch_has_no_literal_item_gate(self):
        for user_text, model_item in (("下一笔需要计算机两台", "电脑"), ("另办两台PC", "办公电脑")):
            runtime = self.runtime()
            state = runtime.new_session()
            self.start(state, quantity=1, unit_price="100", purpose="办公")
            self.assertNotIn(model_item, user_text)
            contract, response = self.stage(state, user_text, proposed(relation="switch_task", item=model_item, quantity=2))
            self.assertEqual(state.slots["item"], "键盘")
            self.choose(state, contract, response, "switch")
            self.assertEqual(state.slots, {"item": model_item, "quantity": 2})
            self.assert_no_business_writes(runtime, state)

    def test_wrong_switch_label_cannot_replace_supplement_task(self):
        state = self.runtime().new_session()
        self.start(state, item="键盘", quantity=1, purpose="办公")
        contract, response = self.stage(state, "每个100元", proposed(relation="switch_task", unit_price="100"))
        self.assertEqual(state.slots, {"item": "键盘", "quantity": 1, "purpose": "办公"})
        accepted = self.choose(state, contract, response, "supplement")
        self.assertTrue(accepted["task_accepted"])
        self.assertEqual(state.slots, {"item": "键盘", "quantity": 1, "purpose": "办公", "unit_price": "100"})
        self.assertEqual(state.task_origin["operation"], "supplement")

    def test_unknown_supplier_uses_original_prepare_clarification(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(state, quantity=2, unit_price="100", purpose="办公", supplier_name="不存在供应商")
        safe = TaskContract.read_attempt(runtime, state, helpers.tool("match_supplier", {"name": "不存在供应商"}),
                                         "供应商不存在供应商")
        self.assertEqual(safe["matches"], [])
        before = dict(state.slots)
        response = runtime._prepare_create(state, {})
        self.assertEqual(response["route_action"], "ASK")
        self.assertEqual(response["route_reason_code"], "SUPPLIER_NOT_UNIQUELY_VERIFIED")
        self.assertIn("核实", response["message"])
        self.assertIn("完整供应商名称", response["message"])
        self.assertEqual(state.active_task_type, "create")
        self.assertEqual(state.slots, before)
        self.assertIsNone(state.pending_action)
        self.assert_no_business_writes(runtime, state)

    def test_trace_attempt_rejection_and_real_execution_are_distinct(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.user_turn(state, "补充数量")
        rejected = TaskContract.read_attempt(runtime, state, helpers.tool("get_request", {"request_id": "GHOST"}), "补充数量")
        self.assertIn("error", rejected)
        events = self.trace_events(state.current_run_id)
        self.assertEqual([e["event_type"] for e in events], ["MODEL_TOOL_ATTEMPTED", "MODEL_TOOL_REJECTED"])
        self.assertTrue(all(e["proposed_tool_name"] == "get_request" and "tool_name" not in e for e in events))
        self.assertFalse(any(e["event_type"] == "TOOL_CALL_STARTED" for e in events))
        self.user_turn(state, "我的申请")
        TaskContract.read_attempt(runtime, state, helpers.tool("list_requests", {}), "我的申请")
        events = self.trace_events(state.current_run_id)
        self.assertEqual(sum(e["event_type"] == "TOOL_CALL_STARTED" for e in events), 1)
        self.assertEqual(sum(e["event_type"] == "TOOL_CALL_COMPLETED" for e in events), 1)
        self.assert_no_business_writes(runtime, state)

    def test_self_consistent_model_goal_and_evidence_are_not_independent_source(self):
        contract = TaskContract("consult", "new_task", "purchase_history")
        goal = {"interpretation": asdict(contract), "source": "user", "confirmed": True}
        state = self.runtime().new_session()
        self.assertEqual(contract.read_completion_issue(goal, {"purchase_history"}, accepted_state=state),
                         "READ_GOAL_SOURCE_UNVERIFIED")
        c, response = self.stage(state, "成交记录", proposed("consult", target="purchase_history"))
        self.choose(state, c, response, "start")
        self.assertIsNone(c.read_completion_issue(goal, {"purchase_history"}, accepted_state=state))
        # 无冲突只说明技术必要条件通过，回复仍明确不把目标满足标为True。
        self.assertIsNone(response["user_goal_satisfied"])

    def test_model_goal_cannot_replace_an_accepted_read_scope(self):
        state = self.runtime().new_session()
        accepted, response = self.stage(state, "所有申请的审批进度", proposed(
            "query", target="request", query_scope="collection"))
        self.choose(state, accepted, response, "start")
        wrong = TaskContract("consult", "new_task", "purchase_history")
        circular_goal = {"interpretation": asdict(wrong)}
        self.assertEqual(wrong.read_completion_issue(circular_goal, {"purchase_history"}, accepted_state=state),
                         "READ_GOAL_SOURCE_UNVERIFIED")
        wrong_scope = TaskContract("query", "follow_up", "request", "single")
        self.assertEqual(wrong_scope.read_completion_issue({"interpretation": asdict(wrong_scope)}, {"request"},
                                                         accepted_state=state), "READ_GOAL_SCOPE_CONFLICT")

    def test_stale_candidate_cannot_change_formal_state(self):
        for mutate in (lambda s: setattr(s, "current_run_id", "another-turn"),
                       lambda s: s.slots.update(quantity=99),
                       lambda s: setattr(s, "actor_identity", "U02"),
                       lambda s: s.clarification["candidate"]["proposal"].update(quantity=99),
                       lambda s: s.conversation_tail[-1].update(content="另一条请求")):
            state = self.runtime().new_session()
            c, response = self.stage(state, "办理键盘申请", proposed(item="键盘"))
            mutate(state)
            before = asdict(state)
            rejected = c.commit_candidate(state, response["candidate_id"], "start")
            self.assertEqual(rejected["route_reason_code"], "TASK_SOURCE_STALE")
            self.assertEqual(asdict(state), before)
            self.assertFalse(rejected["task_accepted"])

    def test_expired_and_replaced_candidate_are_not_reusable(self):
        state = self.runtime().new_session()
        c, response = self.stage(state, "办理键盘申请", proposed(item="键盘"))
        with patch("agent_runtime.time.time", return_value=10**12):
            self.assertEqual(c.commit_candidate(state, response["candidate_id"], "start")["route_reason_code"], "TASK_SOURCE_STALE")
        self.stage(state, "再澄清一次", proposed(item="鼠标"))
        self.assertEqual(c.commit_candidate(state, response["candidate_id"], "start")["route_reason_code"], "TASK_CANDIDATE_NOT_FOUND")

    def test_unknown_and_partial_result_guards_are_not_cleared(self):
        for field in ("uncertain_request_id", "retry_submit_request_id"):
            state = self.runtime().new_session()
            setattr(state, field, "EXISTING")
            state.completed_operations["already"] = {"outcome_status": "UNKNOWN"}
            c, response = self.stage(state, "办理键盘申请", proposed(item="键盘"))
            before = asdict(state)
            result = c.commit_candidate(state, response["candidate_id"], "start")
            self.assertEqual(result["route_reason_code"], "EXISTING_RUNTIME_CONTROL_REQUIRED")
            self.assertEqual(asdict(state), before)

    def test_old_task_without_origin_cannot_accept_supplement_or_switch(self):
        for operation in ("supplement", "switch"):
            state = self.runtime().new_session()
            state.active_task_type = "create"
            state.slots = {"item": "键盘"}
            contract, response = self.stage(state, "每个100元", proposed(unit_price="100"))
            before = asdict(state)
            rejected = contract.commit_candidate(state, response["candidate_id"], operation)
            self.assertEqual(rejected["route_reason_code"], "TASK_SOURCE_CHOICE_CONFLICT")
            self.assertEqual(asdict(state), before)

    def test_pending_business_confirmation_cannot_be_replaced_by_task_choice(self):
        from agent_runtime import PendingAction
        state = self.runtime().new_session()
        state.pending_action = PendingAction("old-action", "old-run", "U01", "create", "OLD", {},
                                             None, None, None, 0, 10**12, "bound", "原操作")
        contract, response = self.stage(state, "另一个需求", proposed(item="电脑"))
        before = asdict(state)
        rejected = contract.commit_candidate(state, response["candidate_id"], "start")
        self.assertEqual(rejected["route_reason_code"], "EXISTING_RUNTIME_CONTROL_REQUIRED")
        self.assertEqual(asdict(state), before)

    def test_unselected_or_rejected_switch_preserves_all_accepted_fields(self):
        state = self.runtime().new_session()
        self.start(state, quantity=1, unit_price="100", purpose="办公")
        before = asdict(state)
        contract, response = self.stage(state, "只补数量", proposed(relation="switch_task", item="电脑", quantity=3))
        rejected = self.choose(state, contract, response, "reject")
        for key in ("active_task_type", "slots", "task_origin", "target_request_id", "last_read_version"):
            self.assertEqual(asdict(state)[key], before[key])
        self.assertFalse(rejected["task_accepted"])

    def test_origin_from_finished_task_does_not_authorize_new_transition(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(state)
        runtime._finish_task(state)
        c, response = self.stage(state, "切换需求", proposed(relation="switch_task", item="电脑"))
        self.assertEqual(c.commit_candidate(state, response["candidate_id"], "switch")["route_reason_code"],
                         "TASK_SOURCE_CHOICE_CONFLICT")


if __name__ == "__main__":
    unittest.main()
