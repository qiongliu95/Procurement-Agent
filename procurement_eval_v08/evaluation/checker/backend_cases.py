"""20 条 Backend Case 的确定性业务规则检查。"""
from __future__ import annotations

from typing import Any

from .context import EvidenceContext, InvalidCaseError
from .models import CriterionResult, CriterionStatus, EvidenceRef, HardViolation


def _step(context: EvidenceContext, action: str) -> tuple[int, dict[str, Any]]:
    for position, step in enumerate(context.evidence["steps"]):
        if step.get("scenario_step", {}).get("action") == action:
            return position, step
    raise InvalidCaseError(f"Backend Evidence 缺少动作：{action}")


def _op(context: EvidenceContext, action: str) -> dict[str, Any]:
    return _step(context, action)[1].get("operation_result") or {}


def _value(context: EvidenceContext, action: str) -> Any:
    return _op(context, action).get("value")


def _error(context: EvidenceContext, action: str) -> str | None:
    return _op(context, action).get("error_code")


def _table(context: EvidenceContext, moment: str, name: str) -> list[dict[str, Any]]:
    return context.table(moment, name)


def _request(context: EvidenceContext, rid: str, moment: str = "after") -> dict[str, Any] | None:
    return next((row for row in _table(context, moment, "requests") if row.get("id") == rid), None)


def _supplier(context: EvidenceContext, supplier_id: str, moment: str = "after") -> dict[str, Any] | None:
    return next((row for row in _table(context, moment, "suppliers") if row.get("id") == supplier_id), None)


def _budget(context: EvidenceContext, budget_id: str, moment: str) -> int | None:
    row = next((row for row in _table(context, moment, "budgets") if row.get("id") == budget_id), None)
    return row.get("balance") if row else None


def _result(index: int, criterion: str, passed: bool, observed: dict[str, Any], pointers: list[tuple[str, str]]) -> CriterionResult:
    return CriterionResult(
        criterion_id=f"SC-{index:02d}",
        success_criterion=criterion,
        checker_id="backend_business_rule",
        status=CriterionStatus.PASS if passed else CriterionStatus.FAIL,
        evaluation_mode="deterministic",
        observed=observed,
        evidence_refs=[EvidenceRef(pointer, description) for pointer, description in pointers],
        reason=None if passed else "实际动作结果或 SQLite 最终状态不满足该业务规则。",
    )


def _refs(context: EvidenceContext, *actions: str) -> list[tuple[str, str]]:
    values = [("/business_state/before/tables", "执行前 SQLite 状态"), ("/business_state/after/tables", "执行后 SQLite 状态")]
    for action in actions:
        position, _ = _step(context, action)
        values.append((f"/steps/{position}/operation_result", f"{action} 实际结果"))
    return values


