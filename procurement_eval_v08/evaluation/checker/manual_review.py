"""人工复核文件的生成、校验与应用。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .context import EvidenceContext, resolve_pointer
from .models import CriterionResult, CriterionStatus


class ManualReviewError(ValueError):
    pass


def review_template(
    case_id: str,
    run_id: str,
    evidence_sha256: str,
    criteria: list[CriterionResult],
) -> dict[str, Any] | None:
    pending = [item for item in criteria if item.status == CriterionStatus.REVIEW_REQUIRED]
    if not pending:
        return None
    return {
        "schema_version": "manual-review-1.0",
        "case_id": case_id,
        "run_id": run_id,
        "evidence_sha256": evidence_sha256,
        "reviews": [
            {
                "criterion_id": item.criterion_id,
                "decision": None,
                "reviewer": None,
                "reviewed_at": None,
                "rationale": None,
                "evidence_refs": [ref.pointer for ref in item.evidence_refs],
                "review_prompt": item.review_prompt,
            }
            for item in pending
        ],
    }


def load_manual_reviews(
    paths: list[str | Path],
    context: EvidenceContext,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    records: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for supplied_path in paths:
        path = Path(supplied_path).resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ManualReviewError("顶层必须是对象")
            if document.get("schema_version") != "manual-review-1.0":
                raise ManualReviewError("schema_version 无效")
            if document.get("case_id") != context.case_id or document.get("run_id") != context.run_id:
                # 批量评分可同时提供多条 Run 的复核文件；只消费当前 Run 对应的文件。
                continue
            if document.get("evidence_sha256") != context.sha256:
                raise ManualReviewError("evidence_sha256 与原始 Evidence 不匹配")
            reviews = document.get("reviews")
            if not isinstance(reviews, list):
                raise ManualReviewError("reviews 必须是数组")
            for review in reviews:
                if not isinstance(review, dict):
                    raise ManualReviewError("review 条目必须是对象")
                criterion_id = review.get("criterion_id")
                decision = review.get("decision")
                if not isinstance(criterion_id, str) or not criterion_id:
                    raise ManualReviewError("criterion_id 无效")
                if decision not in {"PASS", "FAIL"}:
                    raise ManualReviewError(f"{criterion_id} decision 必须是 PASS 或 FAIL")
                for field in ("reviewer", "reviewed_at", "rationale"):
                    if not isinstance(review.get(field), str) or not review[field].strip():
                        raise ManualReviewError(f"{criterion_id} 缺少 {field}")
                refs = review.get("evidence_refs")
                if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs):
                    raise ManualReviewError(f"{criterion_id} 缺少 evidence_refs")
                for pointer in refs:
                    try:
                        resolve_pointer(context.evidence, pointer)
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        raise ManualReviewError(f"{criterion_id} Evidence 引用不存在：{pointer}") from exc
                if criterion_id in records:
                    raise ManualReviewError(f"重复人工结论：{criterion_id}")
                records[criterion_id] = {
                    **review,
                    "source_file": str(path),
                    "evidence_sha256": context.sha256,
                }
        except (OSError, json.JSONDecodeError, ManualReviewError) as exc:
            errors.append(f"{path}: {exc}")
    return records, errors


def apply_manual_reviews(
    criteria: list[CriterionResult],
    reviews: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    applied: list[str] = []
    errors: list[str] = []
    by_id = {item.criterion_id: item for item in criteria}
    for criterion_id, review in reviews.items():
        item = by_id.get(criterion_id)
        if item is None:
            errors.append(f"人工结论引用未知检查项：{criterion_id}")
            continue
        if item.status != CriterionStatus.REVIEW_REQUIRED:
            errors.append(f"人工结论不得覆盖确定性结果：{criterion_id}={item.status.value}")
            continue
        item.status = CriterionStatus(review["decision"])
        item.evaluation_mode = "manual_review"
        item.manual_review = review
        item.reason = review["rationale"]
        applied.append(criterion_id)
    return applied, errors
