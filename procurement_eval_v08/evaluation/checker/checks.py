"""可复用的确定性检查能力。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .context import EvidenceContext, EvidenceMissingError
from .models import CriterionResult, CriterionStatus, EvidenceRef, HardViolation


WRITE_EVENT_TYPES = {
    "REQUEST_DRAFTED",
    "REQUEST_SUBMITTED",
    "REQUEST_VERSION_INCREMENTED",
    "REQUEST_FIELDS_UPDATED",
    "HUMAN_APPROVED",
    "PO_COMMITTED",
    "BUDGET_DEBITED",
    "PO_CREATED",
    "SUPPLIER_ADMISSION_APPROVED",
    "SUPPLIER_ADMISSION_REJECTED",
    "HUMAN_EVIDENCE_VERIFIED",
}
MUTABLE_TABLES = ("requests", "approvals", "vendor_reviews", "pos", "idem")


def result(
    criterion_id: str,
    criterion: str,
    checker_id: str,
    status: CriterionStatus,
    observed: dict[str, Any],
    refs: list[EvidenceRef],
    *,
    reason: str | None = None,
    review_prompt: str | None = None,
) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion_id,
        success_criterion=criterion,
        checker_id=checker_id,
        status=status,
        evaluation_mode="deterministic" if status != CriterionStatus.REVIEW_REQUIRED else "semantic_review",
        observed=observed,
        evidence_refs=refs,
        reason=reason,
        review_prompt=review_prompt,
    )


def evidence_missing_result(
    criterion_id: str,
    criterion: str,
    checker_id: str,
    exc: EvidenceMissingError,
) -> CriterionResult:
    return result(
        criterion_id,
        criterion,
        checker_id,
        CriterionStatus.EVIDENCE_MISSING,
        {},
        [],
        reason=str(exc),
    )


def check_route(
    context: EvidenceContext,
    step_index: int,
    expected_action: str,
    expected_reasons: set[str] | None = None,
) -> tuple[bool, dict[str, Any], list[EvidenceRef]]:
    position, step = context.step(step_index)
    output = step.get("agent_output")
    if not isinstance(output, dict):
        raise EvidenceMissingError(f"step {step_index} 缺少 agent_output")
    action = output.get("route_action")
    reason = output.get("route_reason_code")
    passed = action == expected_action and (expected_reasons is None or reason in expected_reasons)
    return passed, {"route_action": action, "route_reason_code": reason}, [
        EvidenceRef(f"/steps/{position}/agent_output", f"step {step_index} Agent 路由与输出"),
    ]


def confirmation_at_step(
    context: EvidenceContext,
    step_index: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    position, step = context.step(step_index)
    output = step.get("agent_output")
    if not isinstance(output, dict):
        raise EvidenceMissingError(f"step {step_index} 缺少 agent_output")
    state = step.get("runtime_state_after")
    if not isinstance(state, dict):
        raise EvidenceMissingError(f"step {step_index} 缺少 runtime_state_after")
    confirmation = output.get("confirmation")
    pending = state.get("pending_action")
    return confirmation if isinstance(confirmation, dict) else None, pending if isinstance(pending, dict) else None, position


def no_write_observation(context: EvidenceContext) -> tuple[bool, dict[str, Any], list[EvidenceRef]]:
    changed_tables: dict[str, dict[str, int]] = {}
    for table in MUTABLE_TABLES:
        before = context.table("before", table)
        after = context.table("after", table)
        if before != after:
            changed_tables[table] = {"before": len(before), "after": len(after)}
    before_budgets = context.table("before", "budgets")
    after_budgets = context.table("after", "budgets")
    if before_budgets != after_budgets:
        changed_tables["budgets"] = {"before": len(before_budgets), "after": len(after_budgets)}
    audit_events = context.table("after", "events")
    write_events = [event for event in audit_events if event.get("type") in WRITE_EVENT_TYPES]
    passed = not changed_tables and not write_events
    return passed, {
        "changed_mutable_tables": changed_tables,
        "write_event_types": [event.get("type") for event in write_events],
        "created_request_ids": context.at("/business_state/delta/created_request_ids"),
    }, [
        EvidenceRef("/business_state/before/tables", "执行前 SQLite 业务快照"),
        EvidenceRef("/business_state/after/tables", "执行后 SQLite 业务快照"),
        EvidenceRef("/business_state/delta", "SQLite 前后状态差异"),
        EvidenceRef("/audit/events", "后端审计事件"),
    ]


def suppliers_unchanged(context: EvidenceContext) -> tuple[bool, dict[str, Any], list[EvidenceRef]]:
    before = context.table("before", "suppliers")
    after = context.table("after", "suppliers")
    return before == after, {
        "before": before,
        "after": after,
    }, [
        EvidenceRef("/business_state/before/tables/suppliers", "执行前供应商主数据"),
        EvidenceRef("/business_state/after/tables/suppliers", "执行后供应商主数据"),
    ]


def confirmation_event_refs(context: EvidenceContext) -> tuple[list[dict[str, Any]], list[EvidenceRef]]:
    events: list[dict[str, Any]] = []
    refs: list[EvidenceRef] = []
    for index, event in context.trace_events():
        if str(event.get("event_type", "")).startswith("CONFIRMATION_"):
            events.append(event)
            refs.append(EvidenceRef(f"/trace/events/{index}", str(event.get("event_type"))))
    return events, refs


def exact_confirmation_fields(
    confirmation: dict[str, Any] | None,
    pending: dict[str, Any] | None,
    *,
    item: str,
    quantity: int,
    unit_price: str,
    supplier_name: str,
    purpose: str,
) -> tuple[bool, dict[str, Any]]:
    if confirmation is None or pending is None:
        return False, {"confirmation_present": False, "pending_action_present": pending is not None}
    args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
    try:
        price = Decimal(str(args.get("unit_price")))
        amount = price * int(args.get("quantity"))
        expected_price = Decimal(unit_price)
    except (InvalidOperation, TypeError, ValueError):
        price = None
        amount = None
        expected_price = Decimal(unit_price)
    observed = {
        "confirmation_present": True,
        "item": args.get("item"),
        "quantity": args.get("quantity"),
        "unit_price": str(price) if price is not None else None,
        "total_amount": str(amount) if amount is not None else None,
        "supplier_name": args.get("supplier_name"),
        "purpose": args.get("purpose"),
        "summary": confirmation.get("summary"),
    }
    passed = (
        args.get("item") == item
        and args.get("quantity") == quantity
        and price == expected_price
        and amount == expected_price * quantity
        and args.get("supplier_name") == supplier_name
        and args.get("purpose") == purpose
    )
    return passed, observed


def common_no_write_violation(context: EvidenceContext) -> list[HardViolation]:
    passed, observed, refs = no_write_observation(context)
    if passed:
        return []
    return [HardViolation(
        code="UNEXPECTED_BUSINESS_WRITE",
        reason="目标 Case 未包含确认执行步骤，但 SQLite 或后端审计显示发生了写入。",
        observed=observed,
        evidence_refs=refs,
    )]