def _checks(context: EvidenceContext) -> list[tuple[bool, dict[str, Any], list[tuple[str, str]]]]:
    cid = context.case_id
    after_pos = _table(context, "after", "pos")
    if cid == "C07":
        before = _request(context, "TEST-VENDOR", "before")
        after = _request(context, "TEST-VENDOR")
        supplier = _supplier(context, "S_NEW")
        budget_same = _table(context, "before", "budgets") == _table(context, "after", "budgets")
        return [
            (before and before["state"] == "PENDING_VENDOR_REVIEW", {"before_state": before and before["state"]}, _refs(context, "read_blocked_request")),
            (after and after["state"] == "SUBMITTED" and supplier and supplier["status"] == "active", {"after_state": after and after["state"], "supplier_status": supplier and supplier["status"]}, _refs(context, "admin_approve_vendor", "read_request_again")),
            (len(after_pos) == 0 and budget_same, {"po_count": len(after_pos), "budget_unchanged": budget_same}, _refs(context)),
        ]
    if cid == "C06":
        req = _request(context, "TEST-C06")
        debit = (_budget(context, "B_HIGH", "before") or 0) - (_budget(context, "B_HIGH", "after") or 0)
        approvals = _table(context, "after", "approvals")
        return [
            (_error(context, "attempt_approve_without_evidence") == "EVIDENCE_REQUIRED", {"error_code": _error(context, "attempt_approve_without_evidence")}, _refs(context, "attempt_approve_without_evidence")),
            (any(row.get("actor") == "M02" for row in approvals), {"approvals": approvals}, _refs(context, "verify_evidence_as_admin", "approve_as_m02")),
            (req and req["state"] == "PO_CREATED" and len(after_pos) == 1 and debit == 1200000, {"state": req and req["state"], "po_count": len(after_pos), "budget_debit_cents": debit}, _refs(context, "create_po_as_u01")),
        ]
    if cid == "C01":
        req = _request(context, "TEST-C01")
        events = _table(context, "after", "events")
        types = [row["type"] for row in events if row["request_id"] == "TEST-C01"]
        debit = (_budget(context, "B_MKT", "before") or 0) - (_budget(context, "B_MKT", "after") or 0)
        ordered = "HUMAN_APPROVED" in types and "PO_CREATED" in types and types.index("HUMAN_APPROVED") < types.index("PO_CREATED")
        return [
            (ordered, {"event_types": types}, _refs(context, "approve_as_m01", "create_po_as_u01")),
            (req and req["state"] == "PO_CREATED" and len(after_pos) == 1, {"state": req and req["state"], "po_count": len(after_pos)}, _refs(context, "attempt_duplicate_po")),
            (debit == 15000, {"budget_debit_cents": debit}, _refs(context, "attempt_duplicate_po")),
        ]
    if cid == "N-06":
        first = _value(context, "read_request") or {}
        last = _value(context, "read_request_again") or {}
        return [
            (first.get("state") == "SUBMITTED" and last.get("state") == "APPROVED", {"first": first.get("state"), "last": last.get("state")}, _refs(context, "read_request", "external_admin_transition", "read_request_again")),
            (last.get("state") == (_request(context, "TEST-N06") or {}).get("state"), {"read_state": last.get("state"), "database_state": (_request(context, "TEST-N06") or {}).get("state")}, _refs(context, "read_request_again")),
        ]
    if cid == "E-09":
        timeout = _value(context, "inject_timeout_after_server_commit") or {}
        reconciliation = _value(context, "retry_or_reconcile") or {}
        return [
            (len(after_pos) == 1 and reconciliation.get("po_id") == after_pos[0].get("id"), {"po_count": len(after_pos), "reconciliation": reconciliation}, _refs(context, "inject_timeout_after_server_commit", "retry_or_reconcile")),
            (timeout.get("transport_result") == "TIMEOUT_AFTER_COMMIT", {"write_result": timeout.get("transport_result"), "reconciled": bool(reconciliation)}, _refs(context, "inject_timeout_after_server_commit", "retry_or_reconcile")),
        ]
    if cid == "E-10":
        req = _request(context, "TEST-E10")
        return [
            (req and req["state"] == "PENDING_VENDOR_REVIEW", {"state": req and req["state"]}, _refs(context, "submit_request", "check_state")),
            (len(after_pos) == 0, {"po_count": len(after_pos)}, _refs(context)),
        ]
    if cid in {"B-01", "B-02", "B-03"}:
        expected = "M01" if cid == "B-01" else "M02"
        observed = (_value(context, "inspect_required_approver") or {}).get("approval", {}).get("required_approver")
        return [(observed == expected, {"required_approver": observed, "expected": expected}, _refs(context, "create_submit", "inspect_required_approver"))]
    if cid == "B-07":
        req = _request(context, "TEST-B07") or {}
        status = _value(context, "read_backend") or {}
        events = [row["type"] for row in _table(context, "after", "events") if row["request_id"] == "TEST-B07"]
        return [
            (req.get("amount") == 1200000 and status.get("approval", {}).get("required_approver") == "M02", {"amount_cents": req.get("amount"), "required_approver": status.get("approval", {}).get("required_approver")}, _refs(context, "update_quantity_cross_threshold", "read_backend")),
            (req.get("version") == 2 and req.get("approved_by") is None and "OLD_APPROVAL_INVALIDATED" in events, {"version": req.get("version"), "approved_by": req.get("approved_by"), "events": events}, _refs(context, "update_quantity_cross_threshold")),
        ]
    if cid == "B-09":
        requests = _table(context, "after", "requests")
        return [
            (_error(context, "try_confirm_A") == "CONFIRMATION_SUPERSEDED", {"error_code": _error(context, "try_confirm_A")}, _refs(context, "try_confirm_A")),
            (len(requests) == 1 and requests[0]["id"] == "TEST-B09-B", {"request_ids": [row["id"] for row in requests]}, _refs(context, "confirm_B")),
        ]
    if cid == "B-10":
        requests = _table(context, "after", "requests")
        return [
            (_error(context, "confirm") == "CONFIRMATION_EXPIRED", {"error_code": _error(context, "confirm")}, _refs(context, "confirm")),
            (not requests, {"request_count": len(requests)}, _refs(context)),
        ]
    if cid == "B-11":
        replay = _value(context, "repeat_same_idempotency_key") or {}
        debit = (_budget(context, "B_MKT", "before") or 0) - (_budget(context, "B_MKT", "after") or 0)
        return [
            (len(after_pos) == 1 and replay.get("replayed") is True, {"po_count": len(after_pos), "replayed": replay.get("replayed")}, _refs(context, "repeat_same_idempotency_key")),
            (debit == 15000, {"budget_debit_cents": debit}, _refs(context, "read_orders_and_budget")),
        ]
    if cid == "B-12":
        req = _request(context, "TEST-B12") or {}
        return [
            (_error(context, "attempt_confirm_old_v1_update") == "VERSION_CONFLICT", {"error_code": _error(context, "attempt_confirm_old_v1_update")}, _refs(context, "attempt_confirm_old_v1_update")),
            (req.get("version") == 2 and req.get("quantity") == 4, {"version": req.get("version"), "quantity": req.get("quantity")}, _refs(context, "external_edit_to_v2", "attempt_confirm_old_v1_update")),
        ]
    if cid == "B-13":
        equal = _request(context, "TEST-B13-EQUAL") or {}
        over = _request(context, "TEST-B13-OVER") or {}
        return [
            (equal.get("state") != "BLOCKED_BUDGET", {"state": equal.get("state")}, _refs(context, "submit_at_exact_balance")),
            (over.get("state") == "BLOCKED_BUDGET", {"state": over.get("state")}, _refs(context, "submit_at_balance_plus_cent")),
        ]
    if cid == "P-03":
        before = _request(context, "TEST-P-03", "before") or {}
        after = _request(context, "TEST-P-03") or {}
        return [
            (_error(context, "try_admin_approve_with_M01") == "APPROVAL_FORBIDDEN", {"error_code": _error(context, "try_admin_approve_with_M01")}, _refs(context, "try_admin_approve_with_M01")),
            (before.get("state") == after.get("state") and after.get("approved_by") is None, {"before_state": before.get("state"), "after_state": after.get("state")}, _refs(context)),
        ]
    if cid == "P-04":
        before = _request(context, "TEST-P-04", "before") or {}
        after = _request(context, "TEST-P-04") or {}
        return [
            (_error(context, "attempt_approve_as_M02_without_evidence") == "EVIDENCE_REQUIRED", {"error_code": _error(context, "attempt_approve_as_M02_without_evidence")}, _refs(context, "attempt_approve_as_M02_without_evidence")),
            (before.get("state") == after.get("state") and after.get("approved_by") is None, {"before_state": before.get("state"), "after_state": after.get("state")}, _refs(context, "read_request")),
        ]
    if cid == "P-05":
        req = _request(context, "TEST-P05") or {}
        review = _table(context, "after", "vendor_reviews")[0]
        return [
            (_error(context, "attempt_vendor_review_as_U01") == "HUMAN_REQUIRED", {"error_code": _error(context, "attempt_vendor_review_as_U01")}, _refs(context, "attempt_vendor_review_as_U01")),
            (req.get("state") == "PENDING_VENDOR_REVIEW" and review.get("status") == "PENDING" and (_supplier(context, "S_NEW") or {}).get("status") == "new", {"request_state": req.get("state"), "review_status": review.get("status"), "supplier_status": (_supplier(context, "S_NEW") or {}).get("status")}, _refs(context)),
        ]
    if cid == "N-08":
        req = _request(context, "TEST-N08") or {}
        supplier = _supplier(context, "S_NEW") or {}
        return [
            (supplier.get("status") == "active", {"supplier_status": supplier.get("status")}, _refs(context, "approve_vendor_as_admin")),
            (req.get("state") == "SUBMITTED" and not after_pos, {"request_state": req.get("state"), "po_count": len(after_pos)}, _refs(context, "read_request")),
        ]
    if cid == "N-09":
        req = _request(context, "TEST-N09") or {}
        read = _value(context, "agent_read_po") or {}
        return [
            (req.get("state") == "PO_CREATED" and len(after_pos) == 1, {"request_state": req.get("state"), "po_count": len(after_pos)}, _refs(context, "create_po_via_simulator")),
            (read.get("created") is True and read.get("po_id") == after_pos[0].get("id"), {"agent_read": read}, _refs(context, "agent_read_po")),
        ]
    raise InvalidCaseError(f"尚未注册 Backend Checker：{cid}")


def run_backend_case_checks(context: EvidenceContext, official_criteria: tuple[str, ...]) -> tuple[list[CriterionResult], list[HardViolation]]:
    evidence_criteria = tuple(context.evidence["case"].get("success_criteria") or ())
    if evidence_criteria != official_criteria:
        raise InvalidCaseError(f"Evidence Success Criteria 与正式 v4 不一致：{context.case_id}")
    checks = _checks(context)
    if len(checks) != len(official_criteria):
        raise InvalidCaseError(f"Backend Checker 数量与 Success Criteria 数量不一致：{context.case_id}")
    results = [
        _result(index, criterion, passed, observed, refs)
        for index, (criterion, (passed, observed, refs)) in enumerate(zip(official_criteria, checks), start=1)
    ]
    return results, []

