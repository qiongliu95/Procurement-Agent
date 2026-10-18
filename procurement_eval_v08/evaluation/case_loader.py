"""加载并校验 procurement_eval_v4 的 Case、执行脚本和 Fixture 规格。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AGENT_CASE_FIELDS = {
    "case_id",
    "category",
    "task",
    "initial_state",
    "success_criteria",
}


class CaseLoadError(ValueError):
    """正式评估集格式不满足 Harness 约定。"""


def resolve_case_dir(case_dir: str | Path | None = None, *, project_root: Path | None = None) -> Path:
    """显式参数优先，其次环境配置，最后兼容当前与旧版目录布局。"""
    configured = case_dir if case_dir is not None else os.getenv("PROCUREMENT_EVAL_CASE_DIR", "").strip()
    if configured:
        return Path(configured).resolve()
    root = project_root or Path(__file__).resolve().parents[1]
    candidates = (
        root.parent / "评估case" / "procurement_eval_v4",
        root.parent / "procurement_eval_v4",
    )
    return next((path.resolve() for path in candidates if (path / "agent_cases.json").is_file()), candidates[0].resolve())


@dataclass(frozen=True)
class AgentCase:
    case_id: str
    category: str
    task: tuple[str, ...]
    initial_state: str
    success_criteria: tuple[str, ...]


@dataclass(frozen=True)
class BackendCase:
    case_id: str
    category: str
    task: str
    initial_state: str
    success_criteria: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionScenario:
    case_id: str
    suite: str
    execution_kind: str
    steps: tuple[dict[str, Any], ...]
    source_readiness: str
    review_flags: tuple[str, ...]


@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    case_id: str
    source_status: str
    requirements: dict[str, Any]


@dataclass(frozen=True)
class LoadedCase:
    case: AgentCase
    scenario: ExecutionScenario
    fixture: FixtureSpec


@dataclass(frozen=True)
class LoadedBackendCase:
    case: BackendCase
    scenario: ExecutionScenario
    fixture: FixtureSpec


class CaseLoader:
    """只读加载正式 v4 文件，不把溯源索引或成功标准注入 Agent。"""

    def __init__(self, suite_dir: str | Path | None = None):
        self.suite_dir = resolve_case_dir(suite_dir)
        self._agent_cases = self._read_json("agent_cases.json")
        self._backend_cases = self._read_json("backend_cases.json")
        self._scenarios = self._read_json("execution_scenarios.json")
        self._fixtures = self._read_json("fixture_specs.json")
        self._validate_documents()

    def _read_json(self, name: str) -> dict[str, Any]:
        path = self.suite_dir / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CaseLoadError(f"缺少正式评估集文件：{path}") from exc
        except json.JSONDecodeError as exc:
            raise CaseLoadError(f"评估集 JSON 无法解析：{path}: {exc}") from exc
        if not isinstance(value, dict):
            raise CaseLoadError(f"评估集顶层必须是对象：{path}")
        return value

    def _validate_documents(self) -> None:
        cases = self._agent_cases.get("cases")
        backend_cases = self._backend_cases.get("cases")
        scenarios = self._scenarios.get("scenarios")
        fixtures = self._fixtures.get("fixtures")
        if not isinstance(cases, list):
            raise CaseLoadError("agent_cases.json 缺少 cases 数组")
        if not isinstance(backend_cases, list):
            raise CaseLoadError("backend_cases.json 缺少 cases 数组")
        if not isinstance(scenarios, dict):
            raise CaseLoadError("execution_scenarios.json 缺少 scenarios 对象")
        if not isinstance(fixtures, dict):
            raise CaseLoadError("fixture_specs.json 缺少 fixtures 对象")

        seen: set[str] = set()
        for raw in cases:
            if not isinstance(raw, dict) or set(raw) != AGENT_CASE_FIELDS:
                raise CaseLoadError("Agent Case 必须严格使用既定五字段")
            case_id = raw.get("case_id")
            if not isinstance(case_id, str) or not case_id or case_id in seen:
                raise CaseLoadError(f"Agent Case ID 无效或重复：{case_id!r}")
            seen.add(case_id)
            self._validate_string_list(raw.get("task"), f"{case_id}.task")
            self._validate_string_list(raw.get("success_criteria"), f"{case_id}.success_criteria")
            if case_id not in scenarios:
                raise CaseLoadError(f"{case_id} 缺少 execution scenario")
            fixture_id = raw.get("initial_state")
            if not isinstance(fixture_id, str) or fixture_id not in fixtures:
                raise CaseLoadError(f"{case_id} 引用的 Fixture 不存在：{fixture_id!r}")

        declared_count = self._agent_cases.get("case_count")
        if declared_count is not None and declared_count != len(cases):
            raise CaseLoadError("agent_cases.json 的 case_count 与实际数量不一致")

        backend_seen: set[str] = set()
        for raw in backend_cases:
            if not isinstance(raw, dict) or set(raw) != AGENT_CASE_FIELDS:
                raise CaseLoadError("Backend Case 必须严格使用既定五字段")
            case_id = raw.get("case_id")
            if not isinstance(case_id, str) or not case_id or case_id in backend_seen:
                raise CaseLoadError(f"Backend Case ID 无效或重复：{case_id!r}")
            backend_seen.add(case_id)
            if not isinstance(raw.get("task"), str) or not raw["task"]:
                raise CaseLoadError(f"{case_id}.task 必须是非空字符串")
            self._validate_string_list(raw.get("success_criteria"), f"{case_id}.success_criteria")
            scenario = scenarios.get(case_id)
            if not isinstance(scenario, dict) or scenario.get("suite") != "backend":
                raise CaseLoadError(f"{case_id} 缺少 Backend execution scenario")
            fixture_id = raw.get("initial_state")
            if not isinstance(fixture_id, str) or fixture_id not in fixtures:
                raise CaseLoadError(f"{case_id} 引用的 Fixture 不存在：{fixture_id!r}")
        backend_count = self._backend_cases.get("case_count")
        if backend_count is not None and backend_count != len(backend_cases):
            raise CaseLoadError("backend_cases.json 的 case_count 与实际数量不一致")

    @staticmethod
    def _validate_string_list(value: Any, label: str) -> None:
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise CaseLoadError(f"{label} 必须是非空字符串数组")

    def list_case_ids(self) -> list[str]:
        return [str(item["case_id"]) for item in self._agent_cases["cases"]]

    def list_backend_case_ids(self) -> list[str]:
        return [str(item["case_id"]) for item in self._backend_cases["cases"]]

    def load(self, case_id: str) -> LoadedCase:
        raw_case = next(
            (item for item in self._agent_cases["cases"] if item["case_id"] == case_id),
            None,
        )
        if raw_case is None:
            raise KeyError(f"未知 Agent Case：{case_id}")

        raw_scenario = self._scenarios["scenarios"][case_id]
        if not isinstance(raw_scenario, dict) or not isinstance(raw_scenario.get("steps"), list):
            raise CaseLoadError(f"{case_id} 的 execution scenario 无效")
        if raw_scenario.get("suite") != "agent":
            raise CaseLoadError(f"{case_id} 不是 Agent Case")

        fixture_id = raw_case["initial_state"]
        raw_fixture = self._fixtures["fixtures"][fixture_id]
        if not isinstance(raw_fixture, dict) or raw_fixture.get("case_id") != case_id:
            raise CaseLoadError(f"{case_id} 的 Fixture 关联不一致")

        # 用户消息必须与 Case.task 一致；Harness 动作只允许出现在 scenario 中。
        scenario_messages = [
            step.get("text") for step in raw_scenario["steps"]
            if isinstance(step, dict) and step.get("type") == "user_message"
        ]
        if scenario_messages != raw_case["task"]:
            raise CaseLoadError(f"{case_id} 的 task 与 execution scenario 用户消息不一致")

        return LoadedCase(
            case=AgentCase(
                case_id=case_id,
                category=str(raw_case["category"]),
                task=tuple(raw_case["task"]),
                initial_state=fixture_id,
                success_criteria=tuple(raw_case["success_criteria"]),
            ),
            scenario=ExecutionScenario(
                case_id=case_id,
                suite="agent",
                execution_kind=str(raw_scenario.get("execution_kind") or ""),
                steps=tuple(dict(step) for step in raw_scenario["steps"]),
                source_readiness=str(raw_scenario.get("readiness") or ""),
                review_flags=tuple(raw_scenario.get("review_flags") or ()),
            ),
            fixture=FixtureSpec(
                fixture_id=fixture_id,
                case_id=case_id,
                source_status=str(raw_fixture.get("status") or ""),
                requirements=dict(raw_fixture.get("requirements") or {}),
            ),
        )

    def load_backend(self, case_id: str) -> LoadedBackendCase:
        raw_case = next(
            (item for item in self._backend_cases["cases"] if item["case_id"] == case_id),
            None,
        )
        if raw_case is None:
            raise KeyError(f"未知 Backend Case：{case_id}")
        raw_scenario = self._scenarios["scenarios"][case_id]
        if not isinstance(raw_scenario, dict) or not isinstance(raw_scenario.get("steps"), list):
            raise CaseLoadError(f"{case_id} 的 execution scenario 无效")
        if raw_scenario.get("suite") != "backend":
            raise CaseLoadError(f"{case_id} 不是 Backend Case")
        fixture_id = raw_case["initial_state"]
        raw_fixture = self._fixtures["fixtures"][fixture_id]
        if not isinstance(raw_fixture, dict) or raw_fixture.get("case_id") != case_id:
            raise CaseLoadError(f"{case_id} 的 Fixture 关联不一致")
        return LoadedBackendCase(
            case=BackendCase(
                case_id=case_id,
                category=str(raw_case["category"]),
                task=str(raw_case["task"]),
                initial_state=fixture_id,
                success_criteria=tuple(raw_case["success_criteria"]),
            ),
            scenario=ExecutionScenario(
                case_id=case_id,
                suite="backend",
                execution_kind=str(raw_scenario.get("execution_kind") or ""),
                steps=tuple(dict(step) for step in raw_scenario["steps"]),
                source_readiness=str(raw_scenario.get("readiness") or ""),
                review_flags=tuple(raw_scenario.get("review_flags") or ()),
            ),
            fixture=FixtureSpec(
                fixture_id=fixture_id,
                case_id=case_id,
                source_status=str(raw_fixture.get("status") or ""),
                requirements=dict(raw_fixture.get("requirements") or {}),
            ),
        )
