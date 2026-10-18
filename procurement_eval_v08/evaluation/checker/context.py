"""Evidence 读取、结构校验与 JSON Pointer 解析。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InvalidCaseError(ValueError):
    """Evidence 与正式 Case 不一致或 Case 尚无 Checker。"""


class EvidenceMissingError(ValueError):
    """评分所需的 Evidence 结构缺失。"""


class InvalidEvidenceError(ValueError):
    """证据身份或关联不一致，不能作为 Agent 能力失败。"""


def resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise KeyError(pointer)
    return current


@dataclass
class EvidenceContext:
    path: Path
    raw_bytes: bytes
    evidence: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "EvidenceContext":
        evidence_path = Path(path).resolve()
        try:
            raw = evidence_path.read_bytes()
        except FileNotFoundError as exc:
            raise EvidenceMissingError(f"Evidence 文件不存在：{evidence_path}") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceMissingError(f"Evidence JSON 无法解析：{evidence_path}") from exc
        if not isinstance(value, dict):
            raise EvidenceMissingError("Evidence 顶层必须是对象")
        context = cls(evidence_path, raw, value)
        context.validate_base()
        return context

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()

    @property
    def case_id(self) -> str:
        return str(self.evidence["case"]["case_id"])

    @property
    def run_id(self) -> str:
        return str(self.evidence["run"]["run_id"])

    def validate_base(self) -> None:
        required = {
            "case": dict,
            "run": dict,
            "steps": list,
            "business_state": dict,
            "trace": dict,
            "audit": dict,
            "evidence_completeness": dict,
        }
        for name, expected_type in required.items():
            if not isinstance(self.evidence.get(name), expected_type):
                raise EvidenceMissingError(f"Evidence 缺少或无效：{name}")
        for pointer in (
            "/case/case_id",
            "/case/success_criteria",
            "/run/run_id",
            "/run/session_id",
            "/business_state/before/tables",
            "/business_state/after/tables",
            "/business_state/delta",
            "/trace/events",
        ):
            try:
                resolve_pointer(self.evidence, pointer)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise EvidenceMissingError(f"Evidence 缺少：{pointer}") from exc

    def validate_association(self, expected_context: dict[str, str] | None = None) -> None:
        identity = {"case_id": self.evidence["case"].get("case_id"), "run_id": self.evidence["run"].get("run_id"),
                    "session_id": self.evidence["run"].get("session_id")}
        for name, value in identity.items():
            if not isinstance(value, str) or not value.strip():
                raise EvidenceMissingError(f"Evidence 缺少有效身份：{name}")

        def check(values: dict[str, Any], label: str, *, partial: bool = False) -> None:
            for name, expected in identity.items():
                if partial and name not in values:
                    continue
                if values.get(name) != expected:
                    raise InvalidEvidenceError(f"{label}.{name} 不一致：期望 {expected!r}，实际 {values.get(name)!r}")

        if expected_context is not None:
            check(expected_context, "expected_context", partial=True)
        for name in ("scenario", "fixture_spec"):
            values = self.evidence.get(name)
            if isinstance(values, dict):
                check({"case_id": values.get("case_id")}, name, partial=True)
        manifest_path = self.path.parent / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise InvalidEvidenceError("Run manifest 无法解析") from exc
            if not isinstance(manifest, dict):
                raise InvalidEvidenceError("Run manifest 必须是对象")
            check(manifest, "manifest")

        trace = self.evidence["trace"]
        if "context" in trace:
            if not isinstance(trace["context"], dict):
                raise InvalidEvidenceError("trace.context 必须是对象")
            check(trace["context"], "trace.context")
        else:
            # 历史 Trace 没有评估封套，利用已有 Case→Run→Session 和步骤引用校验，不回填历史文件。
            suite = self.evidence.get("suite") or self.evidence.get("scenario", {}).get("suite", "agent")
            session = f"backend:{self.run_id}" if suite == "backend" else f"eval-{self.run_id}"
            if not self.run_id.startswith(self.case_id + "-") or identity["session_id"] != session:
                raise InvalidEvidenceError("历史 Evidence 的 Case/Run/Session 关联不一致")

        turn_ids = set()
        pending_turn_ids = []
        for index, step in enumerate(self.evidence["steps"]):
            for state_name in ("runtime_state_before", "runtime_state_after"):
                state = step.get(state_name)
                if isinstance(state, dict):
                    check({"session_id": state.get("session_id")}, f"steps[{index}].{state_name}", partial=True)
                    pending = state.get("pending_action")
                    if isinstance(pending, dict) and pending.get("run_id"):
                        pending_turn_ids.append(pending["run_id"])
            output = step.get("agent_output")
            if isinstance(output, dict):
                if not isinstance(output.get("trace_id"), str) or not output["trace_id"]:
                    raise InvalidEvidenceError(f"steps[{index}].agent_output 缺少 Trace 轮次关联")
                turn_ids.add(output["trace_id"])
        completed = self.evidence["run"].get("status") in {"COMPLETED_AWAITING_SCORING", "EVIDENCE_INCOMPLETE"}
        if completed and any(not isinstance(value, str) or value not in turn_ids for value in pending_turn_ids):
            raise InvalidEvidenceError("待确认状态的 run_id 不属于当前评估步骤")
        events = trace.get("events")
        if not isinstance(events, list):
            raise EvidenceMissingError("Evidence 缺少 trace.events")
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise InvalidEvidenceError(f"trace.events[{index}] 必须是对象")
            check({"session_id": event.get("session_id")}, f"trace.events[{index}]", partial=True)
            if "evaluation_context" in event:
                if not isinstance(event["evaluation_context"], dict):
                    raise InvalidEvidenceError(f"trace.events[{index}].evaluation_context 必须是对象")
                check(event["evaluation_context"], f"trace.events[{index}].evaluation_context")
            if "case_id" in event:
                check({"case_id": event["case_id"]}, f"trace.events[{index}]", partial=True)
            # 每轮 Runtime run_id 不等于评估 Run ID，必须能回连当前步骤的 trace_id/确认轮次。
            if completed and (not isinstance(event.get("run_id"), str) or event["run_id"] not in turn_ids):
                raise InvalidEvidenceError(f"trace.events[{index}].run_id 不属于当前评估步骤")

    def at(self, pointer: str) -> Any:
        try:
            return resolve_pointer(self.evidence, pointer)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EvidenceMissingError(f"Evidence 引用不存在：{pointer}") from exc

    def step(self, step_index: int) -> tuple[int, dict[str, Any]]:
        for position, step in enumerate(self.evidence["steps"]):
            if step.get("step_index") == step_index:
                return position, step
        raise EvidenceMissingError(f"Evidence 缺少 scenario step {step_index}")

    def trace_events(self, event_type: str | None = None) -> list[tuple[int, dict[str, Any]]]:
        events = self.evidence.get("trace", {}).get("events")
        if not isinstance(events, list):
            raise EvidenceMissingError("Evidence 缺少 trace.events")
        return [
            (index, event) for index, event in enumerate(events)
            if event_type is None or event.get("event_type") == event_type
        ]

    def table(self, moment: str, table: str) -> list[dict[str, Any]]:
        value = self.at(f"/business_state/{moment}/tables/{table}")
        if not isinstance(value, list):
            raise EvidenceMissingError(f"业务快照表无效：{moment}.{table}")
        return value
