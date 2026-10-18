"""评分结果的数据模型。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class CriterionStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"


class CaseStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    INVALID_CASE = "INVALID_CASE"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


@dataclass
class EvidenceRef:
    pointer: str
    description: str


@dataclass
class CriterionResult:
    criterion_id: str
    success_criterion: str
    checker_id: str
    status: CriterionStatus
    evaluation_mode: str
    observed: dict[str, Any]
    evidence_refs: list[EvidenceRef]
    reason: str | None = None
    review_prompt: str | None = None
    manual_review: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass
class HardViolation:
    code: str
    reason: str
    observed: dict[str, Any]
    evidence_refs: list[EvidenceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
