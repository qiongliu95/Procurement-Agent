"""业务一致性受控故障回归：脚本模型 + 真实临时 HTTP/SQLite，不使用演示数据库。"""
import json
import os
import threading
import unittest
import urllib.request
from pathlib import Path

import test_agent_runtime as helpers
from agent_runtime import AgentRuntime, ModelTurn
from agent_tools import ApiCallResult, ProcurementApiError, ProcurementTransportError
from agent_server import create_server


class FaultApi:
    """只在指定边界注入故障，其余调用交给真实后端。"""
    def __init__(self, real, fault=None):
        self.real = real
        self.fault = fault
        self.create_count = self.submit_count = self.update_count = 0
        self.after_write = False
        self.corrupt = None

    def __getattr__(self, name):
        return getattr(self.real, name)

    def get_request(self, **kwargs):
        if self.after_write and self.fault in {"submit_unknown", "update_read_lost", "create_read_lost"}:
            raise ProcurementTransportError("CONTROLLED_READ_UNAVAILABLE", uncertain=False)
        return self.real.get_request(**kwargs)

    def create_request(self, **kwargs):
        self.create_count += 1
        result = self.real.create_request(**kwargs)
        if self.fault == "create_read_lost":
            self.after_write = True
        return result

    def submit_request(self, **kwargs):
        self.submit_count += 1
        if self.fault == "submit_noop":
            return ApiCallResult({"state": "SUBMITTED"}, 200, kwargs["tool_call_id"], 0)
        if self.fault == "submit_rejected":
            raise ProcurementApiError(409, "CONTROLLED_SUBMIT_REJECTED")
        if self.fault == "submit_forbidden":
            raise ProcurementApiError(403, "NOT_REQUESTER")
        if self.fault == "submit_timeout_before_commit":
            self.after_write = True
            raise ProcurementTransportError("CONTROLLED_TIMEOUT", uncertain=True)
        result = self.real.submit_request(**kwargs)
        if self.fault == "submit_unknown":
            self.after_write = True
            raise ProcurementTransportError("CONTROLLED_RECEIPT_LOST", uncertain=True)
        if self.fault == "submit_500_after_commit":
            raise ProcurementApiError(500, "CONTROLLED_SERVER_ERROR")
        return result

    def update_request(self, **kwargs):
        self.update_count += 1
        result = self.real.update_request(**kwargs)
        if self.fault == "update_read_lost":
            self.after_write = True
        if self.corrupt:
            self.corrupt(kwargs["request_id"])
        return result


