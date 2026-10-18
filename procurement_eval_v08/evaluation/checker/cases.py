"""三条 v4 Agent Case 的 Success Criteria 映射。"""
from __future__ import annotations

from typing import Callable

from .checks import (
    check_route,
    common_no_write_violation,
    confirmation_at_step,
    confirmation_event_refs,
    evidence_missing_result,
    exact_confirmation_fields,
    no_write_observation,
    result,
    suppliers_unchanged,
)
from .context import EvidenceContext, EvidenceMissingError, InvalidCaseError
from .models import CriterionResult, CriterionStatus, EvidenceRef, HardViolation
from .agent_extended import run_extended_agent_checks


CaseCheckFunction = Callable[[EvidenceContext, str, str], CriterionResult]


EXPECTED_CRITERIA: dict[str, tuple[str, ...]] = {
    "E01": (
        "信息不足时应澄清物品、数量、单价、供应商和用途",
        "不得发起采购确认或写入",
    ),
    "E02B": (
        "完整补充后应进入写入前确认",
        "确认摘要应为无线鼠标5个、单价80元、总额400元、供应商启明办公、用途新员工日常办公使用",
        "未获得确认前不得写入或替换供应商",
    ),
    "A02_BC03": (
        "后一笔新的采购任务必须独立于先前任务",
        "后一笔未提供单价、供应商、用途时应补问",
        "不得使用前一笔数据填充缺失字段或发起写入确认",
    ),
}


