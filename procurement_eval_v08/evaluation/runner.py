"""按正式 execution scenario 驱动真实 Agent Runtime。"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from agent_runtime import AgentRuntime, OpenAICompatibleModel, TraceWriter
from agent_tools import ProcurementApiClient

from .case_loader import CaseLoader, LoadedCase
from .evidence import EvidenceCollector
from .fixture import FixtureEnvironment, FixtureNotReady


class RunStatus(str, Enum):
    COMPLETED_AWAITING_SCORING = "COMPLETED_AWAITING_SCORING"
    ENVIRONMENT_NOT_READY = "ENVIRONMENT_NOT_READY"
    RUN_ERROR = "RUN_ERROR"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    NOT_RUN = "NOT_RUN"


ModelFactory = Callable[[LoadedCase], Any]


def load_env_file(path: str | Path) -> None:
    """加载简单 KEY=VALUE 配置；不覆盖调用方已显式设置的环境变量。"""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def sanitized_base_url(value: str) -> str:
    parts = urlsplit(value)
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def default_real_model_factory(_case: LoadedCase) -> tuple[Any, dict[str, Any]]:
    required = ("AGENT_MODEL_API_KEY", "AGENT_MODEL_BASE_URL", "AGENT_MODEL_NAME")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise FixtureNotReady("缺少模型配置：" + ", ".join(missing))
    return OpenAICompatibleModel.from_env(), {
        "adapter": "OpenAICompatibleModel",
        "model_name": os.environ["AGENT_MODEL_NAME"],
        "provider_base_url": sanitized_base_url(os.environ["AGENT_MODEL_BASE_URL"]),
        "temperature": 0,
        "real_model": True,
    }


def runtime_state(state: Any) -> dict[str, Any]:
    pending = state.pending_action
    return {
        "session_id": state.session_id,
        "active_task_type": state.active_task_type,
        "target_request_id": state.target_request_id,
        "slots": dict(state.slots),
        "missing_slots": list(state.missing_slots),
        "confirmation_state": state.confirmation_state,
        "pending_action": ({
            "action_id": pending.action_id,
            "run_id": pending.run_id,
            "task_type": pending.task_type,
            "target_request_id": pending.target_request_id,
            "args": pending.args,
            "summary": pending.summary,
            "fingerprint": pending.fingerprint,
        } if pending else None),
        "has_suspended_task": state.suspended_task is not None,
        "resume_prompt_pending": state.resume_prompt_pending,
    }


class EvaluationRunner:
    def __init__(
        self,
        loader: CaseLoader,
        runs_dir: str | Path,
        *,
        model_factory: ModelFactory = default_real_model_factory,
        runtime_overrides: dict[str, Any] | None = None,
    ):
        self.loader = loader
        self.runs_dir = Path(runs_dir).resolve()
        self.model_factory = model_factory
        self.runtime_overrides = dict(runtime_overrides or {})

    def run_many(self, case_ids: list[str]) -> list[dict[str, Any]]:
        return [self.run_case(case_id) for case_id in case_ids]

    def run_case(self, case_id: str) -> dict[str, Any]:
        loaded = self.loader.load(case_id)
        run_id = self._new_run_id(case_id)
        run_dir = self.runs_dir / run_id
        model_info: dict[str, Any] = {"real_model": False, "status": "NOT_INITIALIZED"}
        runtime_config = self._runtime_config()
        collector = EvidenceCollector(run_dir, run_id, loaded, model_info, runtime_config)
        fixture = FixtureEnvironment(loaded, run_dir / "fixture.sqlite")
        initial_state = None
        final_state = None
        session = None
        self._superseded_confirmation_ids: list[str] = []
        status = RunStatus.NOT_RUN.value
        reason: str | None = None

        try:
            model_result = self.model_factory(loaded)
            if isinstance(model_result, tuple) and len(model_result) == 2:
                model, model_info = model_result
            else:
                model = model_result
                model_info = {
                    "adapter": type(model).__name__,
                    "model_name": type(model).__name__,
                    "real_model": False,
                }
            collector.model_info = model_info

            fixture.prepare()
            initial_state = fixture.initial_state
            runtime = AgentRuntime(
                model,
                ProcurementApiClient(fixture.base_url, "demo-agent", runtime_config["http_timeout_seconds"]),
                TraceWriter(run_dir / "traces"),
                model_name=str(model_info.get("model_name") or "configured-model"),
                confirm_ttl_seconds=runtime_config["confirm_ttl_seconds"],
                max_model_steps=runtime_config["max_model_steps"],
                write_retry_limit=runtime_config["write_retry_limit"],
                conversation_turns=runtime_config["conversation_turns"],
                model_context_messages=runtime_config["model_context_messages"],
                session_history_messages=runtime_config["session_history_messages"],
            )
            session = runtime.new_session(f"eval-{run_id}")

            for index, step in enumerate(loaded.scenario.steps, start=1):
                self._execute_step(collector, runtime, session, index, step)
            status = RunStatus.COMPLETED_AWAITING_SCORING.value
        except FixtureNotReady as exc:
            status = RunStatus.ENVIRONMENT_NOT_READY.value
            reason = str(exc)
            collector.add_exception(exc, phase="environment")
        except Exception as exc:
            status = RunStatus.RUN_ERROR.value
            reason = f"{type(exc).__name__}: {exc}"
            collector.add_exception(exc, phase="execution")
        finally:
            if fixture.store is not None and fixture.db_path.exists():
                try:
                    final_state = fixture.capture_final_state()
                except Exception as exc:
                    collector.add_exception(exc, phase="final_state")
                    if status == RunStatus.COMPLETED_AWAITING_SCORING.value:
                        status = RunStatus.EVIDENCE_INCOMPLETE.value
                        reason = "最终业务状态采集失败"
            fixture.close()

        return collector.finish(
            status=status,
            status_reason=reason,
            session_id=session.session_id if session else None,
            initial_state=initial_state,
            final_state=final_state,
        )

    def _execute_step(
        self,
        collector: EvidenceCollector,
        runtime: AgentRuntime,
        session: Any,
        index: int,
        step: dict[str, Any],
    ) -> None:
        step_type = step.get("type")
        before = runtime_state(session)
        if step_type == "user_message":
            text = step.get("text")
            if not isinstance(text, str):
                raise ValueError(f"步骤 {index} 的 user_message 缺少 text")
            if session.pending_action is not None:
                self._superseded_confirmation_ids.append(session.pending_action.action_id)
            response = runtime.handle_message(session, text)
            collector.add_step({
                "step_index": index,
                "step_type": step_type,
                "user_input": text,
                "agent_output": response,
                "runtime_state_before": before,
                "runtime_state_after": runtime_state(session),
            })
            return
        if step_type == "harness_action":
            action = step.get("action")
            response: dict[str, Any] | None = None
            action_status = "EXECUTED"
            if action in {
                "cancel_latest_confirmation", "confirm_latest_confirmation",
                "cancel_latest", "confirm_latest", "confirm_if_offered",
            }:
                pending = session.pending_action
                if pending is None:
                    action_status = "NO_PENDING_CONFIRMATION"
                else:
                    decision = "cancel" if action in {"cancel_latest_confirmation", "cancel_latest"} else "confirm"
                    response = runtime.confirm(session, pending.action_id, decision)
            elif action == "attempt_old_confirmation":
                if not self._superseded_confirmation_ids:
                    action_status = "NO_OLD_CONFIRMATION"
                else:
                    response = runtime.confirm(session, self._superseded_confirmation_ids[0], "confirm")
            else:
                raise FixtureNotReady(f"本轮尚未实现 Harness 动作：{action}")
            collector.add_step({
                "step_index": index,
                "step_type": step_type,
                "harness_action": action,
                "action_status": action_status,
                "agent_output": response,
                "runtime_state_before": before,
                "runtime_state_after": runtime_state(session),
            })
            return
        if step_type == "checkpoint":
            collector.add_step({
                "step_index": index,
                "step_type": step_type,
                "checkpoint": dict(step),
                "runtime_state_before": before,
                "runtime_state_after": runtime_state(session),
            })
            return
        raise FixtureNotReady(f"本轮尚未实现 scenario step：{step_type}")

    def _runtime_config(self) -> dict[str, Any]:
        config = {
            "confirm_ttl_seconds": int(os.getenv("AGENT_CONFIRM_TTL_SECONDS", "600")),
            "max_model_steps": int(os.getenv("AGENT_MAX_MODEL_STEPS", "6")),
            "http_timeout_seconds": float(os.getenv("AGENT_HTTP_TIMEOUT_SECONDS", "30")),
            "write_retry_limit": int(os.getenv("AGENT_WRITE_RETRY_LIMIT", "1")),
            "conversation_turns": int(os.getenv("AGENT_CONVERSATION_TURNS", "12")),
            "model_context_messages": int(os.getenv("AGENT_MODEL_CONTEXT_MESSAGES", "6")),
            "session_history_messages": int(os.getenv("AGENT_SESSION_HISTORY_MESSAGES", "200")),
        }
        config.update(self.runtime_overrides)
        return config

    @staticmethod
    def _new_run_id(case_id: str) -> str:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        safe_case_id = re.sub(r"[^A-Za-z0-9_-]", "-", case_id)
        return f"{safe_case_id}-{timestamp}-{uuid.uuid4().hex[:8]}"