class BusinessConsistencyTests(unittest.TestCase):
    setUp = helpers.AgentRuntimeTests.setUp
    tearDown = helpers.AgentRuntimeTests.tearDown
    call = helpers.AgentRuntimeTests.call
    fixture = helpers.AgentRuntimeTests.fixture
    draft = helpers.AgentRuntimeTests.draft

    def runtime(self, turns, fault=None):
        api = FaultApi(self.api, fault)
        return AgentRuntime(helpers.ScriptedModel(turns), api, self.trace, model_name="controlled-fault"), api

    def create_pending(self, fault=None):
        runtime, api = self.runtime([
            ModelTurn(None, [helpers.tool("match_supplier", {"name": "启明办公"})]),
            ModelTurn(None, [helpers.tool("propose_task_state", {
                "intent": "create", "item": "显示器", "quantity": 2, "unit_price": "800.00",
                "supplier_name": "启明办公", "purpose": "一致性测试",
            }, 2)]),
        ], fault)
        state = runtime.new_session(self.id())
        pending = runtime.handle_message(state, "创建2台显示器申请，单价800元，供应商启明办公，用于一致性测试")
        return runtime, api, state, pending

    def submit_pending(self, fault=None):
        self.draft("CONS-DRAFT")
        runtime, api = self.runtime([ModelTurn(None, [helpers.tool("propose_task_state", {
            "intent": "submit", "request_id": "CONS-DRAFT",
        })])], fault)
        state = runtime.new_session(self.id())
        return runtime, api, state, runtime.handle_message(state, "提交 CONS-DRAFT")

    def evidence(self, runtime, api, state, response, rid, variant=""):
        with self.app.store.connect() as db:
            request = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            events = [dict(row) for row in db.execute("SELECT * FROM events WHERE request_id=? ORDER BY seq", (rid,))]
            count = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        trace = []
        for path in Path(self.tmp.name, "traces").glob("*.jsonl"):
            trace.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        data = {"case": self._testMethodName, "database_request": dict(request) if request else None,
                "request_count": count, "audit": events, "trace": trace, "response": response,
                "session": runtime.public_session(state), "retry_submit_request_id": state.retry_submit_request_id,
                "uncertain_request_id": state.uncertain_request_id,
                "calls": {"create": api.create_count, "submit": api.submit_count, "update": api.update_count}}
        target = os.getenv("STATE_CONSISTENCY_EVIDENCE_DIR")
        if target:
            directory = Path(target)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (self._testMethodName + "_" + rid + variant + ".json")).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return data

    def test_create_tool_submit_success_but_db_draft_is_partial(self):
        runtime, api, state, pending = self.create_pending("submit_noop")
        rid = state.pending_action.target_request_id
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        data = self.evidence(runtime, api, state, result, rid)
        self.assertEqual(data["database_request"]["state"], "DRAFT")
        self.assertEqual(data["request_count"], 1)
        self.assertEqual(result["outcome_status"], "PARTIAL_SUCCESS")
        self.assertEqual(result["route_reason_code"], "CREATE_SUBMIT_PARTIAL")
        self.assertIn("自动提交失败", result["message"])
        self.assertEqual(state.retry_submit_request_id, rid)
        self.assertIsNone(state.pending_action)

    def test_existing_submit_tool_success_but_db_draft_is_failed(self):
        runtime, api, state, pending = self.submit_pending("submit_noop")
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        data = self.evidence(runtime, api, state, result, "CONS-DRAFT")
        self.assertEqual(data["database_request"]["state"], "DRAFT")
        self.assertEqual(result["outcome_status"], "FAILED")
        self.assertNotEqual(result["route_reason_code"], "WRITE_VERIFIED")
        self.assertIsNone(state.active_task_type)

    def test_created_draft_retry_reads_latest_and_requires_new_confirmation(self):
        runtime, api, state, pending = self.create_pending("submit_rejected")
        rid = state.pending_action.target_request_id
        partial = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(partial["outcome_status"], "PARTIAL_SUCCESS")
        self.assertIn(rid, partial["message"])
        self.assertIn("DRAFT", partial["message"])
        self.assertIn("CONTROLLED_SUBMIT_REJECTED", partial["message"])
        self.assertIn("是否重新发起提交", partial["message"])
        self.call("PATCH", f"/requests/{rid}/update", data={"purpose": "外部补充"})
        api.fault = None
        retry = runtime.handle_message(state, "重新提交")
        self.assertEqual(retry["route_action"], "CONFIRM")
        self.assertEqual(state.pending_action.target_request_id, rid)
        self.assertEqual(state.pending_action.expected_version, 2)
        self.assertEqual(api.submit_count, 1)
        done = runtime.confirm(state, retry["confirmation"]["action_id"], "confirm")
        data = self.evidence(runtime, api, state, done, rid)
        self.assertEqual(data["request_count"], 1)
        self.assertEqual(data["database_request"]["state"], "SUBMITTED")
        self.assertEqual(api.create_count, 1)
        self.assertEqual(done["outcome_status"], "SUCCESS")
        self.assertIsNone(state.active_task_type)
        self.assertIsNone(state.retry_submit_request_id)

    def test_created_draft_decline_keeps_request_and_finishes_task(self):
        runtime, api, state, pending = self.create_pending("submit_rejected")
        rid = state.pending_action.target_request_id
        runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        declined = runtime.handle_message(state, "暂不提交")
        data = self.evidence(runtime, api, state, declined, rid)
        self.assertEqual(data["database_request"]["state"], "DRAFT")
        self.assertEqual(api.create_count, 1)
        self.assertEqual(api.submit_count, 1)
        self.assertIsNone(state.active_task_type)
        self.assertIsNone(state.pending_action)
        self.assertIsNone(state.retry_submit_request_id)
        self.assertIn("已保留申请", declined["message"])

    def test_submit_unknown_never_rewrites_and_replay_reads_latest(self):
        runtime, api, state, pending = self.submit_pending("submit_unknown")
        action_id = pending["confirmation"]["action_id"]
        unknown = runtime.confirm(state, action_id, "confirm")
        self.assertEqual(unknown["outcome_status"], "UNKNOWN")
        self.assertIn("执行结果未知", unknown["message"])
        self.assertIsNone(state.pending_action)
        self.assertIsNone(state.active_task_type)
        self.assertNotIn("CONS-DRAFT", state.known_requests)
        repeated = runtime.confirm(state, action_id, "confirm")
        self.assertEqual(repeated["outcome_status"], "UNKNOWN")
        self.assertEqual(api.submit_count, 1)
        api.fault = None
        verified = runtime.confirm(state, action_id, "confirm")
        self.assertEqual(verified["route_reason_code"], "READ_VERIFIED")
        self.assertEqual(verified["result"]["state"], "SUBMITTED")
        self.assertEqual(api.submit_count, 1)
        self.assertIsNone(state.uncertain_request_id)
        self.assertEqual(state.known_requests["CONS-DRAFT"]["state"], "SUBMITTED")
        data = self.evidence(runtime, api, state, verified, "CONS-DRAFT")
        self.assertEqual(data["database_request"]["state"], "SUBMITTED")

    def test_update_committed_but_read_lost_is_unknown(self):
        self.fixture("CONS-UPDATE")
        runtime, api = self.runtime([ModelTurn(None, [helpers.tool("propose_task_state", {
            "intent": "update", "request_id": "CONS-UPDATE", "quantity": 3,
        })])], "update_read_lost")
        state = runtime.new_session(self.id())
        pending = runtime.handle_message(state, "修改 CONS-UPDATE 数量为3")
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        data = self.evidence(runtime, api, state, result, "CONS-UPDATE")
        self.assertEqual((data["database_request"]["quantity"], data["database_request"]["version"]), (3, 2))
        self.assertEqual(result["outcome_status"], "UNKNOWN")
        self.assertIsNone(state.pending_action)
        self.assertIsNone(state.active_task_type)
        self.assertNotIn("CONS-UPDATE", state.known_requests)
        runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(api.update_count, 1)

    def test_update_invariants_approval_evidence_amount_and_untouched_fields(self):
        self.fixture("CONS-INVARIANTS")
        self.assertEqual(self.call("POST", "/requests/CONS-INVARIANTS/approve", "demo-m01", {})[0], 200)
        self.assertEqual(self.call("POST", "/requests/CONS-INVARIANTS/evidence", "demo-m01", {})[0], 200)
        before = self.call("GET", "/requests/CONS-INVARIANTS")[1]
        self.assertTrue(before["approval"]["approval_valid"])
        self.assertTrue(before["approval"]["evidence_verified"])
        runtime, api = self.runtime([ModelTurn(None, [helpers.tool("propose_task_state", {
            "intent": "update", "request_id": "CONS-INVARIANTS", "quantity": 4,
        })])])
        state = runtime.new_session(self.id())
        pending = runtime.handle_message(state, "把 CONS-INVARIANTS 数量改为4")
        done = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        data = self.evidence(runtime, api, state, done, "CONS-INVARIANTS")
        row = data["database_request"]
        self.assertEqual((row["state"], row["version"], row["amount"]), ("REAPPROVAL_REQUIRED", 2, 320000))
        self.assertIsNone(row["approved_by"])
        self.assertIsNone(row["approved_version"])
        self.assertEqual(row["evidence_verified"], 0)
        self.assertEqual(row["purpose"], "办公使用")
        self.assertEqual(done["outcome_status"], "SUCCESS")
        self.assertIsNone(state.active_task_type)

    def test_update_corrupted_invariants_are_not_verified(self):
        corruptions = {
            "version": ("version", 1), "amount": ("amount", 1), "state": ("state", "SUBMITTED"),
            "approval": ("approved_by", "M01"), "evidence": ("evidence_verified", 1),
            "untouched": ("purpose", "错误覆盖"),
        }
        for label, (column, value) in corruptions.items():
            with self.subTest(invariant=label):
                rid = "CONS-CORRUPT-" + label.upper()
                self.fixture(rid)
                runtime, api = self.runtime([ModelTurn(None, [helpers.tool("propose_task_state", {
                    "intent": "update", "request_id": rid, "quantity": 3,
                })])])
                def corrupt(request_id, column=column, value=value):
                    # 独立测试数据库中模拟写后不变量损坏；列名仅来自上述固定表。
                    with self.app.store.connect() as db:
                        db.execute(f"UPDATE requests SET {column}=? WHERE id=?", (value, request_id))
                api.corrupt = corrupt
                state = runtime.new_session(self.id() + label)
                pending = runtime.handle_message(state, f"修改 {rid} 数量为3")
                result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
                self.assertEqual(result["outcome_status"], "UNKNOWN")
                self.assertEqual(result["route_reason_code"], "UPDATE_SNAPSHOT_MISMATCH")
                self.assertIsNone(state.pending_action)
                self.evidence(runtime, api, state, result, rid)

    def test_budget_and_vendor_rule_results_are_blocked_not_success_or_retry(self):
        for supplier, price, expected in (
            ("启明办公", "3500.00", "BLOCKED_BUDGET"),
            ("风险供应商", "100.00", "BLOCKED_VENDOR"),
            ("新锐科技", "100.00", "PENDING_VENDOR_REVIEW"),
        ):
            with self.subTest(expected=expected):
                rid = "CONS-RULE-" + expected
                self.assertEqual(self.call("POST", "/requests", data={
                    "request_id": rid, "item": "显示器", "quantity": 2, "unit_price": price,
                    "supplier_name": supplier, "purpose": "业务阻断",
                })[0], 201)
                runtime, api = self.runtime([ModelTurn(None, [helpers.tool("propose_task_state", {
                    "intent": "submit", "request_id": rid,
                })])])
                state = runtime.new_session(self.id() + expected)
                pending = runtime.handle_message(state, f"提交 {rid}")
                result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
                self.assertEqual(result["result"]["state"], expected)
                self.assertEqual(result["outcome_status"], "BLOCKED")
                self.assertIsNone(state.retry_submit_request_id)
                self.assertIsNone(state.active_task_type)
                self.evidence(runtime, api, state, result, rid)

    def test_explicit_new_id_overrides_conflicting_model_old_for_update_and_submit(self):
        self.draft("CONS-OLD")
        self.draft("CONS-NEW")
        for intent in ("update", "submit"):
            with self.subTest(intent=intent):
                proposal = {"intent": intent, "request_id": "CONS-OLD"}
                if intent == "update":
                    proposal["quantity"] = 3
                runtime, api = self.runtime([ModelTurn(None, [helpers.tool("propose_task_state", proposal)])])
                state = runtime.new_session(self.id() + intent)
                state.target_request_id = "CONS-OLD"
                response = runtime.handle_message(state, f"{intent} 申请 CONS-NEW" + ("，数量3" if intent == "update" else ""))
                self.assertEqual(response["route_action"], "CONFIRM")
                self.assertEqual(state.pending_action.target_request_id, "CONS-NEW")
                self.assertNotIn("CONS-OLD", response["message"])
                data = self.evidence(runtime, api, state, response, "CONS-NEW", intent + "-pending")
                self.assertEqual(data["database_request"]["state"], "DRAFT")
                self.assertEqual(api.update_count + api.submit_count, 0)
                done = runtime.confirm(state, response["confirmation"]["action_id"], "confirm")
                data = self.evidence(runtime, api, state, done, "CONS-NEW", intent)
                self.assertEqual(data["database_request"]["quantity"], 3)
                self.assertEqual(self.call("GET", "/requests/CONS-OLD")[1]["state"], "DRAFT")
                self.assertEqual(self.call("GET", "/requests/CONS-OLD")[1]["quantity"], 2)
                self.assertEqual(done["result"]["request_id"], "CONS-NEW")

    def test_repeated_confirmation_returns_fresh_state_without_rewriting(self):
        runtime, api, state, pending = self.create_pending()
        rid = state.pending_action.target_request_id
        action_id = pending["confirmation"]["action_id"]
        first = runtime.confirm(state, action_id, "confirm")
        self.call("PATCH", f"/requests/{rid}/update", data={"quantity": 3})
        state.active_task_type = "create"
        state.slots = {"item": "另一任务"}
        replay = runtime.confirm(state, action_id, "confirm")
        self.assertEqual(replay["result"]["version"], 2)
        self.assertEqual(replay["result"]["state"], "REAPPROVAL_REQUIRED")
        self.assertEqual(first["result"]["version"], 1)
        self.assertEqual((api.create_count, api.submit_count), (1, 1))
        self.assertEqual(state.slots, {"item": "另一任务"})
        self.assertEqual(state.known_requests[rid]["version"], 2)
        self.evidence(runtime, api, state, replay, rid)

    def test_create_read_lost_keeps_original_id_and_blocks_duplicate_create(self):
        runtime, api, state, pending = self.create_pending("create_read_lost")
        rid = state.pending_action.target_request_id
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(result["outcome_status"], "UNKNOWN")
        blocked = runtime._prepare_create(state, {})
        self.assertEqual(blocked["route_reason_code"], "UNKNOWN_REQUIRES_QUERY")
        data = self.evidence(runtime, api, state, blocked, rid)
        self.assertEqual(data["request_count"], 1)
        self.assertEqual(data["database_request"]["state"], "DRAFT")
        self.assertEqual(api.create_count, 1)

    def test_submit_timeout_without_commit_is_unknown_and_not_auto_retried(self):
        runtime, api, state, pending = self.submit_pending("submit_timeout_before_commit")
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(result["outcome_status"], "UNKNOWN")
        self.assertEqual(api.submit_count, 1)
        self.assertIsNone(state.pending_action)
        data = self.evidence(runtime, api, state, result, "CONS-DRAFT")
        self.assertEqual(data["database_request"]["state"], "DRAFT")

    def test_server_error_after_submit_is_resolved_by_actual_state(self):
        runtime, api, state, pending = self.submit_pending("submit_500_after_commit")
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(result["outcome_status"], "SUCCESS")
        self.assertEqual(api.submit_count, 1)
        data = self.evidence(runtime, api, state, result, "CONS-DRAFT")
        self.assertEqual(data["database_request"]["state"], "SUBMITTED")

    def test_http_partial_and_unknown_are_structured_results_not_internal_errors(self):
        for fault, outcome in (("submit_rejected", "PARTIAL_SUCCESS"), ("create_read_lost", "UNKNOWN")):
            with self.subTest(fault=fault):
                runtime, api, state, pending = self.create_pending(fault)
                rid = state.pending_action.target_request_id
                server = create_server(runtime, 0)
                server.session_store.sessions[state.session_id] = (state, threading.RLock())
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/confirm", method="POST",
                        headers={"Content-Type": "application/json",
                                 "Cookie": "procurement_agent_session=" + state.session_id},
                        data=json.dumps({"action_id": pending["confirmation"]["action_id"], "decision": "confirm"}).encode(),
                    )
                    with urllib.request.urlopen(request, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        result = json.load(response)
                    self.assertEqual(result["outcome_status"], outcome)
                    self.assertEqual(result["result"]["request_id"], rid)
                    self.assertIsNone(runtime.public_session(state)["pending_confirmation"])
                    data = self.evidence(runtime, api, state, result, rid)
                    self.assertEqual(data["database_request"]["state"], "DRAFT")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

    def test_create_succeeded_but_permission_denied_stops_without_retry(self):
        runtime, api, state, pending = self.create_pending("submit_forbidden")
        rid = state.pending_action.target_request_id
        result = runtime.confirm(state, pending["confirmation"]["action_id"], "confirm")
        self.assertEqual(result["outcome_status"], "PARTIAL_SUCCESS")
        self.assertEqual(result["route_action"], "STOP")
        self.assertIn("NOT_REQUESTER", result["message"])
        self.assertIsNone(state.retry_submit_request_id)
        self.assertIsNone(state.active_task_type)
        data = self.evidence(runtime, api, state, result, rid)
        self.assertEqual(data["database_request"]["state"], "DRAFT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
