"""Evidence → Success Criteria → Strict Pass。"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation.case_loader import CaseLoader

from .cases import run_case_checks
from .backend_cases import run_backend_case_checks
from .context import EvidenceContext, EvidenceMissingError, InvalidCaseError, InvalidEvidenceError
from .manual_review import apply_manual_reviews, load_manual_reviews, review_template
from .models import CaseStatus, CriterionResult, CriterionStatus
from .reporter import score_markdown


CHECKER_VERSION = "3.0.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OfflineScorer:
    def __init__(self, loader: CaseLoader):
        self.loader = loader

    def score_file(
        self,
        evidence_path: str | Path,
        *,
        review_files: list[str | Path] | None = None,
        expected_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            context = EvidenceContext.load(evidence_path)
        except EvidenceMissingError as exc:
            return self._terminal_result(
                evidence_path,
                CaseStatus.EVIDENCE_MISSING,
                str(exc),
            )

        try:
            context.validate_association(expected_context)
        except (InvalidEvidenceError, EvidenceMissingError) as exc:
            status = CaseStatus.INVALID_EVIDENCE if isinstance(exc, InvalidEvidenceError) else CaseStatus.EVIDENCE_MISSING
            return self._terminal_result(evidence_path, status, str(exc), context=context)

        try:
            suite = context.evidence.get("suite") or context.evidence.get("scenario", {}).get("suite", "agent")
            loaded = self.loader.load_backend(context.case_id) if suite == "backend" else self.loader.load(context.case_id)
            official_criteria = tuple(loaded.case.success_criteria)
            if suite == "backend":
                criteria, hard_violations = run_backend_case_checks(context, official_criteria)
            else:
                criteria, hard_violations = run_case_checks(context, official_criteria)
        except (KeyError, InvalidCaseError) as exc:
            return self._terminal_result(
                evidence_path,
                CaseStatus.INVALID_CASE,
                str(exc),
                context=context,
            )

        reviews, review_errors = load_manual_reviews(review_files or [], context)
        applied_reviews, apply_errors = apply_manual_reviews(criteria, reviews)
        review_errors.extend(apply_errors)
        evidence_gate = self._evidence_gate(context, len(loaded.scenario.steps))
        final_status, failure_reasons = self._aggregate(
            criteria,
            hard_violations_count=len(hard_violations),
            evidence_ready=evidence_gate["ready"],
        )
        counts = Counter(item.status.value for item in criteria)
        trace_events = context.evidence.get("trace", {}).get("events", [])
        tool_started = [event for event in trace_events if event.get("event_type") == "TOOL_CALL_STARTED"]
        tool_finished = [event for event in trace_events if event.get("event_type") in {"TOOL_CALL_COMPLETED", "TOOL_CALL_FAILED"}]
        route_events = [event for event in trace_events if event.get("event_type") == "ROUTE_DECIDED"]
        score = {
            "schema_version": "scored-result-1.0",
            "score_id": "score-" + uuid.uuid4().hex,
            "scored_at": utc_now(),
            "checker_version": CHECKER_VERSION,
            "case_id": context.case_id,
            "suite": suite,
            "category": loaded.case.category,
            "model": context.evidence.get("run", {}).get("model", {}),
            "run_config": context.evidence.get("run", {}).get("runtime_config", {}),
            "run_id": context.run_id,
            "run_started_at": context.evidence["run"].get("started_at"),
            "evidence": {
                "path": str(context.path),
                "sha256": context.sha256,
                "schema_version": context.evidence.get("schema_version"),
            },
            "evidence_gate": evidence_gate,
            "criteria": [item.to_dict() for item in criteria],
            "hard_violations": [item.to_dict() for item in hard_violations],
            "manual_review": {
                "required_criterion_ids": [
                    item.criterion_id for item in criteria
                    if item.status == CriterionStatus.REVIEW_REQUIRED
                ],
                "applied_criterion_ids": applied_reviews,
                "errors": review_errors,
            },
            "summary": {
                "criterion_counts": {
                    status.value: counts.get(status.value, 0) for status in CriterionStatus
                },
                "hard_violation_count": len(hard_violations),
            },
            "diagnostics": {
                "tool_call_count": len(tool_started),
                "tool_calls_by_name": dict(Counter(str(event.get("tool_name") or "UNKNOWN") for event in tool_started)),
                "tool_outcomes": dict(Counter(str(event.get("outcome_status") or "UNKNOWN") for event in tool_finished)),
                "routes": dict(Counter(str(event.get("route_action") or "UNKNOWN") for event in route_events)),
            },
            "final_result": final_status.value,
            "strict_pass": final_status == CaseStatus.PASS,
            "failure_reasons": failure_reasons,
        }
        return score

    @staticmethod
    def _evidence_gate(context: EvidenceContext, expected_steps: int) -> dict[str, Any]:
        actual_steps = len(context.evidence.get("steps", []))
        complete = context.evidence.get("evidence_completeness", {}).get("complete") is True
        run_status = context.evidence.get("run", {}).get("status")
        session_id = context.evidence.get("run", {}).get("session_id")
        missing = []
        if not complete:
            missing.append("evidence_completeness.complete")
        if actual_steps != expected_steps:
            missing.append(f"scenario_steps:{actual_steps}/{expected_steps}")
        if not isinstance(session_id, str) or not session_id:
            missing.append("run.session_id")
        if run_status not in {"COMPLETED_AWAITING_SCORING", "EVIDENCE_INCOMPLETE"}:
            missing.append(f"run.status:{run_status}")
        return {
            "ready": not missing,
            "run_status": run_status,
            "expected_steps": expected_steps,
            "actual_steps": actual_steps,
            "missing": missing,
        }

    @staticmethod
    def _aggregate(
        criteria: list[CriterionResult],
        *,
        hard_violations_count: int,
        evidence_ready: bool,
    ) -> tuple[CaseStatus, list[str]]:
        failures = [item for item in criteria if item.status == CriterionStatus.FAIL]
        missing = [item for item in criteria if item.status == CriterionStatus.EVIDENCE_MISSING]
        review = [item for item in criteria if item.status == CriterionStatus.REVIEW_REQUIRED]
        reasons: list[str] = []
        if hard_violations_count:
            reasons.append(f"发生 {hard_violations_count} 项硬性违规")
        reasons.extend(f"{item.criterion_id}：{item.reason}" for item in failures)
        # 已有充分失败证据时优先 FAIL，同时保留其他检查项结果。
        if hard_violations_count or failures:
            return CaseStatus.FAIL, reasons
        if missing or not evidence_ready:
            reasons.extend(f"{item.criterion_id}：{item.reason}" for item in missing)
            if not evidence_ready:
                reasons.append("Evidence 完整性门禁未通过")
            return CaseStatus.EVIDENCE_MISSING, reasons
        if review:
            reasons.extend(f"{item.criterion_id}：{item.reason}" for item in review)
            return CaseStatus.REVIEW_REQUIRED, reasons
        return CaseStatus.PASS, []

    @staticmethod
    def _terminal_result(
        evidence_path: str | Path,
        status: CaseStatus,
        reason: str,
        *,
        context: EvidenceContext | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "scored-result-1.0",
            "score_id": "score-" + uuid.uuid4().hex,
            "scored_at": utc_now(),
            "checker_version": CHECKER_VERSION,
            "case_id": context.case_id if context else None,
            "run_id": context.run_id if context else None,
            "run_started_at": context.evidence["run"].get("started_at") if context else None,
            "suite": (context.evidence.get("suite") or context.evidence.get("scenario", {}).get("suite", "agent")) if context else "agent",
            "category": context.evidence["case"].get("category") if context else None,
            "evidence": {
                "path": str(Path(evidence_path).resolve()),
                "sha256": context.sha256 if context else None,
                "schema_version": context.evidence.get("schema_version") if context else None,
            },
            "evidence_gate": {"ready": False, "missing": [reason]},
            "criteria": [],
            "hard_violations": [],
            "manual_review": {"required_criterion_ids": [], "applied_criterion_ids": [], "errors": []},
            "summary": {"criterion_counts": {}, "hard_violation_count": 0},
            "final_result": status.value,
            "strict_pass": False,
            "failure_reasons": [reason],
        }

    @staticmethod
    def persist(score: dict[str, Any], output_dir: str | Path | None = None,
                *, report_dir: str | Path | None = None) -> dict[str, str]:
        directory = Path(output_dir).resolve() if output_dir else Path(score["evidence"]["path"]).resolve().parent
        directory.mkdir(parents=True, exist_ok=True)
        scored_path = directory / "scored.jsonl"
        with scored_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(score, ensure_ascii=False, sort_keys=True) + "\n")
        # 评分记录和复评模板留在证据目录，面向阅读的报告单独输出。
        report_directory = (Path(report_dir).resolve() if report_dir else
                            Path(__file__).resolve().parents[1] / "reports" / directory.name)
        report_directory.mkdir(parents=True, exist_ok=True)
        report_path = report_directory / "score_report.md"
        report_path.write_text(score_markdown(score), encoding="utf-8")

        template = review_template(
            str(score.get("case_id") or ""),
            str(score.get("run_id") or ""),
            str(score.get("evidence", {}).get("sha256") or ""),
            [OfflineScorer._criterion_from_dict(item) for item in score.get("criteria", [])],
        )
        template_path = directory / "manual_review.template.json"
        if template is not None:
            template_path.write_text(
                json.dumps(template, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return {
            "scored_jsonl": str(scored_path),
            "score_report": str(report_path),
            "manual_review_template": str(template_path) if template is not None else "",
        }

    @staticmethod
    def _criterion_from_dict(value: dict[str, Any]) -> CriterionResult:
        from .models import EvidenceRef

        return CriterionResult(
            criterion_id=value["criterion_id"],
            success_criterion=value["success_criterion"],
            checker_id=value["checker_id"],
            status=CriterionStatus(value["status"]),
            evaluation_mode=value["evaluation_mode"],
            observed=value.get("observed", {}),
            evidence_refs=[EvidenceRef(**ref) for ref in value.get("evidence_refs", [])],
            reason=value.get("reason"),
            review_prompt=value.get("review_prompt"),
            manual_review=value.get("manual_review"),
        )
