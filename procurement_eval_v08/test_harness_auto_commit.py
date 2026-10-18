"""TASK-24隔离验证；首项是安全阻断测试，失败时必须停止后续验收。"""
import copy
import unittest
import uuid
from dataclasses import asdict

import test_context_management as helpers
from agent_runtime import AgentRuntime, PendingAction, TaskContract


def proposal(intent="create", relation="new_task", target="current_task", **fields):
    return {"intent": intent, "dialogue_relation": relation, "target_object": target, **fields}


class AutoCommitBoundaryTests(unittest.TestCase):
    setUp = helpers.ContextManagementTests.setUp
    tearDown = helpers.ContextManagementTests.tearDown
    trace_events = helpers.ContextManagementTests.trace_events

    def runtime(self):
        return AgentRuntime(helpers.CapturingModel([]), self.api, self.trace, model_name="offline-auto-commit")

    def database(self):
        # 只读快照覆盖业务写副作用；不把“没有确认卡”当成“没有写入”。
        tables = ("budgets", "requests", "approvals", "vendor_reviews", "pos", "idem", "events")
        with self.app.store.connect() as db:
            return {t: [tuple(row) for row in db.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                    for t in tables}

    def turn(self, state, text):
        state.current_run_id = "auto-" + uuid.uuid4().hex
        state.conversation_tail.append({"role": "user", "content": text})

    def apply(self, runtime, state, text, candidate, *, read=None):
        db_before = self.database()
        self.turn(state, text)
        if read:
            name, args = read
            result = TaskContract.read_attempt(runtime, state, helpers.tool(name, args), text)
            self.assertNotIn("error", result)
        before = asdict(state)
        contract = TaskContract.from_proposal(candidate)
        staged = contract.stage_candidate(state, candidate)
        after_stage = asdict(state)
        self.assertEqual({k: v for k, v in before.items() if k != "clarification"},
                         {k: v for k, v in after_stage.items() if k != "clarification"})
        contract.validate_auto_candidate(state, staged["candidate_id"])
        self.assertEqual(asdict(state), after_stage, "Validate必须无状态副作用")
        response = contract.auto_commit_candidate(state, staged["candidate_id"])
        self.assertTrue(response["message"])
        self.assertIsNone(response["confirmation"])
        self.assertFalse(response["business_write_authorized"])
        self.assertIsNone(response["user_goal_satisfied"])
        self.assertEqual(self.database(), db_before)
        events = self.trace_events(state.current_run_id)
        executed = [e["tool_name"] for e in events if e["event_type"] == "TOOL_CALL_STARTED"]
        self.assertEqual(executed, [read[0]] if read else [])
        self.assertEqual(runtime.model.received, [])
        if not response["task_accepted"]:
            self.assertEqual({k: v for k, v in before.items() if k != "clarification"},
                             {k: v for k, v in asdict(state).items() if k != "clarification"})
        return response

    def start(self, runtime, state):
        text = "请建一张键盘申请，数量2，单价100，用途办公，供应商启明办公"
        response = self.apply(runtime, state, text, proposal(
            item="键盘", quantity=2, unit_price="100", purpose="办公", supplier_name="启明办公",
            request_quote=text, field_sources={"item": "键盘", "quantity": "2", "unit_price": "100",
                                               "purpose": "办公", "supplier_name": "启明办公"}))
        self.assertTrue(response["task_accepted"])
        self.assertFalse(response["requires_task_choice"])

    def test_00_query_without_tool_wrong_create_must_not_commit(self):
        # 真实模型可能先提案而不查工具；不能只测“已有咨询工具冲突”的容易样本。
        for text in ("查询键盘历史成交价", "我只想了解键盘过去卖多少钱"):
            with self.subTest(text=text):
                runtime = self.runtime()
                state = runtime.new_session()
                response = self.apply(runtime, state, text, proposal(
                    item="键盘", request_quote=text, field_sources={"item": "键盘"}))
                observed = {"reply": response, "active_task_type": state.active_task_type,
                            "slots": state.slots, "origin": state.task_origin,
                            "pending_action": state.pending_action,
                            "actual_tools": [], "business_writes": 0}
                self.assertFalse(response["task_accepted"], str(observed))
                self.assertIsNone(state.active_task_type)
                self.assertEqual(state.slots, {})

    def test_01_history_read_wrong_create_is_blocked(self):
        for text in ("查询键盘历史成交价", "键盘以前采购价格是多少？"):
            runtime = self.runtime()
            state = runtime.new_session()
            response = self.apply(runtime, state, text, proposal(
                item="键盘", request_quote=text, field_sources={"item": "键盘"}),
                read=("search_purchase_history", {"item": "键盘"}))
            self.assertEqual(response["route_reason_code"], "READ_WRITE_GOAL_CONFLICT")
            self.assertIsNone(state.active_task_type)

    def test_02_policy_wrong_update_keeps_current_task(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(runtime, state)
        before = copy.deepcopy(state.slots)
        response = self.apply(runtime, state, "采购制度是什么？", proposal(
            "update", "new_task", "request", request_id="A001", request_quote="采购制度是什么？"),
            read=("get_procurement_rules", {}))
        self.assertEqual(response["route_reason_code"], "READ_WRITE_GOAL_CONFLICT")
        self.assertEqual(state.slots, before)

    def test_03_explicit_create_without_extra_choice(self):
        for text, item, quote in (("请办理键盘申请", "键盘", "键盘"),
                                  ("帮我开一张PC的采购申请", "电脑", "PC")):
            runtime = self.runtime()
            state = runtime.new_session()
            response = self.apply(runtime, state, text, proposal(
                item=item, request_quote=text, field_sources={"item": quote}))
            self.assertTrue(response["task_accepted"])
            self.assertFalse(response["requires_task_choice"])
            self.assertEqual(state.slots, {"item": item})
            self.assertEqual(state.task_origin["kind"], "validated_model_state")
            self.assertFalse(state.task_origin["independent_intent_verified"])

    def test_04_supplements_automatically_merge(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(runtime, state)
        for text, fields, sources in (("数量填3", {"quantity": 3}, {"quantity": "3"}),
                                      ("每件120", {"unit_price": "120"}, {"unit_price": "120"}),
                                      ("用途研发", {"purpose": "研发"}, {"purpose": "研发"}),
                                      ("4个，每个99", {"quantity": 4, "unit_price": "99"},
                                       {"quantity": "4", "unit_price": "99"})):
            before = dict(state.slots)
            response = self.apply(runtime, state, text, proposal(
                relation="supplement", **fields, field_sources=sources))
            self.assertTrue(response["task_accepted"])
            self.assertFalse(response["requires_task_choice"])
            self.assertEqual(state.slots, {**before, **fields})

    def test_05_consult_then_continue_preserves_task(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(runtime, state)
        response = self.apply(runtime, state, "顺便问采购制度是什么？", proposal(
            "consult", "pause", "knowledge"), read=("get_procurement_rules", {}))
        self.assertEqual(response["route_action"], "READ")
        self.assertFalse(response["requires_task_choice"])
        self.assertEqual(state.active_task_type, "create")
        self.assertIsNone(state.suspended_task)
        response = self.apply(runtime, state, "原来的数量填3", proposal(
            relation="supplement", quantity=3, field_sources={"quantity": "3"}))
        self.assertTrue(response["task_accepted"])
        self.assertEqual(state.slots["quantity"], 3)

    def test_06_switch_isolates_old_fields_without_synonym_table(self):
        for text, canonical, quote in (("另办计算机申请2台", "电脑", "计算机"),
                                       ("换一笔PC申请2台", "办公电脑", "PC")):
            runtime = self.runtime()
            state = runtime.new_session()
            self.start(runtime, state)
            response = self.apply(runtime, state, text, proposal(
                relation="switch_task", item=canonical, quantity=2, unit_price="100", purpose="办公",
                supplier_name="启明办公", request_quote=text,
                field_sources={"item": quote, "quantity": "2"}))
            self.assertTrue(response["task_accepted"])
            self.assertEqual(state.slots, {"item": canonical, "quantity": 2})

    def test_07_wrong_switch_label_cannot_clear_task(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(runtime, state)
        response = self.apply(runtime, state, "每个120元", proposal(
            relation="switch_task", unit_price="120", request_quote="每个120元",
            field_sources={"unit_price": "120"}))
        self.assertEqual(response["route_reason_code"], "TASK_SWITCH_OBJECT_REQUIRED")

    def test_08_unknown_supplier_keeps_task_and_original_clarification(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.start(runtime, state)
        self.apply(runtime, state, "供应商不存在供应商", proposal(
            relation="supplement", supplier_name="不存在供应商", field_sources={"supplier_name": "不存在供应商"}))
        db_before = self.database()
        result = TaskContract.read_attempt(runtime, state, helpers.tool(
            "match_supplier", {"name": "不存在供应商"}), "供应商不存在供应商")
        self.assertEqual(result["matches"], [])
        before = dict(state.slots)
        response = runtime._prepare_create(state, {})
        self.assertEqual(response["route_action"], "ASK")
        self.assertEqual(response["route_reason_code"], "SUPPLIER_NOT_UNIQUELY_VERIFIED")
        self.assertIn("完整供应商名称", response["message"])
        self.assertEqual(state.slots, before)
        self.assertEqual(state.active_task_type, "create")
        self.assertIsNone(state.pending_action)
        self.assertEqual(self.database(), db_before)
        self.assertEqual([e["tool_name"] for e in self.trace_events(state.current_run_id)
                          if e["event_type"] == "TOOL_CALL_STARTED"], ["match_supplier"])

    def test_09_followup_and_correction_do_not_authorize_writes(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.apply(runtime, state, "键盘历史成交价是多少？", proposal(
            "consult", "new_task", "purchase_history"), read=("search_purchase_history", {"item": "键盘"}))
        for text, relation in (("那次供应商呢？", "follow_up"), ("不是问价格，是问数量", "correction")):
            response = self.apply(runtime, state, text, proposal("consult", relation, "purchase_history"))
            self.assertEqual(response["route_action"], "READ")
            wrong = self.apply(runtime, state, text, proposal(
                relation=relation, request_quote=text, quantity=3, field_sources={"quantity": "3"}))
            self.assertFalse(wrong["task_accepted"])
            self.assertIsNone(state.active_task_type)

    def test_10_pending_unknown_and_partial_result_are_untouched(self):
        for guard in ("pending_action", "uncertain_request_id", "retry_submit_request_id"):
            runtime = self.runtime()
            state = runtime.new_session()
            self.start(runtime, state)
            if guard == "pending_action":
                state.pending_action = PendingAction("bound", "old-run", "U01", "create", "OLD", {},
                                                     None, None, None, 0, 10**12, "fingerprint", "原确认")
                state.confirmation_state = "PENDING"
            else:
                setattr(state, guard, "OLD")
            state.completed_operations["old-operation"] = {"outcome_status": "UNKNOWN"}
            response = self.apply(runtime, state, "数量3", proposal(
                relation="supplement", quantity=3, field_sources={"quantity": "3"}))
            self.assertEqual(response["route_reason_code"], "EXISTING_RUNTIME_CONTROL_REQUIRED")

    def test_11_rejected_tool_has_no_actual_execution(self):
        runtime = self.runtime()
        state = runtime.new_session()
        self.turn(state, "补充数量")
        before = asdict(state)
        db_before = self.database()
        response = TaskContract.read_attempt(runtime, state, helpers.tool(
            "get_request", {"request_id": "GHOST"}), "补充数量")
        self.assertIn("error", response)
        events = self.trace_events(state.current_run_id)
        self.assertEqual([e["event_type"] for e in events], ["MODEL_TOOL_ATTEMPTED", "MODEL_TOOL_REJECTED"])
        self.assertTrue(all("tool_name" not in e for e in events))
        self.assertEqual(asdict(state), before)
        self.assertEqual(self.database(), db_before)


if __name__ == "__main__":
    unittest.main()