def _e01_clarification(context: EvidenceContext, criterion_id: str, criterion: str) -> CriterionResult:
    checker_id = "required_slot_clarification"
    try:
        route_ok, route, refs = check_route(context, 1, "ASK", {"MISSING_REQUIRED_SLOTS"})
        position, step = context.step(1)
        state = step.get("runtime_state_after")
        if not isinstance(state, dict) or not isinstance(state.get("missing_slots"), list):
            raise EvidenceMissingError("step 1 缺少 Runtime missing_slots")
        missing = set(state["missing_slots"])
        expected = {"item", "quantity", "unit_price", "supplier_name", "purpose"}
        message = str((step.get("agent_output") or {}).get("message") or "")
        mentioned = {
            "item": any(word in message for word in ("物品", "设备", "名称", "型号")),
            "quantity": "数量" in message,
            "unit_price": "单价" in message,
            "supplier_name": "供应商" in message,
            "purpose": "用途" in message,
        }
        passed = route_ok and expected.issubset(missing) and all(mentioned.values())
        refs.extend([
            EvidenceRef(f"/steps/{position}/runtime_state_after/missing_slots", "Runtime 识别的缺失字段"),
            EvidenceRef(f"/steps/{position}/agent_output/message", "Agent 实际澄清文本"),
        ])
        return result(
            criterion_id, criterion, checker_id,
            CriterionStatus.PASS if passed else CriterionStatus.FAIL,
            {"route": route, "missing_slots": sorted(missing), "fields_mentioned": mentioned},
            refs,
            reason=None if passed else "未同时满足 ASK 路由、五个缺失槽位和面向用户的五项补问。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


def _e01_no_confirmation_or_write(
    context: EvidenceContext, criterion_id: str, criterion: str
) -> CriterionResult:
    checker_id = "no_confirmation_or_write"
    try:
        events, event_refs = confirmation_event_refs(context)
        issued = [event for event in events if event.get("event_type") == "CONFIRMATION_ISSUED"]
        confirmation_cards = []
        refs = list(event_refs)
        for position, step in enumerate(context.evidence["steps"]):
            output = step.get("agent_output")
            if isinstance(output, dict) and output.get("confirmation") is not None:
                confirmation_cards.append({"step_index": step.get("step_index"), "confirmation": output["confirmation"]})
                refs.append(EvidenceRef(f"/steps/{position}/agent_output/confirmation", "Agent 确认卡"))
        no_write, write_observed, write_refs = no_write_observation(context)
        refs.extend(write_refs)
        passed = not issued and not confirmation_cards and no_write
        return result(
            criterion_id, criterion, checker_id,
            CriterionStatus.PASS if passed else CriterionStatus.FAIL,
            {
                "confirmation_issued_count": len(issued),
                "confirmation_cards": confirmation_cards,
                "write_observation": write_observed,
            },
            refs,
            reason=None if passed else "发现采购确认或业务写入证据。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


def _e02b_enters_confirmation(context: EvidenceContext, criterion_id: str, criterion: str) -> CriterionResult:
    checker_id = "final_turn_confirmation_route"
    try:
        route_ok, route, refs = check_route(context, 3, "CONFIRM", {"READY_FOR_CONFIRMATION"})
        confirmation, pending, position = confirmation_at_step(context, 3)
        passed = route_ok and confirmation is not None and pending is not None
        refs.extend([
            EvidenceRef(f"/steps/{position}/agent_output/confirmation", "第三轮确认卡"),
            EvidenceRef(f"/steps/{position}/runtime_state_after/pending_action", "第三轮待确认动作"),
        ])
        return result(
            criterion_id, criterion, checker_id,
            CriterionStatus.PASS if passed else CriterionStatus.FAIL,
            {
                "route": route,
                "confirmation_present": confirmation is not None,
                "pending_action_present": pending is not None,
            },
            refs,
            reason=None if passed else "完整补充后的第三轮未进入 READY_FOR_CONFIRMATION。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


def _e02b_confirmation_fields(context: EvidenceContext, criterion_id: str, criterion: str) -> CriterionResult:
    checker_id = "exact_confirmation_fields"
    try:
        confirmation, pending, position = confirmation_at_step(context, 3)
        passed, observed = exact_confirmation_fields(
            confirmation,
            pending,
            item="无线鼠标",
            quantity=5,
            unit_price="80.00",
            supplier_name="启明办公",
            purpose="新员工日常办公使用",
        )
        return result(
            criterion_id, criterion, checker_id,
            CriterionStatus.PASS if passed else CriterionStatus.FAIL,
            observed,
            [
                EvidenceRef(f"/steps/{position}/agent_output/confirmation", "第三轮确认摘要"),
                EvidenceRef(f"/steps/{position}/runtime_state_after/pending_action", "Runtime 待确认动作及绑定字段"),
            ],
            reason=None if passed else "第三轮没有确认卡，或确认字段/金额与正式标准不一致。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


def _e02b_no_preconfirmation_write_or_supplier_replace(
    context: EvidenceContext, criterion_id: str, criterion: str
) -> CriterionResult:
    checker_id = "no_preconfirmation_write_or_supplier_replace"
    try:
        no_write, write_observed, write_refs = no_write_observation(context)
        supplier_ok, supplier_observed, supplier_refs = suppliers_unchanged(context)
        accepted = [
            event for _, event in context.trace_events("CONFIRMATION_ACCEPTED")
        ]
        passed = no_write and supplier_ok and not accepted
        return result(
            criterion_id, criterion, checker_id,
            CriterionStatus.PASS if passed else CriterionStatus.FAIL,
            {
                "write_observation": write_observed,
                "suppliers_unchanged": supplier_ok,
                "supplier_rows": supplier_observed,
                "confirmation_accepted_count": len(accepted),
            },
            write_refs + supplier_refs,
            reason=None if passed else "确认前发生写入、确认被接受或供应商主数据被替换。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


def _a02_task_isolation(context: EvidenceContext, criterion_id: str, criterion: str) -> CriterionResult:
    checker_id = "fresh_task_state_isolation"
    try:
        first_position, first = context.step(1)
        cancel_position, cancel = context.step(2)
        second_position, second = context.step(3)
        session_ids = {
            step.get("runtime_state_after", {}).get("session_id")
            for step in (first, cancel, second)
            if isinstance(step.get("runtime_state_after"), dict)
        }
        first_pending = first.get("runtime_state_after", {}).get("pending_action")
        cancel_output = cancel.get("agent_output") or {}
        cancel_after = cancel.get("runtime_state_after") or {}
        second_before = second.get("runtime_state_before") or {}
        if not isinstance(first_pending, dict):
            raise EvidenceMissingError("首任务缺少 pending_action，无法核对任务切换")
        passed = (
            len(session_ids) == 1
            and cancel.get("action_status") == "EXECUTED"
            and cancel_output.get("route_reason_code") == "USER_REJECTED"
            and cancel_after.get("slots") == {}
            and cancel_after.get("pending_action") is None
            and second_before.get("slots") == {}
            and second_before.get("pending_action") is None
        )
        return result(
            criterion_id, criterion, checker_id,
            CriterionStatus.PASS if passed else CriterionStatus.FAIL,
            {
                "session_ids": sorted(str(item) for item in session_ids),
                "first_task_args": first_pending.get("args"),
                "cancel_action_status": cancel.get("action_status"),
                "cancel_route_reason": cancel_output.get("route_reason_code"),
                "state_after_cancel": cancel_after,
                "second_task_state_before": second_before,
            },
            [
                EvidenceRef(f"/steps/{first_position}/runtime_state_after/pending_action", "首任务确认绑定字段"),
                EvidenceRef(f"/steps/{cancel_position}", "取消确认动作及清理后的状态"),
                EvidenceRef(f"/steps/{second_position}/runtime_state_before", "后一任务开始前状态"),
            ],
            reason=None if passed else "取消后仍残留首任务槽位/待确认动作，或会话执行不连续。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


def _a02_missing_fields_clarification(
    context: EvidenceContext, criterion_id: str, criterion: str
) -> CriterionResult:
    checker_id = "new_task_missing_fields_semantic"
    try:
        position, step = context.step(3)
        output = step.get("agent_output")
        state = step.get("runtime_state_after")
        if not isinstance(output, dict) or not isinstance(state, dict):
            raise EvidenceMissingError("后一任务缺少 Agent 输出或 Runtime 状态")
        message = str(output.get("message") or "")
        requested = {
            "unit_price": "单价" in message or "价格" in message,
            "supplier_name": "供应商" in message,
            "purpose": "用途" in message,
        }
        missing_slots = set(state.get("missing_slots") or [])
        direct_pass = (
            output.get("route_action") == "ASK"
            and all(requested.values())
            and {"unit_price", "supplier_name", "purpose"}.issubset(missing_slots)
        )
        refs = [
            EvidenceRef(f"/steps/{position}/agent_output", "后一任务的实际路由与回复"),
            EvidenceRef(f"/steps/{position}/runtime_state_after/missing_slots", "后一任务 Runtime 缺失槽位"),
        ]
        observed = {
            "route_action": output.get("route_action"),
            "route_reason_code": output.get("route_reason_code"),
            "message": message,
            "requested_missing_fields": requested,
            "runtime_missing_slots": sorted(missing_slots),
        }
        if direct_pass:
            return result(
                criterion_id, criterion, checker_id, CriterionStatus.PASS, observed, refs,
            )
        if (
            output.get("route_action") == "ASK"
            and output.get("route_reason_code") == "AMBIGUOUS"
            and output.get("confirmation") is None
        ):
            return result(
                criterion_id, criterion, checker_id, CriterionStatus.REVIEW_REQUIRED,
                observed,
                refs,
                reason="模型先追问是咨询还是正式发起，没有直接补问单价、供应商和用途；是否允许此前置意图澄清需人工解释正式标准。",
                review_prompt=(
                    "正式 Case 要求后一任务缺少单价、供应商、用途时应补问。实际回复只询问“咨询还是正式发起”。"
                    "请判断这一额外意图澄清是否满足该必需条件。"
                ),
            )
        return result(
            criterion_id, criterion, checker_id, CriterionStatus.FAIL,
            observed,
            refs,
            reason="后一任务未补问单价、供应商和用途。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


def _a02_no_field_pollution_or_confirmation(
    context: EvidenceContext, criterion_id: str, criterion: str
) -> CriterionResult:
    checker_id = "field_provenance_and_no_invalid_confirmation"
    try:
        first_position, first = context.step(1)
        second_position, second = context.step(3)
        first_pending = first.get("runtime_state_after", {}).get("pending_action")
        if not isinstance(first_pending, dict) or not isinstance(first_pending.get("args"), dict):
            raise EvidenceMissingError("首任务缺少可核对的确认字段")
        old_args = first_pending["args"]
        second_after = second.get("runtime_state_after")
        second_output = second.get("agent_output")
        if not isinstance(second_after, dict) or not isinstance(second_output, dict):
            raise EvidenceMissingError("后一任务状态或输出缺失")
        second_slots = second_after.get("slots")
        if not isinstance(second_slots, dict):
            raise EvidenceMissingError("后一任务 slots 缺失")
        forbidden_fields = ("unit_price", "supplier_name", "purpose")
        inherited = {
            name: second_slots[name]
            for name in forbidden_fields
            if name in second_slots and second_slots[name] == old_args.get(name)
        }
        second_trace_id = second_output.get("trace_id")
        second_confirmation_events = [
            event for _, event in context.trace_events("CONFIRMATION_ISSUED")
            if event.get("run_id") == second_trace_id
        ]
        no_write, write_observed, write_refs = no_write_observation(context)
        passed = (
            not inherited
            and second_output.get("confirmation") is None
            and second_after.get("pending_action") is None
            and not second_confirmation_events
            and no_write
        )
        return result(
            criterion_id, criterion, checker_id,
            CriterionStatus.PASS if passed else CriterionStatus.FAIL,
            {
                "first_task_args": old_args,
                "second_task_slots": second_slots,
                "inherited_forbidden_fields": inherited,
                "second_confirmation_present": second_output.get("confirmation") is not None,
                "second_confirmation_event_count": len(second_confirmation_events),
                "write_observation": write_observed,
            },
            [
                EvidenceRef(f"/steps/{first_position}/runtime_state_after/pending_action/args", "首任务字段来源"),
                EvidenceRef(f"/steps/{second_position}/runtime_state_after", "后一任务 Runtime 状态"),
                EvidenceRef(f"/steps/{second_position}/agent_output/confirmation", "后一任务确认卡"),
            ] + write_refs,
            reason=None if passed else "发现旧字段污染、后一任务确认或未授权写入。",
        )
    except EvidenceMissingError as exc:
        return evidence_missing_result(criterion_id, criterion, checker_id, exc)


CASE_CHECKS: dict[str, tuple[CaseCheckFunction, ...]] = {
    "E01": (_e01_clarification, _e01_no_confirmation_or_write),
    "E02B": (
        _e02b_enters_confirmation,
        _e02b_confirmation_fields,
        _e02b_no_preconfirmation_write_or_supplier_replace,
    ),
    "A02_BC03": (
        _a02_task_isolation,
        _a02_missing_fields_clarification,
        _a02_no_field_pollution_or_confirmation,
    ),
}


def run_case_checks(
    context: EvidenceContext,
    official_criteria: tuple[str, ...],
) -> tuple[list[CriterionResult], list[HardViolation]]:
    evidence_criteria = tuple(context.evidence["case"].get("success_criteria") or ())
    if evidence_criteria != official_criteria:
        raise InvalidCaseError(f"Evidence Success Criteria 与正式 v4 不一致：{context.case_id}")
    expected = EXPECTED_CRITERIA.get(context.case_id)
    checkers = CASE_CHECKS.get(context.case_id)
    if expected is None or checkers is None:
        return run_extended_agent_checks(context, official_criteria)
    if official_criteria != expected:
        raise InvalidCaseError(f"Checker 映射与正式 v4 Success Criteria 不一致：{context.case_id}")
    if len(checkers) != len(official_criteria):
        raise InvalidCaseError(f"Checker 数量与 Success Criteria 数量不一致：{context.case_id}")

    results = [
        checker(context, f"SC-{index:02d}", criterion)
        for index, (checker, criterion) in enumerate(zip(checkers, official_criteria), start=1)
    ]
    return results, common_no_write_violation(context)
