"""运行证据落盘与完整性检查。"""
from __future__ import annotations

import json
import os
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .case_loader import LoadedCase


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact_secrets(value: str) -> str:
    """异常文本可能来自 SDK，落盘前移除本进程已知的模型凭据。"""
    result = value
    for name in ("AGENT_MODEL_API_KEY", "OPENAI_API_KEY"):
        secret = os.getenv(name, "")
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result


def read_jsonl_files(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(paths):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def state_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_tables = before.get("tables", {})
    after_tables = after.get("tables", {})
    before_requests = {row["id"]: row for row in before_tables.get("requests", [])}
    after_requests = {row["id"]: row for row in after_tables.get("requests", [])}
    return {
        "count_delta": {
            table: len(after_tables.get(table, [])) - len(before_tables.get(table, []))
            for table in sorted(set(before_tables) | set(after_tables))
        },
        "created_request_ids": sorted(set(after_requests) - set(before_requests)),
        "removed_request_ids": sorted(set(before_requests) - set(after_requests)),
        "changed_requests": [
            {"request_id": request_id, "before": before_requests[request_id], "after": after_requests[request_id]}
            for request_id in sorted(set(before_requests) & set(after_requests))
            if before_requests[request_id] != after_requests[request_id]
        ],
    }


class EvidenceCollector:
    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        loaded_case: LoadedCase,
        model_info: dict[str, Any],
        runtime_config: dict[str, Any],
    ):
        self.run_dir = run_dir
        self.run_id = run_id
        self.loaded_case = loaded_case
        self.model_info = model_info
        self.runtime_config = runtime_config
        self.started_at = utc_now()
        self.steps: list[dict[str, Any]] = []
        self.exceptions: list[dict[str, Any]] = []
        self.run_dir.mkdir(parents=True, exist_ok=False)
        (self.run_dir / "traces").mkdir()

    def add_step(self, step: dict[str, Any]) -> None:
        self.steps.append({"recorded_at": utc_now(), **step})

    def add_exception(self, exc: BaseException, *, phase: str) -> None:
        self.exceptions.append({
            "phase": phase,
            "type": type(exc).__name__,
            "message": redact_secrets(str(exc)),
            "traceback": redact_secrets("".join(traceback.format_exception(exc))),
        })

    def finish(
        self,
        *,
        status: str,
        status_reason: str | None,
        session_id: str | None,
        initial_state: dict[str, Any] | None,
        final_state: dict[str, Any] | None,
    ) -> dict[str, Any]:
        trace_files = list((self.run_dir / "traces").glob("*.jsonl"))
        trace_events = read_jsonl_files(trace_files)
        trace_context = {"case_id": self.loaded_case.case.case_id, "run_id": self.run_id, "session_id": session_id}
        # Runtime run_id 是每轮调用 ID，不能覆盖；在评估层另外绑定整次执行身份。
        trace_events = [{"evaluation_context": dict(trace_context), **event} for event in trace_events]
        audit_events = (final_state or {}).get("tables", {}).get("events", [])
        completeness = self._completeness(initial_state, final_state, trace_events)
        effective_status = status
        effective_reason = status_reason
        if status == "COMPLETED_AWAITING_SCORING" and not completeness["complete"]:
            effective_status = "EVIDENCE_INCOMPLETE"
            effective_reason = ", ".join(completeness["missing"])

        evidence = {
            "schema_version": "eval-evidence-1.0",
            "case": {
                "case_id": self.loaded_case.case.case_id,
                "category": self.loaded_case.case.category,
                "task": list(self.loaded_case.case.task),
                "initial_state": self.loaded_case.case.initial_state,
                "success_criteria": list(self.loaded_case.case.success_criteria),
            },
            "scenario": asdict(self.loaded_case.scenario),
            "fixture_spec": asdict(self.loaded_case.fixture),
            "run": {
                "run_id": self.run_id,
                "session_id": session_id,
                "started_at": self.started_at,
                "finished_at": utc_now(),
                "status": effective_status,
                "status_reason": effective_reason,
                "model": self.model_info,
                "runtime_config": self.runtime_config,
            },
            "steps": self.steps,
            "business_state": {
                "before": initial_state,
                "after": final_state,
                "delta": state_delta(initial_state, final_state) if initial_state and final_state else None,
            },
            "trace": {
                "context": trace_context,
                "files": [str(path.relative_to(self.run_dir)) for path in trace_files],
                "events": trace_events,
            },
            "audit": {
                "source": "fixture.sqlite:events",
                "events": audit_events,
            },
            "exceptions": self.exceptions,
            "evidence_completeness": completeness,
        }
        self._write_outputs(evidence)
        return evidence

    def _completeness(
        self,
        initial_state: dict[str, Any] | None,
        final_state: dict[str, Any] | None,
        trace_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        missing: list[str] = []
        if initial_state is None:
            missing.append("before_business_state")
        if final_state is None:
            missing.append("after_business_state")
        expected_steps = len(self.loaded_case.scenario.steps)
        if len(self.steps) != expected_steps:
            missing.append(f"scenario_steps:{len(self.steps)}/{expected_steps}")
        user_steps = sum(step.get("type") == "user_message" for step in self.loaded_case.scenario.steps)
        traced_users = sum(event.get("event_type") == "USER_MESSAGE" for event in trace_events)
        if traced_users < user_steps:
            missing.append(f"USER_MESSAGE_trace:{traced_users}/{user_steps}")
        response_steps = sum(step.get("agent_output") is not None for step in self.steps)
        traced_responses = sum(event.get("event_type") == "FINAL_RESPONSE" for event in trace_events)
        if traced_responses < response_steps:
            missing.append(f"FINAL_RESPONSE_trace:{traced_responses}/{response_steps}")
        return {"complete": not missing, "missing": missing}

    def _write_outputs(self, evidence: dict[str, Any]) -> None:
        def dump(name: str, value: Any) -> None:
            (self.run_dir / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        dump("evidence.json", evidence)
        dump("manifest.json", evidence["run"] | {"case_id": evidence["case"]["case_id"]})
        if evidence["business_state"]["before"] is not None:
            dump("before_state.json", evidence["business_state"]["before"])
        if evidence["business_state"]["after"] is not None:
            dump("after_state.json", evidence["business_state"]["after"])
        if evidence["exceptions"]:
            dump("errors.json", evidence["exceptions"])

        raw_records = [
            {"record_type": "run", **evidence["run"]},
            *({"record_type": "step", **step} for step in evidence["steps"]),
            *({"record_type": "trace", **event} for event in evidence["trace"]["events"]),
            *({"record_type": "audit", **event} for event in evidence["audit"]["events"]),
            {"record_type": "business_state_delta", **(evidence["business_state"]["delta"] or {})},
        ]
        with (self.run_dir / "raw.jsonl").open("w", encoding="utf-8") as stream:
            for record in raw_records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

        # 统一保存一个固定文件名，便于后续 Checker 不感知 Trace 日期分片。
        with (self.run_dir / "trace.jsonl").open("w", encoding="utf-8") as stream:
            for record in evidence["trace"]["events"]:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
