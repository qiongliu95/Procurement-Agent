"""采购 Agent v1：模型负责理解与只读探索，Runtime 负责安全状态机与写入。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from agent_tools import (
    HISTORY_FIELDS,
    MODEL_TOOL_SCHEMAS,
    READ_TOOL_NAMES,
    ProcurementApiClient,
    ProcurementApiError,
    ProcurementTransportError,
    model_safe_result,
)
from knowledge_rag import KnowledgeIndexError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelTurn:
    content: str | None
    tool_calls: list[ModelToolCall]
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskContract:
    """模型的任务解释，不承载事实或授权；更新约束不依赖用户话术关键词。"""
    intent: str
    dialogue_relation: str = "unclear"
    target_object: str = "unknown"
    query_scope: str = "single"
    answer_status: str = "ready"

    @classmethod
    def from_proposal(cls, proposal: dict[str, Any]) -> "TaskContract":
        contract = cls(**{name: proposal[name] for name in cls.__dataclass_fields__ if name in proposal})
        if contract.intent not in {"create", "query", "update", "submit", "consult", "ambiguous", "unsupported"}:
            raise ValueError("INVALID_INTENT")
        if contract.dialogue_relation not in {
            "new_task", "follow_up", "supplement", "correction", "confirm", "cancel",
            "switch_task", "pause", "resume", "unclear",
        } or contract.target_object not in {"request", "purchase_history", "product", "knowledge", "current_task", "unknown"}:
            raise ValueError("INVALID_DIALOGUE_CONTEXT")
        if contract.query_scope not in {"single", "collection"} or (
            contract.query_scope == "collection" and (contract.intent != "query" or contract.target_object != "request")
        ):
            raise ValueError("INVALID_QUERY_SCOPE")
        if contract.answer_status not in {"ready", "needs_input", "evidence_insufficient"}:
            raise ValueError("INVALID_ANSWER_STATUS")
        return contract

    def create_slot_policy(self, *, accepted_operation: str | None = None) -> str:
        """仅映射已核准的操作；调用者必须先过候选绑定门禁，不解析商品词形。"""
        if accepted_operation in {"start", "switch"}:
            return "replace"
        if accepted_operation in {"supplement", "resume"}:
            return "merge"
        return "clarify"

    def read_completion_issue(
        self, goal: dict[str, Any], domains: set[str], *, accepted_state: SessionState | None = None,
    ) -> str | None:
        """必要条件检查；None也不是回答覆盖或用户目标完成的充分证明。"""
        if self.answer_status != "ready":
            return "READ_GOAL_NEEDS_INPUT"
        expected = goal.get("interpretation") or {}
        if expected.get("target_object", self.target_object) != self.target_object:
            return "READ_GOAL_DOMAIN_CONFLICT"
        if expected.get("query_scope", self.query_scope) != self.query_scope:
            return "READ_GOAL_SCOPE_CONFLICT"
        if self.target_object != "unknown" and self.target_object not in domains:
            return "CONSULTATION_SOURCE_REQUIRED"
        if (accepted_state is None or accepted_state.active_task_type != self.intent
                or not self._has_origin(accepted_state, accepted_state.task_origin, self.intent)):
            return "READ_GOAL_SOURCE_UNVERIFIED"
        accepted = accepted_state.task_origin["interpretation"]
        if accepted["target_object"] != self.target_object:
            return "READ_GOAL_DOMAIN_CONFLICT"
        if accepted["query_scope"] != self.query_scope:
            return "READ_GOAL_SCOPE_CONFLICT"
        return None

    @staticmethod
    def _state_fingerprint(state: SessionState) -> str:
        # 排除候选澄清及读缓存；版本、确认、正式槽位和UNKNOWN等必须参与并发校验。
        fields = ("active_task_type", "target_request_id", "slots", "missing_slots", "last_read_version",
                  "task_messages", "task_origin", "pending_action", "confirmation_state", "operation_key",
                  "suspended_task", "resume_prompt_pending", "resume_proposal", "retry_submit_request_id",
                  "uncertain_request_id", "completed_operations")
        view = asdict(state)
        return hashlib.sha256(json.dumps({k: view[k] for k in fields}, sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _has_origin(state: SessionState, origin: dict[str, Any] | None, intent: str | None = None) -> bool:
        return bool(origin and origin.get("kind") == "user_task_choice"
                    and origin.get("session_id") == state.session_id and origin.get("actor") == state.actor_identity
                    and origin.get("candidate_id") and origin.get("source_turn_id")
                    and isinstance(origin.get("interpretation"), dict)
                    and (intent is None or origin["interpretation"].get("intent") == intent))

    @staticmethod
    def _decision(reason: str, message: str, *, accepted: bool = False, stop: bool = False) -> dict[str, Any]:
        return {"route_action": "STOP" if stop else "ASK", "route_reason_code": reason,
                "message": message, "task_accepted": accepted, "confirmation": None,
                "business_write_authorized": False, "user_goal_satisfied": None}

    def stage_candidate(self, state: SessionState, proposal: dict[str, Any]) -> dict[str, Any]:
        """隔离原型：仅存候选澄清；handle_message未接入，模型无此工具入口。"""
        if self != self.from_proposal(proposal):
            raise ValueError("CANDIDATE_CONTRACT_MISMATCH")
        if not state.current_run_id or not state.conversation_tail or state.conversation_tail[-1]["role"] != "user":
            raise ValueError("USER_TURN_REQUIRED")
        allowed = (*self.__dataclass_fields__, "request_id", "item", "quantity", "unit_price",
                   "supplier_name", "purpose", "relative_quantity_delta", "request_quote", "field_sources")
        frozen = json.loads(json.dumps({k: v for k, v in proposal.items() if k in allowed}, ensure_ascii=False))
        prior = state.clarification or {}
        if prior.get("kind") == "task_candidate":
            prior = prior.get("prior_clarification") or {}
        candidate = {"candidate_id": "candidate-" + uuid.uuid4().hex, "session_id": state.session_id,
                     "actor": state.actor_identity, "source_turn_id": state.current_run_id,
                     "source_message_index": len(state.conversation_tail) - 1,
                     "user_message": state.conversation_tail[-1]["content"], "proposal": frozen,
                     "base_fingerprint": self._state_fingerprint(state), "expires_at": time.time() + 600}
        # 保留展示快照及摘要指纹；用户选择必须指向同一个候选，而不是泛化的“同意”。
        candidate["fingerprint"] = hashlib.sha256(json.dumps(candidate, sort_keys=True,
                                                             ensure_ascii=False).encode()).hexdigest()
        question = "候选任务理解（尚未接受，也未授权写入）：" + json.dumps(frozen, ensure_ascii=False)
        question += "。请明确选择新任务、补充原任务、切换、暂停、恢复或不接受。"
        state.clarification = {"kind": "task_candidate", "candidate": candidate,
                               "question": question, "prior_clarification": json.loads(json.dumps(prior))}
        return {**self._decision("TASK_SOURCE_CHOICE_REQUIRED", question), "candidate_id": candidate["candidate_id"]}

    def _candidate_issue(self, state: SessionState, candidate_id: str) -> str | None:
        pending = state.clarification or {}
        candidate = pending.get("candidate") or {}
        if pending.get("kind") != "task_candidate" or candidate.get("candidate_id") != candidate_id:
            return "TASK_CANDIDATE_NOT_FOUND"
        unsigned = {k: v for k, v in candidate.items() if k != "fingerprint"}
        intact = hashlib.sha256(json.dumps(unsigned, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if (intact != candidate["fingerprint"] or candidate["session_id"] != state.session_id
                or candidate["actor"] != state.actor_identity or candidate["expires_at"] < time.time()
                or candidate["source_turn_id"] != state.current_run_id
                or candidate["source_message_index"] >= len(state.conversation_tail)
                or state.conversation_tail[candidate["source_message_index"]] != {"role": "user", "content": candidate["user_message"]}
                or candidate["base_fingerprint"] != self._state_fingerprint(state)
                or self != self.from_proposal(candidate["proposal"])):
            return "TASK_SOURCE_STALE"
        return None

    @staticmethod
    def _working_origin(state: SessionState) -> bool:
        origin = state.task_origin or {}
        return bool(state.active_task_type and origin.get("kind") in {"user_task_choice", "validated_model_state"}
                    and origin.get("session_id") == state.session_id and origin.get("actor") == state.actor_identity
                    and origin.get("candidate_id") and origin.get("source_turn_id")
                    and (origin.get("interpretation") or {}).get("intent") == state.active_task_type)

    def commit_candidate(self, state: SessionState, candidate_id: str, operation: str) -> dict[str, Any]:
        """仅供可信宿主交付用户选择；不得把模型输出映射成此事件。无业务执行副作用。"""
        issue = self._candidate_issue(state, candidate_id)
        if issue:
            return self._decision(issue, "候选、任务或来源已变化，请重新澄清。")
        pending = state.clarification
        candidate = pending["candidate"]
        if operation == "reject":
            state.clarification = pending.get("prior_clarification") or None
            return self._decision("TASK_CANDIDATE_REJECTED", "未接受候选任务；原任务未改变，未执行写入。", stop=True)
        if state.pending_action or state.uncertain_request_id or state.retry_submit_request_id:
            return self._decision("EXISTING_RUNTIME_CONTROL_REQUIRED", "请先按原流程处理待确认或未决结果。")
        has_origin = self._working_origin(state)
        if operation == "start":
            valid = state.active_task_type is None and state.suspended_task is None
        elif operation == "switch":
            valid = has_origin and state.suspended_task is None
        elif operation == "supplement":
            valid = has_origin and state.active_task_type == self.intent
        elif operation == "pause":
            valid = has_origin and state.active_task_type in {"create", "update", "submit"} and state.suspended_task is None
            valid = valid and self.intent in {"query", "consult"}
        elif operation == "resume":
            snapshot = state.suspended_task
            valid = bool(snapshot and self._has_origin(state, snapshot.task_origin, snapshot.task_type) and snapshot.task_type == self.intent)
            valid = valid and state.active_task_type is None
        else:
            valid = False
        if not valid or self.intent in {"ambiguous", "unsupported"}:
            return self._decision("TASK_SOURCE_CHOICE_CONFLICT", "任务来源或所选关系不匹配，请继续澄清。")
        return self._apply_task_update(state, candidate, operation)

    def _apply_task_update(
        self, state: SessionState, candidate: dict[str, Any], operation: str, *,
        fields: dict[str, Any] | None = None, source_kind: str = "user_task_choice",
    ) -> dict[str, Any]:
        if fields is None:
            fields = {k: v for k, v in candidate["proposal"].items()
                      if k in {"item", "quantity", "unit_price", "supplier_name", "purpose", "relative_quantity_delta"}}
        if self.intent not in {"create", "update"}:
            fields = {}
        origin = {"kind": source_kind, "candidate_id": candidate["candidate_id"], "session_id": state.session_id,
                  "actor": state.actor_identity, "source_turn_id": candidate["source_turn_id"],
                  "user_message": candidate["user_message"], "operation": operation, "interpretation": asdict(self)}
        if source_kind == "validated_model_state":
            origin["independent_intent_verified"] = False
            origin["field_sources"] = {
                **((state.task_origin or {}).get("field_sources", {}) if operation == "supplement" else {}),
                **{k: candidate["proposal"]["field_sources"][k] for k in fields},
            }
        if operation == "pause":
            state.suspended_task = TaskSnapshot(state.active_task_type, state.target_request_id, dict(state.slots),
                                               list(state.missing_slots), state.last_read_version,
                                               list(state.task_messages), json.loads(json.dumps(state.task_origin)))
            state.active_task_type = None
            state.slots, state.missing_slots, state.task_messages = {}, [], []
            state.task_origin = None
            state.resume_prompt_pending = True
        else:
            if operation == "resume":
                snapshot = state.suspended_task
                state.slots = dict(snapshot.slots)
                state.task_messages = list(snapshot.task_messages)
                state.target_request_id = snapshot.target_request_id
                state.suspended_task, state.resume_proposal = None, None
                state.resume_prompt_pending = False
            elif self.create_slot_policy(accepted_operation=operation) == "replace":
                state.slots, state.task_messages = {}, []
                state.target_request_id = None
            state.slots.update(fields)
            if "quantity" in fields and "relative_quantity_delta" not in fields:
                state.slots.pop("relative_quantity_delta", None)
            state.active_task_type = self.intent
            if self.intent != "create":
                state.target_request_id = candidate["proposal"].get("request_id", state.target_request_id)
            state.task_origin = origin
            state.missing_slots = [k for k in AgentRuntime.REQUIRED_CREATE if k not in state.slots] if self.intent == "create" else []
        # 提交任务不恢复旧版本/确认/匹配缓存，后续仍须原prepare和confirm。
        state.last_read_version = None
        state.supplier_matches, state.supplier_match_runs = {}, {}
        if operation == "pause":
            state.target_request_id = None
        state.clarification = None
        return self._decision("TASK_SOURCE_ACCEPTED", "任务方向已接受，尚未执行业务写入；仍需补全校验和具体操作确认。", accepted=True)

    def validate_auto_candidate(self, state: SessionState, candidate_id: str) -> dict[str, Any]:
        """只验证可检查的关系/出处；不是自由文本意图分类，也不产生用户授权。"""
        def blocked(reason: str) -> dict[str, Any]:
            return {"eligible": False, "reason": reason}

        issue = self._candidate_issue(state, candidate_id)
        if issue:
            return blocked(issue)
        if state.pending_action or state.uncertain_request_id or state.retry_submit_request_id:
            return blocked("EXISTING_RUNTIME_CONTROL_REQUIRED")
        candidate = state.clarification["candidate"]
        proposal, message = candidate["proposal"], candidate["user_message"]
        change_fields = (*AgentRuntime.CHANGE_FIELDS, "relative_quantity_delta")
        evidence_domains = {"search_purchase_history": "purchase_history", "search_products": "product",
                            "search_knowledge": "knowledge", "get_procurement_rules": "knowledge"}
        current_evidence = [e for e in state.consultation_evidence if e.get("run_id") == state.current_run_id]
        if self.intent in {"query", "consult"}:
            if any(k in proposal for k in change_fields):
                return blocked("READ_PROPOSAL_HAS_WRITE_FIELDS")
            context = state.trusted_read_contexts.get(self.target_object) or {}
            domains = {evidence_domains.get(e.get("tool")) for e in current_evidence}
            reusable = self.dialogue_relation in {"follow_up", "correction", "supplement"}
            if self.target_object not in domains and not (context and (reusable or context.get("run_id") == state.current_run_id)):
                return blocked("CONSULTATION_SOURCE_REQUIRED")
            return {"eligible": True, "operation": "read_only", "fields": {}}
        if self.intent not in {"create", "update", "submit"}:
            return blocked("TASK_RELATION_REQUIRES_CLARIFICATION")
        # 仅有读证据不能提升为写任务；真实混合请求也在此冲突澄清，而不另做词路由。
        if current_evidence or any("run_id" not in e for e in state.consultation_evidence):
            return blocked("READ_WRITE_GOAL_CONFLICT")
        if state.suspended_task:
            return blocked("SUSPENDED_TASK_DECISION_REQUIRED")
        if state.active_task_type and not self._working_origin(state):
            return blocked("ACCEPTED_TASK_SOURCE_REQUIRED")
        if state.active_task_type is None:
            if self.dialogue_relation != "new_task":
                return blocked("TASK_RELATION_REQUIRES_CLARIFICATION")
            operation = "start"
        elif self.intent != state.active_task_type:
            return blocked("TASK_TYPE_CONFLICT")
        elif self.dialogue_relation in {"supplement", "correction"}:
            operation = "supplement"
        elif self.dialogue_relation in {"switch_task", "new_task"}:
            operation = "switch"
        else:
            return blocked("TASK_RELATION_REQUIRES_CLARIFICATION")
        if operation in {"start", "switch"}:
            quote = proposal.get("request_quote")
            if not isinstance(quote, str) or not quote.strip() or quote not in message:
                return blocked("TASK_REQUEST_SOURCE_REQUIRED")
        sources = proposal.get("field_sources") or {}
        if not isinstance(sources, dict):
            return blocked("INVALID_FIELD_SOURCES")
        fields = {}
        for key in change_fields:
            if key not in proposal:
                continue
            value, quote = proposal[key], sources.get(key)
            if not isinstance(quote, str) or not quote.strip() or quote not in message:
                if operation == "supplement" and value != state.slots.get(key):
                    return blocked("FIELD_SOURCE_REQUIRED")
                continue  # 切换时丢弃模型带回的无本轮来源旧字段，而非继承。
            if key in {"quantity", "relative_quantity_delta"}:
                if type(value) is not int or key == "quantity" and value <= 0:
                    return blocked("INVALID_FIELD_VALUE")
            elif not isinstance(value, str) or not value.strip():
                return blocked("INVALID_FIELD_VALUE")
            if key in {"quantity", "relative_quantity_delta", "unit_price"}:
                try:
                    number = Decimal(quote)
                    if not number.is_finite() or number != Decimal(str(value)):
                        return blocked("FIELD_VALUE_SOURCE_CONFLICT")
                    if key == "unit_price" and (number < 0 or number.as_tuple().exponent < -2):
                        return blocked("INVALID_FIELD_VALUE")
                except InvalidOperation:
                    return blocked("FIELD_VALUE_SOURCE_CONFLICT")
            elif key != "item" and value != quote:
                return blocked("FIELD_VALUE_SOURCE_CONFLICT")
            fields[key] = value
        if operation == "switch":
            old_quote = (state.task_origin or {}).get("field_sources", {}).get("item")
            if (self.intent != "create" or "item" not in fields or fields["item"] == state.slots.get("item")
                    or sources["item"] == old_quote):
                return blocked("TASK_SWITCH_OBJECT_REQUIRED")
        if operation == "supplement" and "item" in fields and fields["item"] != state.slots.get("item"):
            return blocked("TASK_OBJECT_CONFLICT")
        request_id = proposal.get("request_id")
        if self.intent == "create" and request_id:
            return blocked("TASK_OBJECT_CONFLICT")
        if self.intent in {"update", "submit"}:
            if not request_id or request_id not in state.known_requests or request_id not in message:
                return blocked("REQUEST_OBJECT_SOURCE_REQUIRED")
            if operation == "supplement" and request_id != state.target_request_id:
                return blocked("TASK_OBJECT_CONFLICT")
        return {"eligible": True, "operation": operation, "fields": fields}

    def auto_commit_candidate(self, state: SessionState, candidate_id: str) -> dict[str, Any]:
        """隔离自动入口：现场重验后应用；不接受模型提供eligible或伪造人的选择。"""
        validated = self.validate_auto_candidate(state, candidate_id)
        if not validated["eligible"]:
            return {**self._decision(validated["reason"], "候选与任务、来源或安全状态不一致；已保留原任务，请澄清。"),
                    "requires_task_choice": True}
        candidate = state.clarification["candidate"]
        if validated["operation"] == "read_only":
            state.clarification = state.clarification.get("prior_clarification") or None
            return {**self._decision("READ_CONTEXT_PRESERVED", "可信只读证据可供答复；原采购任务和字段保持不变。"),
                    "route_action": "READ", "requires_task_choice": False}
        result = self._apply_task_update(state, candidate, validated["operation"], fields=validated["fields"],
                                         source_kind="validated_model_state")
        result.update(route_reason_code="AUTO_STATE_COMMITTED", requires_task_choice=False,
                      independent_intent_verified=False,
                      message="可撤销临时采购参数已更新；请补齐缺项，业务写入仍须具体操作确认。")
        return result

    @staticmethod
    def read_attempt(runtime: AgentRuntime, state: SessionState, call: ModelToolCall, message: str) -> dict[str, Any]:
        """未接入的Trace协议原型：尝试/拒绝用proposed_tool_name，实际调用沿用原工具事件。"""
        runtime._record(state, "MODEL_TOOL_ATTEMPTED", proposed_tool_name=call.name, model_tool_call_id=call.id)
        try:
            if call.name not in READ_TOOL_NAMES:
                raise ValueError("FORBIDDEN_MODEL_TOOL")
            count = len(state.consultation_evidence)
            result = runtime._execute_read_tool(state, call.name, call.arguments, message)
            for evidence in state.consultation_evidence[count:]:
                evidence["run_id"] = state.current_run_id
            return result
        except (ValueError, KnowledgeIndexError, ProcurementApiError, ProcurementTransportError) as exc:
            runtime._record(state, "MODEL_TOOL_REJECTED", proposed_tool_name=call.name,
                            model_tool_call_id=call.id, reason=getattr(exc, "code", str(exc)))
            return {"error": getattr(exc, "code", str(exc))}


class OpenAICompatibleModel:
    def __init__(self, api_key: str, base_url: str, model: str):
        import httpx
        from openai import OpenAI

        # 显式提供客户端，兼容本地已安装的 openai/httpx 小版本组合。
        self.client = OpenAI(api_key=api_key, base_url=base_url, http_client=httpx.Client())
        self.model = model

    @classmethod
    def from_env(cls) -> "OpenAICompatibleModel":
        api_key = os.getenv("AGENT_MODEL_API_KEY", "").strip()
        base_url = os.getenv("AGENT_MODEL_BASE_URL", "").strip()
        model = os.getenv("AGENT_MODEL_NAME", "").strip()
        missing = [
            name for name, value in (
                ("AGENT_MODEL_API_KEY", api_key),
                ("AGENT_MODEL_BASE_URL", base_url),
                ("AGENT_MODEL_NAME", model),
            ) if not value
        ]
        if missing:
            raise RuntimeError("缺少模型配置：" + ", ".join(missing))
        return cls(api_key, base_url, model)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        request: dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            temperature=0,
        )
        if tools:
            request.update(tools=tools, tool_choice="auto")
        response = self.client.chat.completions.create(**request)
        message = response.choices[0].message
        calls: list[ModelToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ModelToolCall(call.id, call.function.name, arguments))
        usage = response.usage.model_dump() if response.usage else {}
        return ModelTurn(message.content, calls, usage)


class TraceWriter:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        record = {"timestamp": utc_now(), **event}
        path = self.directory / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
        with self.lock, path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


@dataclass
class PendingAction:
    action_id: str
    run_id: str
    actor: str
    task_type: str
    target_request_id: str
    args: dict[str, Any]
    supplier_match: dict[str, Any] | None
    expected_version: int | None
    old_snapshot: dict[str, Any] | None
    created_at: float
    expires_at: float
    fingerprint: str
    summary: str


@dataclass
class TaskSnapshot:
    """只保存恢复业务任务所需的确定性状态；查询过程和工具结果不进入快照。"""
    task_type: str
    target_request_id: str | None
    slots: dict[str, Any]
    missing_slots: list[str]
    last_read_version: int | None
    task_messages: list[dict[str, str]]
    task_origin: dict[str, Any] | None = None


@dataclass
class SessionState:
    session_id: str
    actor_identity: str = "U01"
    active_task_type: str | None = None
    target_request_id: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    last_read_version: int | None = None
    pending_action: PendingAction | None = None
    confirmation_state: str = "NONE"
    operation_key: str | None = None
    current_run_id: str | None = None
    conversation_tail: list[dict[str, str]] = field(default_factory=list)
    task_messages: list[dict[str, str]] = field(default_factory=list)
    suspended_task: TaskSnapshot | None = None
    clarification: dict[str, Any] | None = None
    resume_prompt_pending: bool = False
    resume_proposal: dict[str, Any] | None = None
    recent_request_ids: list[str] = field(default_factory=list)
    candidate_request_ids: list[str] = field(default_factory=list)
    known_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    supplier_matches: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    supplier_match_runs: dict[str, str] = field(default_factory=dict)
    consultation_evidence: list[dict[str, Any]] = field(default_factory=list)
    trusted_read_contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    turn_read_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    completed_operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    retry_submit_request_id: str | None = None
    uncertain_request_id: str | None = None
    write_started: bool = False
    task_origin: dict[str, Any] | None = None


SYSTEM_PROMPT = """你是采购 Agent v1 的语言理解与只读探索模块，固定代表 U01。
你需要结合用户目标、会话状态和工具结果选择必要的只读工具，并决定还需澄清什么。
支持且只支持：新建、查询、修改、提交已有 DRAFT，以及商品目录、采购规则、操作指南、通用采购知识和历史成交价咨询。

先判断用户当前话语的沟通目标和操作承诺，再选择工具：
- create：用户明确要求发起、建立或办理采购申请；口语表达同样有效。缺少申请字段时仍是 create，补问字段即可。
- update：用户明确要求改变已有申请。目标申请或新值不完整时仍是 update，先读取候选或补问。
- query：用户询问申请事实、状态，或询问某申请是否可修改；询问可行性本身不是 update。
- consult：用户在计划、比较、了解价格或制度，尚未要求创建或修改业务对象。
- ambiguous：只有连用户要咨询还是发起业务操作都无法确定时使用；不能因为字段缺失或对象不唯一就改成 ambiguous。
- unsupported：明确要求取消、审批、创建 PO 等支持范围外的操作。

语义对照：
- “打算买/先了解价格/预算够不够”是 consult；不得收集正式申请必填字段或生成写确认。
- “帮我弄一张采购申请/我要申请采购”是 create；即使字段不全，也直接进入 create 补问。
- “我要采购两台电脑”没有说明是咨询还是发起申请，属于 ambiguous，只问一次目标。
- “把申请的数量改成 3 台”是 update；申请不唯一时先 list_requests，再以 update 提出最少澄清。
- “申请数量填错了”只是在描述问题，没有提出修改动作，属于 ambiguous 或 consult；先问用户是要查询还是修改，不生成确认卡。
- “申请还能改吗”是 query 或 consult；可以读取状态，但不能提出 update 或确认卡。
- “下单”可能指发起申请或创建 PO，属于 ambiguous；说明 Agent 不创建 PO，并询问是否发起采购申请。
- 同一句中先描述计划、后明确要求创建时，以明确的操作要求为准；不要先调用咨询工具。

规则：
1. 查询业务事实时主动使用只读工具；不要凭对话历史猜状态。
2. 新建员工只需提供 item、quantity、purpose。Runtime自动读取商品目录的唯一有效供应商和正式采购单价；缺失时阻断并提示联系采购负责人维护，不向员工补问单价/供应商。
3. 修改前必须 get_request；相对数量通过 relative_quantity_delta 表达。
4. 提交已有草稿前必须 get_request，且只能提出 submit 意图；不得改为新建申请。
5. 只有用户确实在咨询时才调用对应只读工具：商品目录调用 search_products，历史成交价调用 search_purchase_history；公司采购制度、操作指南和通用采购知识统一调用 search_knowledge；
   历史价格不是实时市场价，回答必须带申请/PO或规则文档来源。咨询本身不能提出写入。
6. 对象不唯一或字段含糊时，在 propose_task_state.question 中提出最少必要问题，同时保留已经明确的 create/query/update 意图。
7. 不得请求或提供 user_id、actor、role、Token、budget_id、supplier_id、expected_version、确认 ID。
8. 不得尝试审批、核验凭证、供应商准入、创建 PO、访问 __eval__ 或任意 HTTP。
9. 完成只读探索后必须调用 propose_task_state；它只提交理解结果，不执行写操作。
10. 不要声称写入成功；确认、写入和最终核验都由 Runtime 完成。
11. 用户给出的预算上限不是单价；新采购任务不得静默复用上一任务的单价、供应商或用途。
12. 不要为补齐 create 字段而查询历史价格或制度；咨询工具不是创建字段的推断来源。
13. “它/刚才那个/第二个”等指代只能根据 Runtime 给出的最近对象和当前候选解析；用户明确给出新申请编号时以新编号为准。无法唯一定位就 ASK，不猜测。
14. 存在暂停任务时，当前查询仍须独立完成；是否恢复或放弃由 Runtime 处理，模型不得把查询结果写回暂停任务参数。
15. 当前活动任务为 create 时，用户补充数量或用途是在继续原采购，不是商品咨询；合并同一任务已收集字段，不把补参无依据解释成换商品。只在用户明确要求查询/咨询时使用咨询工具。
16. 意图澄清尚未结束的话题应参考 pending_clarification 中商品数量等候选信息；不支持的临时插曲不等于放弃采购。候选信息不是写入授权；明确发起后再进入创建，不把调整未落库商品数量变成修改历史申请。不复用已结束/取消任务的可写信息。
17. 单条目采购必须唯一对应现有商品目录记录。旧参考价/历史成交价不能充当有效采购价，员工口头供应商和单价不能覆盖目录；不要额外查询历史/知识补价。目录自动补齐由Runtime执行，无需模型重复检索。
18. 当前 create 的 target_request_id 只是未落库草稿标识时，调整商品/数量/用途仍提出 create，不调用 get_request/update；Runtime 作废旧确认、重读目录并重新生成摘要。员工报价和供应商不能覆盖目录。
19. 用户同时给出单价、数量和总金额而互相冲突时，必须保留已知字段并提问，不生成写确认，也不静默选择一个金额。
20. purpose 使用用户明确提供的用途原文，不扩写为“使用”等额外词，不把旧任务用途改写后复用。确认字段忠实于当前用户，历史槽位只属于同一未结束任务。
21. 每次 propose_task_state 同时给出 dialogue_relation 和 target_object。业务 intent 与对话关系分开判断；历史成交、商品目录、知识制度属于 consult，不得误报为采购申请 query。
22. 连续追问可引用 Runtime 提供的 trusted_read_contexts；省略原查询词而需要重查时，对相应只读工具设置 use_previous_query=true。不同对象类型的证据不得混用。
23. 对历史成交、商品目录或知识制度结果继续询问其中已展示字段时，intent 仍为 consult，dialogue_relation 为 follow_up，target_object 保持原对象类型。只有用户明确要求查询采购申请当前状态，或明确给出/选择申请编号时，才使用 query + request。
24. 查询全部采购申请及进度时，使用 list_requests，提出 query + request + query_scope=collection；不要要求用户选择一笔。单笔查询默认 single，修改和提交始终要求唯一申请。
25. 用户纠正上一只读答复时使用 correction，并根据对应 trusted_read_contexts 恢复对象类型与查询范围；纠错不等于发起修改，也不授予写入。
26. 追问申请预算不足、部门余额或缺口时，使用 get_request_budget 并提出 query + request + query_topic=budget；关联预算来自申请，不向用户补问个人预算上限。每轮重读当前余额，金额与时点以工具为准，不将当前缺口说成提交时缺口；普通状态查询仍为status。
"""


class AgentRuntime:
    CONFIRM_WORDS = {"确认", "确认执行", "执行", "提交", "同意"}
    CANCEL_WORDS = {"取消", "不执行", "拒绝"}
    RESUME_WORDS = {"继续", "继续办理", "继续刚才的采购申请", "是，继续", "要继续"}
    ABANDON_WORDS = {"不继续", "放弃", "取消", "放弃刚才的采购申请", "不用了", "不办了", "取消当前任务", "放弃当前任务", "取消这个任务"}
    WRITE_TASKS = {"create", "update", "submit"}
    SUBMIT_TERMINAL_STATES = {
        "SUBMITTED", "PENDING_HIGH_APPROVAL", "PENDING_VENDOR_REVIEW",
        "BLOCKED_VENDOR", "BLOCKED_BUDGET", "NEED_INFO",
    }
    RETRY_SUBMIT_WORDS = {"重新提交", "重提", "是", "同意", "继续", "是，重新提交"}
    KEEP_DRAFT_WORDS = {"暂不提交", "不提交", "不继续", "否", "不用了"}
    REQUIRED_CREATE = ("item", "quantity", "purpose")
    CHANGE_FIELDS = ("item", "quantity", "unit_price", "supplier_name", "purpose")

    def __init__(
        self,
        model: Any,
        api: ProcurementApiClient,
        trace_writer: TraceWriter,
        *,
        model_name: str = "configured-model",
        confirm_ttl_seconds: int = 600,
        max_model_steps: int = 6,
        write_retry_limit: int = 1,
        conversation_turns: int = 12,
        model_context_messages: int = 6,
        session_history_messages: int = 200,
        knowledge_index: Any | None = None,
    ):
        self.model = model
        self.api = api
        self.trace = trace_writer
        self.model_name = model_name
        self.confirm_ttl_seconds = confirm_ttl_seconds
        self.max_model_steps = max_model_steps
        self.write_retry_limit = write_retry_limit
        # 保留旧配置作为兼容上限，但模型只接收任务相关的小窗口。
        self.model_context_messages = min(max(2, model_context_messages), max(2, conversation_turns * 2))
        self.session_history_messages = max(20, session_history_messages)
        self.knowledge_index = knowledge_index

    def new_session(self, session_id: str | None = None) -> SessionState:
        return SessionState(session_id or ("sess-" + uuid.uuid4().hex))

    def public_session(self, state: SessionState) -> dict[str, Any]:
        if state.pending_action and time.time() > state.pending_action.expires_at:
            self._invalidate_confirmation(state, "CONFIRMATION_EXPIRED")
        pending = state.pending_action
        return {
            "session_id": state.session_id,
            "actor": state.actor_identity,
            "active_task_type": state.active_task_type,
            "target_request_id": state.target_request_id,
            "confirmation_state": state.confirmation_state,
            "messages": list(state.conversation_tail),
            "has_suspended_task": state.suspended_task is not None,
            "resume_prompt_pending": state.resume_prompt_pending,
            "clarification": json.loads(json.dumps(state.clarification, ensure_ascii=False)),
            "pending_confirmation": ({
                "action_id": pending.action_id,
                "summary": pending.summary,
                "expires_at": datetime.fromtimestamp(
                    pending.expires_at, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
            } if pending else None),
        }

    def _record(self, state: SessionState, event_type: str, **details: Any) -> None:
        self.trace.write({
            "event_type": event_type,
            "run_id": details.pop("run_id", state.current_run_id),
            "session_id": state.session_id,
            "actor": state.actor_identity,
            "task_type": state.active_task_type,
            **details,
        })

    def _route(self, state: SessionState, action: str, reason: str, **details: Any) -> None:
        self._record(state, "ROUTE_DECIDED", route_action=action, route_reason_code=reason, **details)

    @staticmethod
    def _fingerprint_payload(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _pending_fingerprint(self, pending: PendingAction) -> str:
        return self._fingerprint_payload({
            "actor": pending.actor,
            "task_type": pending.task_type,
            "target_request_id": pending.target_request_id,
            "args": pending.args,
            "supplier_match": pending.supplier_match,
            "expected_version": pending.expected_version,
        })

    def _append_message(self, state: SessionState, role: str, content: str) -> None:
        state.conversation_tail.append({"role": role, "content": content})
        state.conversation_tail = state.conversation_tail[-self.session_history_messages:]

    def _remember_task_message(self, state: SessionState, role: str, content: str) -> None:
        if state.task_messages and state.task_messages[-1] == {"role": role, "content": content}:
            return
        state.task_messages.append({"role": role, "content": content})
        state.task_messages = state.task_messages[-self.model_context_messages:]

    @staticmethod
    def _remember_reference(state: SessionState, request_id: str) -> None:
        """最近对象按使用顺序去重，供“它/刚才那个/第二个”解析。"""
        state.recent_request_ids = [item for item in state.recent_request_ids if item != request_id]
        state.recent_request_ids.append(request_id)
        state.recent_request_ids = state.recent_request_ids[-5:]

    def _remember_read_context(
        self, state: SessionState, object_type: str, tool_name: str,
        query: dict[str, Any], data: dict[str, Any],
    ) -> None:
        """只保存工具返回的净化证据；模型回答不会成为后续业务事实。"""
        context = {
            "object_type": object_type,
            "tool": tool_name,
            "query": dict(query),
            "data": json.loads(json.dumps(data, ensure_ascii=False)),
            "run_id": state.current_run_id,
            "captured_at": utc_now(),
        }
        # 每种对象只保留最近一次可信结果，避免跨任务候选无限积累。
        state.trusted_read_contexts.pop(object_type, None)
        state.trusted_read_contexts[object_type] = context
        while len(state.trusted_read_contexts) > 4:
            state.trusted_read_contexts.pop(next(iter(state.trusted_read_contexts)))
        self._record(
            state, "TRUSTED_READ_CONTEXT_UPDATED", object_type=object_type,
            tool_name=tool_name, query=query,
        )

    @staticmethod
    def _read_context_summary(state: SessionState) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for object_type, context in state.trusted_read_contexts.items():
            data = context.get("data") or {}
            item: dict[str, Any] = {
                "object_type": object_type,
                "tool": context.get("tool"),
                "query": context.get("query"),
                "captured_at": context.get("captured_at"),
            }
            if object_type == "purchase_history":
                item["records"] = data.get("records", [])[:5]
            elif object_type == "product":
                item["products"] = data.get("products", [])[:5]
            elif object_type == "knowledge":
                item["hits"] = [
                    {
                        **{key: hit.get(key) for key in (
                            "chunk_id", "doc_id", "title", "heading", "class", "source", "score"
                        )},
                        "text": str(hit.get("text") or "")[:500],
                    }
                    for hit in data.get("hits", [])[:3]
                ]
            elif object_type == "request":
                if (context.get("query") or {}).get("query_scope") == "collection":
                    item["requests"] = [
                        {key: record.get(key) for key in ("request_id", "item", "state", "version")}
                        for record in data.get("requests", [])[:8]
                    ]
                else:
                    item["request_id"] = data.get("request_id")
            summaries.append(item)
        return summaries

    @staticmethod
    def _previous_read_query(state: SessionState, object_type: str, field: str) -> str:
        context = state.trusted_read_contexts.get(object_type) or {}
        return str((context.get("query") or {}).get(field) or "").strip()

    def _restore_consultation_context(
        self, state: SessionState, proposal: dict[str, Any]
    ) -> str | None:
        """连续追问仅可恢复同类型的可信工具结果；返回错误码时由调用方澄清。"""
        object_type = proposal.get("target_object")
        if object_type not in {"purchase_history", "product", "knowledge"}:
            return "TRUSTED_READ_CONTEXT_UNAVAILABLE"
        context = state.trusted_read_contexts.get(object_type)
        if not context:
            return "TRUSTED_READ_CONTEXT_UNAVAILABLE"
        data = json.loads(json.dumps(context.get("data") or {}, ensure_ascii=False))
        index = proposal.get("reference_index")
        collection_key = {
            "purchase_history": "records", "product": "products", "knowledge": "hits"
        }[object_type]
        if index is not None:
            values = data.get(collection_key, [])
            if type(index) is not int or not 1 <= index <= len(values):
                return "READ_REFERENCE_INDEX_INVALID"
            # 历史成交统一在回答投影时选择，确保本轮新查与跨轮复用只应用一次序号。
            if object_type != "purchase_history":
                data[collection_key] = [values[index - 1]]
        state.consultation_evidence.append({
            "tool": context.get("tool"), "data": data, "from_trusted_context": True,
        })
        self._record(
            state, "TRUSTED_READ_CONTEXT_REUSED", object_type=object_type,
            source_run_id=context.get("run_id"), reference_index=index,
        )
        return None

    @staticmethod
    def _capture_task(state: SessionState) -> TaskSnapshot | None:
        if state.active_task_type not in AgentRuntime.WRITE_TASKS:
            return None
        return TaskSnapshot(
            task_type=state.active_task_type,
            target_request_id=state.target_request_id,
            slots=dict(state.slots),
            missing_slots=list(state.missing_slots),
            last_read_version=state.last_read_version,
            task_messages=list(state.task_messages),
        )

    def _model_messages(self, state: SessionState, current_message: str) -> list[dict[str, Any]]:
        context = {
            "actor": state.actor_identity,
            "active_task": ({
                "task_type": state.active_task_type,
                "target_request_id": state.target_request_id,
                "object_persisted": state.target_request_id in state.known_requests,
                "collected_slots": state.slots,
                "missing_slots": state.missing_slots,
                "confirmation_state": state.confirmation_state,
            } if state.active_task_type else None),
            "suspended_task": ({
                "task_type": state.suspended_task.task_type,
                "target_request_id": state.suspended_task.target_request_id,
                "collected_slots": state.suspended_task.slots,
                "missing_slots": state.suspended_task.missing_slots,
            } if state.suspended_task else None),
            "pending_resume_changes": state.resume_proposal,
            "pending_clarification": state.clarification,
            "recent_request_ids_oldest_to_newest": list(state.recent_request_ids),
            "current_candidate_request_ids": list(state.candidate_request_ids),
            "trusted_read_contexts": self._read_context_summary(state),
        }
        relevant = state.task_messages[-self.model_context_messages:]
        return [
            {"role": "system", "content": SYSTEM_PROMPT + "\n当前结构化会话状态：" + json.dumps(context, ensure_ascii=False)},
            *relevant,
            {"role": "user", "content": current_message},
        ]

    def clear_session(self, state: SessionState) -> dict[str, Any]:
        """原地替换会话派生状态，保持 Cookie 会话 ID，不触碰业务数据库。"""
        session_id = state.session_id
        actor = state.actor_identity
        fresh = SessionState(session_id=session_id, actor_identity=actor)
        state.__dict__.clear()
        state.__dict__.update(fresh.__dict__)
        state.current_run_id = "run-" + uuid.uuid4().hex
        self._record(state, "SESSION_CLEARED")
        # 清空响应只用于当前 HTTP 回执，不重新写入刚清空的聊天历史。
        return {
            "message": "会话已清空。聊天记录、未完成任务、对象引用和待确认操作均已重置。",
            "route_action": "STOP", "route_reason_code": "SESSION_CLEARED",
            "trace_id": state.current_run_id, "confirmation": None,
            "result": None, "outcome_status": "SUCCESS",
        }

    def _invalidate_confirmation(self, state: SessionState, reason: str) -> None:
        if state.pending_action:
            self._record(
                state, "CONFIRMATION_INVALIDATED", action_id=state.pending_action.action_id,
                route_reason_code=reason,
            )
        state.pending_action = None
        state.confirmation_state = "INVALIDATED"

    def handle_message(self, state: SessionState, user_message: str) -> dict[str, Any]:
        message = str(user_message or "").strip()
        if not message:
            return self._response(state, "ASK", "请输入需要办理的采购事项。", "EMPTY_MESSAGE")
        normalized = message.replace("。", "").strip()
        if state.retry_submit_request_id and not state.resume_prompt_pending:
            request_id = state.retry_submit_request_id
            if normalized in self.RETRY_SUBMIT_WORDS | self.KEEP_DRAFT_WORDS:
                state.current_run_id = "run-" + uuid.uuid4().hex
                self._append_message(state, "user", message)
                self._record(state, "USER_MESSAGE", message=message)
                state.retry_submit_request_id = None
                if normalized in self.KEEP_DRAFT_WORDS:
                    self._finish_task(state)
                    return self._response(
                        state, "STOP", f"已保留申请 {request_id}，本次不再提交，采购任务已结束。",
                        "DRAFT_RETAINED", outcome_status="PARTIAL_SUCCESS",
                    )
                state.active_task_type = "submit"
                state.target_request_id = request_id
                return self._prepare_submit(state, {"request_id": request_id}, request_id)
        resume_ids = self._explicit_request_ids(state, {}, message) if state.suspended_task else set()
        resume_context = state.resume_prompt_pending or (
            state.suspended_task is not None and state.active_task_type not in self.WRITE_TASKS
        )
        # 否定恢复优先于“继续”字样；带其他明确对象的消息交回正常对象解析。
        decline_resume = bool(re.fullmatch(r"(?:我)?(?:不想|不|不要|不用|暂不|先不)(?:再)?(?:继续|恢复|修改).{0,20}(?:了|吧)?", normalized))
        if (resume_context and (normalized in self.ABANDON_WORDS or decline_resume)
                and (not resume_ids or resume_ids == {state.suspended_task.target_request_id})):
            state.current_run_id = "run-" + uuid.uuid4().hex
            self._append_message(state, "user", message)
            self._record(state, "USER_MESSAGE", message=message)
            return self._abandon_suspended_task(state)
        resume_ack = state.resume_proposal is not None and normalized in {"是", "好的", "对", "确认", "是的"}
        natural_resume = bool(re.fullmatch(r"(?:好的[，,]?|请)?(?:继续|恢复)(?:刚才|原来|之前|原)?(?:的)?(?:采购|修改|提交)?(?:任务|申请|办理)", normalized))
        if resume_context and (normalized in self.RESUME_WORDS or resume_ack or natural_resume):
            state.current_run_id = "run-" + uuid.uuid4().hex
            self._append_message(state, "user", message)
            self._record(state, "USER_MESSAGE", message=message)
            return self._resume_suspended_task(state, latest_user_message=message)
        if state.pending_action and normalized in self.CONFIRM_WORDS:
            return self.confirm(state, state.pending_action.action_id, "confirm")
        if state.pending_action and normalized in self.CANCEL_WORDS:
            return self.confirm(state, state.pending_action.action_id, "cancel")
        if normalized in self.ABANDON_WORDS and (
                state.active_task_type or state.pending_action or state.clarification):
            state.current_run_id = "run-" + uuid.uuid4().hex
            self._append_message(state, "user", message)
            self._record(state, "USER_MESSAGE", message=message)
            return self._abandon_current_task(state)
        if (state.clarification or {}).get("item_change"):
            response = self._resolve_item_change(state, message)
            if response is not None:
                return response
        if not state.pending_action and normalized in self.CONFIRM_WORDS:
            return self._response(state, "ASK", "当前没有待确认操作。", "NO_PENDING_CONFIRMATION")
        pre_turn_task = self._capture_task(state)
        if state.pending_action:
            self._invalidate_confirmation(state, "USER_CHANGED_CONTEXT")

        state.current_run_id = "run-" + uuid.uuid4().hex
        state.consultation_evidence = []
        state.turn_read_cache = {}
        self._append_message(state, "user", message)
        self._record(state, "USER_MESSAGE", message=message)
        messages = self._model_messages(state, message)
        self._record(
            state, "MODEL_CONTEXT_BUILT",
            model_message_count=len(messages),
            task_history_message_count=len(state.task_messages[-self.model_context_messages:]),
            full_history_message_count=len(state.conversation_tail),
            recent_request_ids=list(state.recent_request_ids),
        )
        latest_user_message = message
        for _ in range(self.max_model_steps):
            turn = self.model.complete(messages, MODEL_TOOL_SCHEMAS)
            self._record(
                state, "MODEL_TURN_COMPLETED", model_name=self.model_name,
                token_usage=turn.usage, tool_names=[call.name for call in turn.tool_calls],
            )
            if not turn.tool_calls:
                text = (turn.content or "请补充更明确的采购目标。").strip()
                if (turn.content and turn.content.strip() and state.consultation_evidence
                        and pre_turn_task is None and state.active_task_type not in self.WRITE_TASKS
                        and state.suspended_task is None and not state.resume_prompt_pending
                        and not state.pending_action and not state.candidate_request_ids
                         and not state.retry_submit_request_id and not state.uncertain_request_id
                         and not self._write_turn_without_consultation(state, message)):
                    return self._complete_consultation(state, direct_answer=text)
                return self._response(state, "ASK", text, "MODEL_REQUESTED_CLARIFICATION")
            messages.append({
                "role": "assistant",
                "content": turn.content,
                "tool_calls": [{
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                } for call in turn.tool_calls],
            })
            proposal: dict[str, Any] | None = None
            for call in turn.tool_calls:
                if call.name in READ_TOOL_NAMES:
                    try:
                        safe = self._execute_read_tool(
                            state, call.name, call.arguments, latest_user_message
                        )
                    except (ValueError, KnowledgeIndexError, ProcurementApiError, ProcurementTransportError) as exc:
                        safe = {"error": getattr(exc, "code", str(exc))}
                    messages.append({
                        "role": "tool", "tool_call_id": call.id,
                        "content": json.dumps(safe, ensure_ascii=False),
                    })
                elif call.name == "propose_task_state":
                    proposal = call.arguments
                    self._record(state, "MODEL_TASK_PROPOSAL_RECEIVED", proposal=dict(proposal))
                    messages.append({
                        "role": "tool", "tool_call_id": call.id,
                        "content": "Runtime will validate the proposal.",
                    })
                else:
                    self._route(state, "STOP", "FORBIDDEN_MODEL_TOOL", tool_name=call.name)
                    return self._response(
                        state, "STOP", "模型请求了未授权操作，已安全停止。", "FORBIDDEN_MODEL_TOOL"
                    )
            if proposal is not None:
                return self._process_proposal(state, proposal, latest_user_message, pre_turn_task)
        return self._response(state, "STOP", "本轮工具调用次数已达上限，请缩小任务范围后重试。", "MODEL_STEP_LIMIT")

    def _resolve_item_change(self, state: SessionState, message: str) -> dict[str, Any] | None:
        """消费现有澄清中的商品候选；选择商品不等于业务写入确认。"""
        change = state.clarification["item_change"]
        item = change["fields"]["item"]
        word = message.strip().rstrip("。.!！?？").strip()
        accept = word in {"更换", "换", "确认更换", "同意更换", "确定更换", "确认切换"} or bool(
            re.fullmatch(r"(?:是的?|好的?)?[，,\s]*(?:请)?(?:确认|同意|确定)?(?:更换|切换|换)(?:吧)?", word)
            or re.fullmatch(r"(?:请)?(?:更换为|更换成|换成|改成|改为)\s*" + re.escape(item), word))
        reject = word in {"不换", "不更换", "不要更换", "不用更换", "取消更换", "保留原物品", "保留原商品", "否", "不是", "拒绝"}
        unclear = word in self.CONFIRM_WORDS | {"是", "是的", "好的", "对", "可以", "随便", "都行", "继续"}
        if not (accept or reject or unclear):
            # 新话题交还原路由，过期候选不能在之后被一句“更换”误消费。
            state.clarification.pop("item_change", None)
            return None
        state.current_run_id = "run-" + uuid.uuid4().hex
        state.consultation_evidence, state.turn_read_cache = [], {}
        self._append_message(state, "user", message)
        self._record(state, "USER_MESSAGE", message=message)
        if (state.active_task_type != "create" or state.pending_action
                or state.slots != change["base_slots"] or state.target_request_id != change["request_id"]):
            state.clarification.pop("item_change", None)
            return self._response(state, "ASK", "原任务已变化，旧商品切换候选已失效，请重新说明要采购的商品。",
                                  "CREATE_ITEM_CHANGE_STALE")
        if unclear:
            return self._response(state, "ASK", f"请明确回复“更换”以采用{item}，或“保留原物品”。这不是采购写入确认。",
                                  "CREATE_ITEM_CHANGE_DECISION_REQUIRED")
        self._record(state, "CREATE_ITEM_CHANGE_ACCEPTED" if accept else "CREATE_ITEM_CHANGE_REJECTED",
                     old_item=state.slots["item"], proposed_fields=change["fields"])
        if reject:
            state.clarification = None
            return self._response(state, "ASK", f"已保留原采购物品{state.slots['item']}及原参数，未更换、未执行写入。请继续说明原申请需要补充的信息。",
                                  "CREATE_ITEM_CHANGE_REJECTED")
        # 新商品只采用本次候选的明确字段，绝不继承旧用途、数量或目录供货价。
        self._finish_task(state, clear_target=True)
        state.active_task_type = "create"
        state.slots = dict(change["fields"])
        state.last_read_version = None
        state.candidate_request_ids = []
        self._remember_task_message(state, "user", change["source_message"])
        # 已确认的是具体候选，直接复用原准备流程，不能再次用当前“更换”文本触发旧商品冲突门禁。
        return self._prepare_create(state, {})

    @staticmethod
    def _resume_changes(proposal: dict[str, Any], message: str) -> dict[str, Any]:
        """恢复提案只接收本轮明确的变更，查询对象被模型带回的其他字段不能污染原任务。"""
        markers = {
            "quantity": r"数量|(?:改|调|设)(?:成|为)\s*\d|\d+\s*(?:台|个|件)",
            "relative_quantity_delta": r"增加|减少|增添|减去",
            "unit_price": r"单价|每台|每个|价格",
            "supplier_name": r"供应商", "purpose": r"用途|用于|用作", "item": r"物品|品名",
        }
        return {key: value for key, value in proposal.items()
                if key in markers and re.search(markers[key], message)}

    def _resume_suspended_task(
        self, state: SessionState, proposal: dict[str, Any] | None = None, *, latest_user_message: str = ""
    ) -> dict[str, Any]:
        snapshot = state.suspended_task
        if snapshot is None:
            state.resume_prompt_pending = False
            return self._response(state, "ASK", "当前没有可继续的未完成任务。", "NO_SUSPENDED_TASK")
        state.active_task_type = snapshot.task_type
        state.target_request_id = snapshot.target_request_id
        state.slots = dict(snapshot.slots)
        state.missing_slots = list(snapshot.missing_slots)
        state.last_read_version = snapshot.last_read_version
        state.task_messages = list(snapshot.task_messages)
        changes = dict(state.resume_proposal or {})
        changes.update(proposal or {})
        changes = {key: value for key, value in changes.items()
                   if key in (*self.CHANGE_FIELDS, "relative_quantity_delta")}
        state.slots.update(changes)
        if "quantity" in changes and "relative_quantity_delta" not in changes:
            state.slots.pop("relative_quantity_delta", None)
        state.candidate_request_ids = []
        self._invalidate_confirmation(state, "TASK_RESUMED")
        state.suspended_task = None
        state.resume_prompt_pending = False
        state.resume_proposal = None
        state.confirmation_state = "NONE"
        self._record(state, "TASK_RESUMED", request_id=snapshot.target_request_id)
        if snapshot.task_type in {"update", "submit"} and snapshot.target_request_id:
            # 恢复仅选择任务；最新状态、版本及正式写前确认仍由原 prepare 链路核验。
            restored = {"intent": snapshot.task_type, **state.slots, "request_id": snapshot.target_request_id}
            if "supplier_name" in state.slots:
                try:
                    self._execute_read_tool(state, "match_supplier", {"name": state.slots["supplier_name"]}, "")
                except (ValueError, ProcurementApiError, ProcurementTransportError):
                    return self._response(state, "ASK", "恢复任务后无法核实供应商，请核实后再继续，未执行写入。",
                                          "SUPPLIER_NOT_UNIQUELY_VERIFIED")
            # 复用原字段/金额/对象校验，而非从恢复入口直接跳到确认卡。
            state.consultation_evidence = []
            return self._process_proposal(state, restored, latest_user_message, proposal_source="resume_snapshot")
        missing = "、".join(state.missing_slots)
        text = "已恢复刚才的采购任务。"
        if missing:
            text += f"还需要补充：{missing}。"
        else:
            text += "请继续说明要补充或调整的内容，我会重新核验后生成确认。"
        return self._response(state, "ASK", text, "INTERRUPTED_TASK_RESUMED")

    def _abandon_suspended_task(self, state: SessionState) -> dict[str, Any]:
        state.suspended_task = None
        state.resume_prompt_pending = False
        state.resume_proposal = None
        state.retry_submit_request_id = None
        self._finish_task(state, clear_target=True)
        self._invalidate_confirmation(state, "TASK_ABANDONED")
        self._record(state, "TASK_ABANDONED")
        return self._response(state, "STOP", "已放弃刚才未完成的采购任务，未执行任何写入。", "INTERRUPTED_TASK_ABANDONED")

    def _abandon_current_task(self, state: SessionState) -> dict[str, Any]:
        """仅放弃会话临时办理，保留其他只读对象、已落库事实及 UNKNOWN 防重保护。"""
        persisted = state.retry_submit_request_id
        unknown = state.uncertain_request_id
        self._invalidate_confirmation(state, "TASK_ABANDONED")
        self._finish_task(state, clear_target=True)
        state.candidate_request_ids = []
        state.retry_submit_request_id = None
        text = "已放弃当前未完成的临时任务，未调用任何业务取消接口。"
        if persisted:
            text += f"已创建的申请 {persisted} 仍保留，不代表撤销该申请。"
        if unknown:
            text += f"申请 {unknown} 的前次写入结果仍未知，必须先查询真实状态，不能直接重试。"
        self._record(state, "TASK_ABANDONED", retained_request_id=persisted, uncertain_request_id=unknown)
        return self._response(state, "STOP", text, "TEMPORARY_TASK_ABANDONED",
                              outcome_status="UNKNOWN" if unknown else None)

    def _execute_read_tool(
        self,
        state: SessionState,
        tool_name: str,
        arguments: dict[str, Any],
        latest_user_message: str,
    ) -> dict[str, Any]:
        allowed_arguments = {
            "match_supplier": {"name"},
            "list_requests": set(),
            "get_request": {"request_id"},
            "get_request_po": {"request_id"},
            "get_request_budget": {"request_id"},
            "search_purchase_history": {"item", "use_previous_query"},
            "get_procurement_rules": {"topic"},
            "search_products": {"query", "status", "use_previous_query"},
            "search_knowledge": {"query", "top_k", "use_previous_query"},
        }
        if tool_name not in allowed_arguments or set(arguments) - allowed_arguments[tool_name]:
            raise ValueError("INVALID_TOOL_ARGUMENTS")
        if tool_name in {"search_purchase_history", "search_knowledge", "get_procurement_rules"}:
            if self._write_turn_without_consultation(state, latest_user_message):
                self._record(state, "READ_TOOL_REJECTED", tool_name=tool_name,
                             reason="TASK_FIELDS_ARE_NOT_CONSULTATION")
                raise ValueError("当前是在补充或发起采购申请，请匹配供应商并提出 create；不要查询商品或价格来替代用户字段。")
        if tool_name == "match_supplier":
            name = str(arguments.get("name", "")).strip()
            known_name = str(state.slots.get("supplier_name", "")).strip()
            if not name or (name not in latest_user_message and name != known_name):
                raise ValueError("UNGROUNDED_SUPPLIER_NAME")
            result = self._call_api(state, tool_name, self.api.match_supplier, name=name)
            state.supplier_matches[name] = result.data.get("matches", [])
            state.supplier_match_runs[name] = state.current_run_id or ""
        elif tool_name == "list_requests":
            if arguments:
                raise ValueError("INVALID_TOOL_ARGUMENTS")
            result = self._call_api(state, tool_name, self.api.list_requests)
            listed_ids: list[str] = []
            for item in result.data.get("requests", []):
                if item.get("request_id"):
                    state.known_requests[item["request_id"]] = item
                    listed_ids.append(item["request_id"])
            state.candidate_request_ids = listed_ids[-8:]
        elif tool_name == "search_purchase_history":
            item = str(arguments.get("item", "")).strip()
            use_previous = arguments.get("use_previous_query") is True
            previous = self._previous_read_query(state, "purchase_history", "item")
            if use_previous:
                if not previous:
                    raise ValueError("TRUSTED_HISTORY_CONTEXT_UNAVAILABLE")
                item = previous
                self._record(state, "TRUSTED_QUERY_REUSED", object_type="purchase_history", query={"item": item})
            elif item and item not in latest_user_message:
                raise ValueError("UNGROUNDED_HISTORY_ITEM")
            result = self._call_api(
                state, tool_name, self.api.search_purchase_history, item=item
            )
        elif tool_name == "get_procurement_rules":
            topic = str(arguments.get("topic", "")).strip()
            result = self._call_api(
                state, tool_name, self.api.get_procurement_rules, topic=topic
            )
        elif tool_name == "search_knowledge":
            if self.knowledge_index is None:
                raise KnowledgeIndexError("KNOWLEDGE_INDEX_UNAVAILABLE")
            proposed_query = str(arguments.get("query", "")).strip()
            if not proposed_query:
                raise ValueError("KNOWLEDGE_QUERY_REQUIRED")
            top_k = arguments.get("top_k", 4)
            if type(top_k) is not int or not 1 <= top_k <= 8:
                raise ValueError("KNOWLEDGE_TOP_K_INVALID")
            use_previous = arguments.get("use_previous_query") is True
            previous = self._previous_read_query(state, "knowledge", "query")
            if use_previous:
                pending = state.clarification or {}
                pending_knowledge = (pending.get("intent") in {None, "consult", "ambiguous"}
                                     and pending.get("target_object", "unknown") in {"knowledge", "unknown"}
                                     and pending.get("question") and state.active_task_type not in self.WRITE_TASKS)
                if pending_knowledge:
                    previous = pending.get("topic") or previous
                if not previous:
                    raise ValueError("TRUSTED_KNOWLEDGE_CONTEXT_UNAVAILABLE")
                conditions = list(pending.get("conditions") or []) if pending_knowledge else []
                effective_query = previous + "\n连续追问：" + "\n".join(dict.fromkeys([*conditions, latest_user_message]))
                self._record(state, "TRUSTED_QUERY_REUSED", object_type="knowledge", query={"query": previous})
            else:
                # 新主题始终使用可信的当前用户原话，防止模型注入未出现的规则问题。
                effective_query = latest_user_message
            cache_key = json.dumps(
                {"tool": tool_name, "query": effective_query, "top_k": top_k},
                ensure_ascii=False, sort_keys=True,
            )
            cached = state.turn_read_cache.get(cache_key)
            if cached is not None:
                self._record(
                    state, "TOOL_CALL_CACHE_HIT", tool_name=tool_name,
                    cache_scope="current_turn", query=effective_query, top_k=top_k,
                )
                return cached
            tool_call_id = "rag-call-" + uuid.uuid4().hex
            started = time.monotonic()
            data = self.knowledge_index.search(effective_query, top_k=top_k)
            safe = model_safe_result(tool_name, data)
            self._record(
                state, "TOOL_CALL_COMPLETED", tool_name=tool_name,
                tool_call_id=tool_call_id, http_status=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                sanitized_result=safe, outcome_status="SUCCESS",
            )
            self._record(
                state, "KNOWLEDGE_RETRIEVED", tool_name=tool_name,
                tool_call_id=tool_call_id,
                index_fingerprint=safe.get("index_fingerprint"),
                evidence_sufficient=safe.get("evidence_sufficient"),
                chunk_ids=[item.get("chunk_id") for item in safe.get("hits", [])],
                core_rule_chunk_ids=data.get("core_rule_chunk_ids", []),
                timings_ms=safe.get("timings_ms"), cache_hit=False,
            )
            state.consultation_evidence.append({"tool": tool_name, "data": safe})
            state.turn_read_cache[cache_key] = safe
            self._remember_read_context(
                state, "knowledge", tool_name, {"query": effective_query, "top_k": top_k}, safe
            )
            return safe
        elif tool_name == "search_products":
            query = str(arguments.get("query", "")).strip()
            status = str(arguments.get("status", "active")).strip().lower()
            use_previous = arguments.get("use_previous_query") is True
            previous = self._previous_read_query(state, "product", "query")
            pending = state.clarification or {}
            current_item = state.slots.get("item") if state.active_task_type == "create" else None
            if not current_item and not state.active_task_type and pending.get("intent") in {"ambiguous", "consult"}:
                current_item = (pending.get("known_slots") or {}).get("item")
            unrestricted = bool(re.search(
                r"(?:全部|所有|整个|不限|不限定).*(?:商品|目录|范围)|(?:商品目录|目录商品).*(?:有哪些|列表)",
                latest_user_message))
            # 当前明示条件/当前待澄清商品优先，不能被较早的目录查询覆盖。
            if use_previous and not unrestricted and not (query and query in latest_user_message):
                query = str(current_item or previous or "").strip()
                if not query:
                    raise ValueError("TRUSTED_PRODUCT_CONTEXT_UNAVAILABLE")
                self._record(state, "TRUSTED_QUERY_REUSED", object_type="product", query={"query": query})
            else:
                # 已确认任务槽位可用于只读查询，不代表新写入授权；暂停任务不参与自动补参。
                if not query and not unrestricted:
                    query = str(current_item or "").strip()
                if query and query not in latest_user_message and query != current_item:
                    raise ValueError("UNGROUNDED_PRODUCT_QUERY")
                if not query and not unrestricted:
                    raise ValueError("PRODUCT_QUERY_REQUIRED")
            if status not in {"active","inactive","all"}:
                raise ValueError("INVALID_PRODUCT_STATUS")
            result = self._call_api(
                state, tool_name, self.api.search_products, query=query, status=status
            )
        else:
            request_id = str(arguments.get("request_id", "")).strip()
            if (state.active_task_type == "create" and request_id == state.target_request_id
                    and request_id not in state.known_requests):
                raise ValueError("当前对象只是尚未落库的会话草稿；调整字段请提出 create 并重新确认，不查询业务申请。")
            grounded = (
                request_id in latest_user_message
                or request_id == state.target_request_id
                or request_id in state.known_requests
            )
            if not request_id or not grounded:
                raise ValueError("UNGROUNDED_REQUEST_ID")
            method = getattr(self.api, tool_name)
            result = self._call_api(state, tool_name, method, request_id=request_id)
            if tool_name == "get_request":
                state.known_requests[request_id] = result.data
                state.last_read_version = result.data.get("version")
                self._remember_reference(state, request_id)
        safe = model_safe_result(tool_name, result.data)
        if tool_name == "search_products":
            candidates = self._product_candidates(safe.get("products", []), query)
            if candidates and len(candidates) != len(safe.get("products", [])):
                self._record(state, "PRODUCT_CANDIDATES_FILTERED", query=query,
                             recalled_codes=[p.get("code") for p in safe["products"]],
                             candidate_codes=[p.get("code") for p in candidates])
                safe = {**safe, "products": candidates, "total": len(candidates)}
        if tool_name in {"search_purchase_history", "get_procurement_rules", "search_products"}:
            state.consultation_evidence.append({"tool": tool_name, "data": safe})
        if tool_name == "search_purchase_history":
            self._remember_read_context(state, "purchase_history", tool_name, {"item": item}, safe)
        elif tool_name == "search_products":
            self._remember_read_context(
                state, "product", tool_name, {"query": query, "status": status}, safe
            )
        elif tool_name == "get_procurement_rules":
            self._remember_read_context(state, "knowledge", tool_name, {"query": topic}, safe)
        elif tool_name == "get_request":
            self._remember_read_context(state, "request", tool_name, {"request_id": request_id}, safe)
        if tool_name == "list_requests":
            state.turn_read_cache["list_requests"] = safe
        elif tool_name == "get_request_budget":
            # 仅复用本轮同一申请的真实读结果；下一轮handle_message会清空，不能复用旧余额。
            state.turn_read_cache["request_budget:" + request_id] = safe
        return safe

    @staticmethod
    def _explicit_consultation(message: str) -> bool:
        return bool(re.search(
            r"(?:^|[，。；]|先|顺便|再|同时|并|然后)(?:请|帮我|想)?(?:查询|查一下|查查|查|了解|咨询|看看|比较|推荐)"
            r"|(?:怎么|如何|什么时候|为什么|能否|是否|什么|哪些|吗|[？?])"
            r"|(?:告诉我|解释|说明).*(?:规则|条件|流程|原因)|预算.*(?:够不够|是否足够)", message
        ))

    @staticmethod
    def _write_turn_without_consultation(state: SessionState, message: str) -> bool:
        """只限制采购字段补充中的无关咨询读取，不负责授予写权限或替代模型意图判断。"""
        if AgentRuntime._explicit_consultation(message):
            return False
        # 活动任务本身不能证明本轮仍在补参；只保留明确数值字段补充的读门禁。
        if state.active_task_type == "create" and re.match(
            r"\s*(?:数量|单价|每台|每个)\s*(?:改成|改为|调整为|为|是|[:：])?\s*[¥￥]?-?\d", message
        ):
            return True
        # 只识别明确操作承诺，不因咨询中出现业务动作名就升级写任务。
        explicit_create = re.search(r"(?:帮我|请|直接|我要|决定|现在)(?:帮我)?\s*(?:新建|创建|发起|办理).*申请", message)
        complete_purchase = bool(re.search(r"^\s*(?:我(?:要|需要))?(?:采购|购买)\s*\d", message)
                                 and re.search(r"单价\s*\d", message) and "供应商" in message)
        return bool(explicit_create or complete_purchase)

    def _call_api(self, state: SessionState, tool_name: str, method: Any, **kwargs: Any):
        if tool_name in {"create_request", "submit_request", "update_request"}:
            state.write_started = True
        tool_call_id = self.api.new_tool_call_id()
        sanitized = {key: value for key, value in kwargs.items() if key not in {"token", "authorization"}}
        self._record(
            state, "TOOL_CALL_STARTED", tool_name=tool_name,
            tool_call_id=tool_call_id, sanitized_args=sanitized,
        )
        try:
            result = method(
                **kwargs, run_id=state.current_run_id or ("run-" + uuid.uuid4().hex),
                tool_call_id=tool_call_id,
            )
        except ProcurementApiError as exc:
            self._record(
                state, "TOOL_CALL_FAILED", tool_name=tool_name,
                tool_call_id=tool_call_id, http_status=exc.status,
                outcome_status=exc.code,
            )
            raise
        except ProcurementTransportError as exc:
            self._record(
                state, "TOOL_CALL_FAILED", tool_name=tool_name,
                tool_call_id=tool_call_id, outcome_status="TRANSPORT_ERROR",
                result_uncertain=exc.uncertain,
            )
            raise
        self._record(
            state, "TOOL_CALL_COMPLETED", tool_name=tool_name,
            tool_call_id=tool_call_id, http_status=result.status,
            latency_ms=result.latency_ms,
            sanitized_result=model_safe_result(tool_name, result.data),
            outcome_status="SUCCESS",
        )
        return result

    def _process_proposal(
        self,
        state: SessionState,
        proposal: dict[str, Any],
        latest_user_message: str,
        pre_turn_task: TaskSnapshot | None = None,
        *, proposal_source: str = "model",
    ) -> dict[str, Any]:
        proposal = dict(proposal)
        intent = proposal.get("intent")
        dialogue_relation = proposal.get("dialogue_relation", "unclear")
        target_object = proposal.get("target_object", "unknown")
        query_scope = proposal.get("query_scope", "single")
        previous_request = state.trusted_read_contexts.get("request") or {}
        if (intent == "query" and target_object == "request" and dialogue_relation == "correction"
                and "query_scope" not in proposal and not proposal.get("request_id")
                and proposal.get("reference_index") is None
                and (previous_request.get("query") or {}).get("query_scope") == "collection"
                and not self._explicit_request_ids(state, proposal, latest_user_message)
                and not self._candidate_from_message(state, state.candidate_request_ids, latest_user_message)):
            # 纠正集合答复时恢复已验证范围；明确编号或新范围始终优先，写操作不使用此推断。
            query_scope = proposal["query_scope"] = "collection"
        if query_scope not in {"single", "collection"} or (
            query_scope == "collection" and (intent != "query" or target_object != "request")
        ):
            return self._response(state, "ASK", "集合范围仅用于查询申请；修改或提交请指定唯一申请。",
                                  "INVALID_QUERY_SCOPE")
        allowed_relations = {
            "new_task", "follow_up", "supplement", "correction", "confirm",
            "cancel", "switch_task", "pause", "resume", "unclear",
        }
        allowed_objects = {
            "request", "purchase_history", "product", "knowledge", "current_task", "unknown",
        }
        if dialogue_relation not in allowed_relations or target_object not in allowed_objects:
            return self._response(
                state, "ASK", "当前话语与已有任务的关系或目标对象不明确，请补充说明。",
                "INVALID_DIALOGUE_CONTEXT",
            )
        if dialogue_relation == "cancel" and (state.active_task_type or state.clarification or state.suspended_task):
            if state.suspended_task and state.active_task_type not in self.WRITE_TASKS:
                return self._abandon_suspended_task(state)
            return self._abandon_current_task(state)
        model_intent = intent if proposal_source == "model" else None
        if (intent == "create" and target_object == "product" and not state.active_task_type
                and not state.suspended_task and not self._explicit_consultation(latest_user_message)
                and not re.search(r"申请|创建|新建|发起|办理", latest_user_message)):
            # 商品对象与创建意图矛盾时，候选字段不是已接受任务；明确创建/补参不额外确认意图。
            intent = proposal["intent"] = "ambiguous"
            proposal["question"] = "您要发起采购申请，还是仅了解商品或价格？已保留物品、数量及用途，尚未办理申请。"
            self._record(state, "TASK_INTENT_CONFLICT", model_intent=model_intent, target_object=target_object)
        if (intent == "consult" and target_object == "unknown"
                and self._write_turn_without_consultation(state, latest_user_message)):
            # 兼容无对象类型的旧提案；有类型的咨询不因当前存在采购任务而重绑。
            intent = proposal["intent"] = "create"
            self._record(state, "TASK_INTENT_RETAINED", source_intent="consult", target_intent="create")
        if (intent == "update" and pre_turn_task and pre_turn_task.task_type == "create"
                and pre_turn_task.target_request_id not in state.known_requests):
            resolved = self._resolve_request_id(state, proposal, latest_user_message)
            if not resolved or resolved == pre_turn_task.target_request_id:
                # 未落库的新建草稿改字段仍属同一 create，不走 Backend 的已有申请修改。
                intent = proposal["intent"] = "create"
                if "relative_quantity_delta" in proposal and "quantity" not in proposal:
                    base = pre_turn_task.slots.get("quantity")
                    delta = proposal["relative_quantity_delta"]
                    if type(base) is int and type(delta) is int:
                        proposal["quantity"] = base + delta
                self._record(state, "DRAFT_CHANGE_REBOUND", source_intent="update", target_intent="create")
        if intent not in {"create", "query", "update", "submit", "consult", "ambiguous", "unsupported"}:
            return self._response(state, "ASK", "请说明要咨询、新建、查询、修改还是提交草稿。", "INVALID_INTENT")
        self._record(state, "TASK_INTENT_ACCEPTED", model_intent=model_intent, accepted_intent=intent,
                     proposal_source=proposal_source, dialogue_relation=dialogue_relation,
                     target_object=target_object)
        # 问题和已知信息由 Session 显式持有；模型只解释本轮与它的关系，不自行充当存储。
        prior = state.clarification or {}
        pending_goal = dict(prior) if not state.active_task_type and prior.get("intent") == "ambiguous" else None
        if (pending_goal and intent == "update" and not proposal.get("request_id")
                and not self._explicit_request_ids(state, {}, latest_user_message)
                and (pending_goal.get("known_slots") or {}).get("item")):
            # 未确认的创建目标不是已有申请；补数量不能据此授权修改历史对象。
            pending_goal["known_slots"].update({k: proposal[k] for k in ("quantity", "purpose") if k in proposal})
            state.clarification = pending_goal
            return self._response(state, "ASK", "已保留当前物品及补充信息。请明确是发起这笔采购申请，还是仅咨询；未修改已有申请。",
                                  "PENDING_GOAL_NOT_PERSISTED_REQUEST")
        if (pending_goal and intent == "create" and dialogue_relation != "switch_task"
                and (not proposal.get("item") or proposal["item"] == (pending_goal.get("known_slots") or {}).get("item"))):
            for key,value in (pending_goal.get("known_slots") or {}).items():
                if key in {"item", "quantity", "purpose"}:
                    proposal.setdefault(key, value)
        if intent != "ambiguous" or not state.active_task_type:
            continuing = dialogue_relation not in {"new_task", "switch_task"}
            same_topic = continuing and prior.get("target_object", target_object) in {target_object, "unknown"}
            conditions = list(prior.get("conditions") or []) if same_topic else []
            if same_topic and prior.get("question") and target_object == "knowledge":
                conditions = list(dict.fromkeys([*conditions, latest_user_message[:500]]))[-4:]
            state.clarification = {
                "intent": intent, "target_object": target_object,
                "topic": prior.get("topic", latest_user_message[:1000]) if same_topic else latest_user_message[:1000],
                "conditions": conditions,
            }
        if dialogue_relation in {"new_task", "switch_task"} and target_object != "request":
            # 新的非申请话题不能继续携带旧申请候选；暂停任务本身仍由独立快照保存。
            state.candidate_request_ids = []
        if intent in {"ambiguous", "unsupported"}:
            if intent == "ambiguous" and state.active_task_type is None:
                known = dict((pending_goal or {}).get("known_slots") or {})
                known.update({k: proposal[k] for k in ("item", "quantity", "purpose") if k in proposal})
                state.clarification["known_slots"] = known
            if intent == "ambiguous" and state.active_task_type is None:
                # 意图待澄清时只保留话题消息，不把它们当作可写槽位或确认授权。
                self._remember_task_message(state, "user", latest_user_message)
            text = str(proposal.get("question") or (
                "请说明要咨询、新建、查询、修改还是提交已有草稿。" if intent == "ambiguous"
                else "当前仅支持采购咨询、新建、查询、修改和提交已有草稿。"
            ))
            if intent == "unsupported" and re.search(r"切换.*(?:用户|身份)|(?:用户|身份).*切换|冒充|token|令牌", latest_user_message, re.I):
                # 拒绝响应说明真实固定身份；不读取聊天 Token，更不改变现有权限机制。
                text = f"当前业务身份固定为 {state.actor_identity}，无法切换身份或使用聊天提供的 Token 冒充管理员。当前仅支持采购咨询、新建、查询、修改和提交已有草稿。"
            response = self._response(state, "ASK" if intent == "ambiguous" else "STOP", text, intent.upper())
            if intent == "ambiguous" and state.active_task_type is None:
                self._remember_task_message(state, "assistant", text)
            elif intent == "unsupported" and state.active_task_type is None:
                # 不支持的临时插曲不能擦掉尚待澄清的采购目标，也不是放弃授权。
                if pending_goal:
                    state.clarification = pending_goal
                    self._record(state, "CLARIFICATION_RETAINED", clarification=pending_goal)
                else:
                    state.task_messages = []
            return response
        if (intent == "consult" and not state.consultation_evidence
                and dialogue_relation in {"follow_up", "supplement", "correction"}):
            context_error = self._restore_consultation_context(state, proposal)
            if context_error:
                return self._response(
                    state, "ASK", str(proposal.get("question") or
                    "当前可信上下文不足以唯一定位要追问的只读结果，请说明是历史成交、商品目录还是知识制度，并指出具体对象。"),
                    context_error,
                )
        catalog_reads = []
        catalog_canonical = False
        for entry in state.consultation_evidence:
            if entry["tool"] != "search_products":
                continue
            data = entry["data"]
            query = data.get("query")
            candidates = self._product_candidates(data.get("products", []), query or "")
            binds_item = any(proposal.get("item") in {p.get("name"), p.get("code")} for p in candidates)
            if query and (query in {proposal.get("item"), state.slots.get("item")}
                          or binds_item):
                catalog_reads.append(entry)
                if binds_item and len(candidates) == 1:
                    catalog_canonical = query == state.slots.get("item")
                elif binds_item and proposal.get("item") not in latest_user_message and query in latest_user_message:
                    # 模型不能从多个目录候选自行选一个；恢复用户原条件交还原目录消歧。
                    proposal["item"] = query
        if (intent == "create" and pre_turn_task and pre_turn_task.task_type == "create"
                and target_object in {"current_task", "product"}
                and not self._explicit_consultation(latest_user_message)):
            excluded = len(state.consultation_evidence) - len(catalog_reads)
            if excluded:
                self._record(state, "READ_EVIDENCE_EXCLUDED", target_object="current_task", count=excluded)
                state.consultation_evidence = catalog_reads
        catalog_auxiliary = bool(
            intent == "create" and state.consultation_evidence
            and all(e["tool"] == "search_products" for e in state.consultation_evidence)
            and not self._explicit_consultation(latest_user_message)
            and ((pre_turn_task and pre_turn_task.task_type == "create")
                 or re.search(r"(?:新建|创建|发起|申请|办理).*?(?:采购|申请|采购单)", latest_user_message))
            and len(catalog_reads) == len(state.consultation_evidence)
        )
        if catalog_auxiliary:
            self._record(state, "TASK_AUXILIARY_READ_ACCEPTED", task_type="create", tool_name="search_products")
        mixed_write = bool(state.consultation_evidence and not catalog_auxiliary and intent in self.WRITE_TASKS and (
            pre_turn_task or re.search(
                r"(?:^|[，,。；;]|帮我|请|直接|我要|决定|然后|再|并)\s*(?:帮我|继续)?\s*"
                r"(?:新建|创建|发起|修改|提交|把|将|采购|购买)",
                latest_user_message,
            )
        ))
        if state.consultation_evidence and intent in self.WRITE_TASKS and not mixed_write and not catalog_auxiliary:
            # 同一轮只要执行了咨询工具，就只返回只读证据；写入必须由用户下一条明确消息重新发起。
            if pre_turn_task:
                state.suspended_task = pre_turn_task
                state.resume_prompt_pending = True
                self._finish_task(state, clear_target=True)
                self._record(state, "TASK_SUSPENDED", suspended_task_type=pre_turn_task.task_type)
            response = self._complete_consultation(
                state, finalize=state.suspended_task is None
            )
            return self._append_resume_question(state, response) if state.suspended_task else response

        if state.suspended_task and intent in self.WRITE_TASKS:
            explicit_ids = self._explicit_request_ids(state, proposal, latest_user_message)
            snapshot = state.suspended_task
            original_reference = not explicit_ids and bool(re.search(
                r"(?:继续|恢复).{0,12}(?:原来|刚才|之前|原任务)|(?:原来|刚才|之前).{0,12}(?:继续|恢复)",
                latest_user_message,
            )) and not re.search(r"(?:不|别|取消|放弃).{0,8}(?:继续|恢复)", latest_user_message)
            if (explicit_ids == {snapshot.target_request_id} or original_reference) and intent == snapshot.task_type:
                changes = self._resume_changes(proposal, latest_user_message)
                if not state.consultation_evidence:
                    return self._resume_suspended_task(state, changes, latest_user_message=latest_user_message)
                state.resume_proposal = {**(state.resume_proposal or {}), **changes}
                return self._complete_consultation(state, deferred_write=True)
            elif len(explicit_ids) == 1 and snapshot.target_request_id not in explicit_ids:
                # 用户明确选择另一对象时保留原暂停任务，仍沿原链路核验新对象并生成写前确认。
                state.resume_prompt_pending = False
                state.resume_proposal = None
            else:
                state.resume_prompt_pending = True
                # 连续归属澄清只增补当前明确变更；空提案不能擦掉已暂存的数量等参数。
                state.resume_proposal = {**(state.resume_proposal or {}), **self._resume_changes(proposal, latest_user_message)}
                self._record(state, "REQUEST_OBJECT_CHECKED", object_status="AMBIGUOUS_TASK",
                             request_id=None)
                labels = {"quantity": "数量", "unit_price": "单价", "item": "物品",
                          "supplier_name": "供应商", "purpose": "用途", "relative_quantity_delta": "数量变化"}
                changes = "，".join(f"{labels[key]}为{value}" for key, value in state.resume_proposal.items())
                task_label = {"create": "新建", "update": "修改", "submit": "提交"}[snapshot.task_type]
                return self._response(
                    state, "ASK", f"是否继续原来的{task_label}任务，目标申请 {snapshot.target_request_id or '未确定'}"
                    + (f"，{changes}" if changes else "")
                    + "？请明确继续或不继续，也可提供唯一申请编号。恢复任务不代表授权写入。",
                    "SUSPENDED_TASK_DECISION_REQUIRED",
                )

        if pre_turn_task and state.suspended_task and intent in {"query", "consult"}:
            # 仍只保留一个暂停快照；不能用第二次中断静默覆盖尚未处理的原任务。
            return self._response(state, "ASK", "已有一个暂停任务，不能再暂停当前写任务。请先完成或明确放弃其中的任务。",
                                  "SUSPENDED_TASK_DECISION_REQUIRED")
        interrupted = pre_turn_task is not None and intent in {"query", "consult"}
        if interrupted:
            current_read_target = state.target_request_id
            state.suspended_task = pre_turn_task
            state.resume_prompt_pending = True
            self._finish_task(state, clear_target=True)
            if intent == "query":
                state.target_request_id = str(proposal.get("request_id") or current_read_target or "") or None
            self._record(state, "TASK_SUSPENDED", suspended_task_type=pre_turn_task.task_type)

        explicit_targets = self._explicit_request_ids(state, proposal, latest_user_message)
        switched_object = (intent in {"update", "submit"} and len(explicit_targets) == 1
                           and state.target_request_id is not None
                           and state.target_request_id not in explicit_targets)
        explicit_create = bool(re.search(r"(?:新建|创建|发起|采购|购买|换成|改成|改为|换为)", latest_user_message))
        product_context = state.trusted_read_contexts.get("product") or {}
        selected_products = [p for p in (product_context.get("data") or {}).get("products", [])
                             if any(str(p.get(k) or "") and str(p[k]) in latest_user_message for k in ("code", "name"))]
        catalog_selection = (prior.get("reason") == "CATALOG_PRODUCT_AMBIGUOUS"
                             and (product_context.get("query") or {}).get("query") == state.slots.get("item")
                             and len(selected_products) == 1
                             and proposal.get("item") in {selected_products[0].get("code"), selected_products[0].get("name")})
        new_create = (state.active_task_type == intent == "create"
                      and dialogue_relation in {"new_task", "switch_task"}
                      and isinstance(proposal.get("item"), str) and bool(proposal["item"].strip())
                      and proposal["item"] in latest_user_message and explicit_create
                      and proposal["item"] != state.slots.get("item"))
        if (intent == state.active_task_type == "create" and state.slots.get("item")
                and proposal.get("item") and proposal["item"] != state.slots["item"]
                and not new_create and not catalog_selection and not catalog_canonical
                and not (proposal["item"] in latest_user_message and re.search(r"(?:物品|商品|品名).{0,6}(?:改|换)|(?:改成|改为|换成|换为)", latest_user_message))):
            self._record(state, "PROPOSAL_FIELD_REJECTED", field="item", reason="CREATE_ITEM_CHANGE_AMBIGUOUS",
                         old_item=state.slots["item"], proposed_item=proposal["item"])
            if isinstance(proposal["item"], str) and proposal["item"] in latest_user_message:
                fields = {"item": proposal["item"]}
                if "quantity" in proposal:
                    fields["quantity"] = proposal["quantity"]
                purpose = proposal.get("purpose")
                if isinstance(purpose, str) and purpose.strip() and purpose in latest_user_message:
                    fields["purpose"] = purpose
                state.clarification["item_change"] = {
                    "fields": fields, "base_slots": dict(state.slots),
                    "request_id": state.target_request_id, "source_message": latest_user_message,
                }
            return self._response(state, "ASK", f"当前采购物品是{state.slots['item']}，您是要更换物品为{proposal['item']}，还是补充原申请信息？未更换物品、未写入。",
                                  "CREATE_ITEM_CHANGE_AMBIGUOUS")
        # 单独的新任务标签不清槽位；关系与新商品一致时才隔离上一笔参数。
        if state.active_task_type and (state.active_task_type != intent or switched_object or new_create):
            old_slots = dict(state.slots)
            state.slots = {}
            state.missing_slots = []
            state.task_messages = []
            # 本轮已经合法匹配的新供应商应保留，旧任务缓存不能用于新任务。
            state.supplier_matches = {q: matches for q, matches in state.supplier_matches.items()
                                      if state.supplier_match_runs.get(q) == state.current_run_id}
            state.supplier_match_runs = {q: state.current_run_id for q in state.supplier_matches}
            state.target_request_id = None
            state.last_read_version = None
            if new_create:
                state.candidate_request_ids = []
                supplied = self._resume_changes(proposal, latest_user_message)
                for key in self.CHANGE_FIELDS:
                    if key in proposal and proposal[key] == old_slots.get(key) and key not in supplied:
                        # 数量与旧单价恰好相同，也不能据字符串相等继承另一字段。
                        proposal.pop(key)
                self._record(state, "TASK_FIELDS_ISOLATED", reason="EXPLICIT_NEW_CREATE")
        state.active_task_type = intent
        if intent in self.WRITE_TASKS:
            self._remember_task_message(state, "user", latest_user_message)
        slots_before = dict(state.slots)
        proposed_supplier = proposal.get("supplier_name")
        if (
            intent == "create"
            and isinstance(proposed_supplier, str)
            and proposed_supplier.strip()
            and not self._supplier_candidate(state, proposed_supplier.strip())
        ):
            # 未经本任务可信匹配的供应商既不能写入，也不能继续占用旧槽位。
            proposal.pop("supplier_name")
            state.slots.pop("supplier_name", None)
            self._record(
                state,
                "PROPOSAL_FIELD_REJECTED",
                field="supplier_name",
                reason="SUPPLIER_NOT_GROUNDED_IN_TASK",
            )
        # “预算”是约束，不得被模型在新任务中直接转换成确定单价。
        price_words = ("单价", "每台", "每个", "价格")
        if (
            intent == "create"
            and "unit_price" in proposal
            and "预算" in latest_user_message
            and not any(word in latest_user_message for word in price_words)
            and "unit_price" not in state.slots
        ):
            proposal.pop("unit_price")
            self._record(state, "PROPOSAL_FIELD_REJECTED", field="unit_price", reason="BUDGET_IS_NOT_UNIT_PRICE")
        if intent in {"create", "update"}:
            explicit_price = re.search(
                r"单价\s*(?:却|竟然)?\s*(?:改成|改为|调整为|为|是|[:：])?\s*[¥￥]?\s*(-?\d+(?:\.\d+)?)(?![\d.])",
                latest_user_message,
            )
            if explicit_price:
                # 用户给出的非法金额也必须原样校验，不能因模型省略或修正而丢失失败原因。
                proposal["unit_price"] = explicit_price.group(1)
            # 模型偶尔保留“元/¥”展示单位，只去掉币种标记；金额精度与非负校验仍沿用原规则。
            price = proposal.get("unit_price")
            if isinstance(price, str):
                amount_text = re.fullmatch(r"\s*[¥￥]?\s*(-?\d+(?:\.\d+)?)\s*(?:元)?\s*", price)
                if amount_text:
                    proposal["unit_price"] = amount_text.group(1)
            # 用途是用户文本，保留原文而不接受模型无依据的扩写。
            purpose = re.search(r"(?:用于|用作|用途\s*[:：]?\s*(?:改为|为|是)?)\s*([^，,。；;！？!?]+)", latest_user_message)
            if purpose is None:
                purpose = re.search(r"(?:^|[，,；;])\s*给([^，,。；;！？!?]+)", latest_user_message)
            if purpose:
                proposal["purpose"] = purpose.group(1).strip()
            elif isinstance(proposal.get("purpose"), str) and proposal["purpose"] != state.slots.get("purpose"):
                supplied = proposal["purpose"]
                answered = "purpose" in state.missing_slots and supplied in latest_user_message
                # 三字段简写同样是明确输入；只接受模型字段与本轮逐项文本完全一致的用途。
                parts = [part.strip() for part in re.split(r"[，,]", latest_user_message)]
                triple_item = (len(parts) == 3 and (parts[0] == proposal.get("item") or any(
                    e["data"].get("query") == parts[0]
                    and len(self._product_candidates(e["data"].get("products", []), parts[0])) == 1
                    and any(proposal.get("item") in {p.get("name"), p.get("code")} for p in e["data"].get("products", []))
                    for e in catalog_reads)))
                triple = (intent == "create" and len(parts) == 3
                          and triple_item and parts[2] == supplied
                          and re.fullmatch(re.escape(str(proposal.get("quantity"))) + r"\s*(?:[\u4e00-\u9fff]{1,2})?", parts[1]))
                prior_explicit = any(
                    entry["role"] == "user" and re.search(r"用于|用作|用途", entry["content"])
                    and supplied in entry["content"] for entry in state.task_messages
                )
                if not answered and not prior_explicit and not triple:
                    # 背景描述不自动变成正式用途；允许回答用途补问及沿用同一任务明确提供的原文。
                    proposal.pop("purpose")
                    self._record(state, "PROPOSAL_FIELD_REJECTED", field="purpose", reason="PURPOSE_NOT_EXPLICITLY_PROVIDED")
            for name in self.CHANGE_FIELDS:
                if intent == "create" and name in {"supplier_name", "unit_price"}:
                    continue  # DEC-09：这些字段仅由可信目录回填，模型或员工均不能覆盖。
                if name in proposal:
                    state.slots[name] = proposal[name]
            if intent == "update" and "relative_quantity_delta" in proposal:
                state.slots["relative_quantity_delta"] = proposal["relative_quantity_delta"]
            elif "quantity" in proposal:
                state.slots.pop("relative_quantity_delta", None)
            stated_total = re.search(
                r"(?:总金额|总价|总额|合计|总计)\s*(?:为|是|[:：])?\s*(-?\d+(?:\.\d+)?)", latest_user_message
            )
            if stated_total:
                # 总额只用于校验用户输入一致性，不作为可写字段或价格推断来源。
                state.slots["stated_total_amount"] = stated_total.group(1)
            elif re.search(r"单价|数量", latest_user_message):
                state.slots.pop("stated_total_amount", None)
        if intent == "query" and query_scope == "collection":
            # 集合读取不能选出可写对象，也不能沿用上一笔申请的版本。
            state.target_request_id = None
            state.last_read_version = None
        elif intent in {"query", "update", "submit"}:
            request_id = self._resolve_request_id(state, proposal, latest_user_message)
            self._record(state, "REQUEST_OBJECT_CHECKED", request_id=request_id,
                         object_status="RESOLVED" if request_id else "AMBIGUOUS")
            if not request_id:
                state.target_request_id = None
                state.last_read_version = None
                return self._response(
                    state, "ASK", "请明确指定要操作的申请编号，当前对象无法唯一确定。",
                    "REQUEST_OBJECT_NOT_UNIQUE",
                )
            # 对象由当前输入和可信上下文决定，模型提案不能覆盖用户选择。
            if proposal.get("request_id") and proposal["request_id"] != request_id:
                self._record(state, "PROPOSAL_FIELD_REJECTED", field="request_id",
                             reason="CURRENT_USER_OBJECT_OVERRIDES_PROPOSAL")
            proposal["request_id"] = request_id
            state.target_request_id = request_id
        elif intent == "create":
            proposal.pop("request_id", None)
        self._record(
            state,
            "TASK_PROPOSAL_VALIDATED",
            proposal=proposal,
            slots_before=slots_before,
            slots_after=dict(state.slots),
        )
        if mixed_write:
            return self._complete_consultation(state, deferred_write=True)
        if intent == "create":
            return self._prepare_create(state, proposal)
        if intent == "query":
            needs_resume = interrupted or state.suspended_task is not None
            response = self._complete_query(
                state, proposal, latest_user_message, finalize=not needs_resume
            )
            return self._append_resume_question(state, response) if needs_resume else response
        if intent == "update":
            return self._prepare_update(state, proposal, latest_user_message)
        if intent == "submit":
            return self._prepare_submit(state, proposal, latest_user_message)
        needs_resume = interrupted or state.suspended_task is not None
        response = self._complete_consultation(
            state, finalize=not needs_resume, history_fields=proposal.get("history_fields"),
            reference_index=proposal.get("reference_index") if target_object == "purchase_history" else None,
            clarification_question=proposal.get("question"),
            target_object=target_object,
        )
        return self._append_resume_question(state, response) if needs_resume else response

    def _append_resume_question(
        self, state: SessionState, response: dict[str, Any]
    ) -> dict[str, Any]:
        if not state.suspended_task:
            return response
        suffix = "\n\n是否继续刚才的采购任务？请回复“继续”或“不继续”。"
        response["message"] = str(response.get("message") or "") + suffix
        response["route_action"] = "ASK"
        response["route_reason_code"] = "INTERRUPTED_TASK_WAITING_RESUME"
        state.resume_prompt_pending = True
        # _complete_query/_complete_consultation 已写入原答复，替换最后一条以保证刷新后显示一致。
        if state.conversation_tail and state.conversation_tail[-1].get("role") == "assistant":
            state.conversation_tail[-1]["content"] = response["message"]
        self._save_clarification(state, suffix.strip(), response["route_reason_code"])
        self._route(state, "ASK", "INTERRUPTED_TASK_WAITING_RESUME", outcome_status=response.get("outcome_status"))
        self._record(
            state, "FINAL_RESPONSE", final_response=response["message"],
            outcome_status=response.get("outcome_status"), request_id=state.target_request_id,
            version=(response.get("result") or {}).get("version"),
        )
        self._record(state, "TASK_RESUME_PROMPTED")
        return response

    def _validate_create_slots(self, slots: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        clean: dict[str, Any] = {}
        missing: list[str] = []
        for name in ("item", "supplier_name", "purpose"):
            value = slots.get(name)
            if not isinstance(value, str) or not value.strip():
                missing.append(name)
            else:
                clean[name] = value.strip()
        quantity = slots.get("quantity")
        if type(quantity) is not int or quantity <= 0:
            missing.append("quantity")
        else:
            clean["quantity"] = quantity
        try:
            value = Decimal(str(slots.get("unit_price")))
            if not value.is_finite() or value < 0 or value.as_tuple().exponent < -2:
                raise InvalidOperation
            clean["unit_price"] = f"{value:.2f}"
        except (InvalidOperation, ValueError, TypeError):
            missing.append("unit_price")
        return clean, missing

    def _supplier_candidate(self, state: SessionState, supplied_name: str) -> dict[str, Any] | None:
        current_queries = {
            query for query, run_id in state.supplier_match_runs.items()
            if run_id == state.current_run_id
        }
        for query, matches in state.supplier_matches.items():
            # 本轮若重新查过供应商，只接受本轮查询结果，防止旧任务缓存覆盖用户明确输入。
            if current_queries and query not in current_queries:
                continue
            exact = [item for item in matches if item.get("name") == supplied_name]
            if len(exact) == 1:
                return exact[0]
            if query == supplied_name and len(matches) == 1:
                return matches[0]
        return None

    def _prepare_create(self, state: SessionState, proposal: dict[str, Any]) -> dict[str, Any]:
        if state.uncertain_request_id:
            return self._response(
                state, "ASK",
                f"申请 {state.uncertain_request_id} 的前次写入结果未知，请先查询该申请真实状态，避免重复创建。",
                "UNKNOWN_REQUIRES_QUERY", outcome_status="UNKNOWN",
            )
        # 先收齐员工三字段；目录事实每次准备确认都重新读取，不沿用旧任务缓存。
        missing = [key for key in ("item", "purpose") if not isinstance(state.slots.get(key),str) or not state.slots[key].strip()]
        if type(state.slots.get("quantity")) is not int or state.slots["quantity"] <= 0:
            missing.append("quantity")
        state.missing_slots = missing
        if missing:
            labels={"item":"物品", "quantity":"数量", "purpose":"用途"}
            return self._response(state,"ASK","请补充："+"、".join(labels[k] for k in missing)+"。","MISSING_REQUIRED_SLOTS")
        try:
            product = self._catalog_product(state, state.slots["item"])
        except (ProcurementApiError, ProcurementTransportError, ValueError) as exc:
            reason=getattr(exc,'code',str(exc))
            if str(reason).startswith("CATALOG_PRODUCT_AMBIGUOUS:"):
                candidates=str(reason).split(':',1)[1]
                return self._response(
                    state,"ASK",f"“{state.slots['item']}”匹配到多个商品：{candidates}。请指定商品名称或编码；未创建草稿。",
                    "CATALOG_PRODUCT_AMBIGUOUS"
                )
            return self._response(state, "ASK", f"商品目录尚无唯一有效供应商和采购单价（{getattr(exc,'code',str(exc))}）。请联系采购负责人维护后再申请；未创建草稿。",
                                  "CATALOG_DATA_REQUIRED", outcome_status="BLOCKED")
        state.slots.update(item=product["name"], unit_price=product["purchase_price"], supplier_name=product["supplier"]["name"])
        clean, missing = self._validate_create_slots(state.slots)
        state.missing_slots = missing
        if missing:
            return self._response(state,"ASK","目录数据格式无效，请联系采购负责人维护；未创建草稿。",
                                  "CATALOG_DATA_REQUIRED",outcome_status="BLOCKED")
        supplier = product["supplier"]
        request_id = "AGT-" + uuid.uuid4().hex[:16].upper()
        clean["supplier_name"] = supplier["name"]
        clean.update(product_code=product["code"], catalog_version=product["version"])
        amount = Decimal(clean["unit_price"]) * clean["quantity"]
        conflict = self._amount_conflict(state, amount)
        if conflict:
            return conflict
        summary = (
            f"新建并提交采购申请：{clean['item']} × {clean['quantity']}，"
            f"单价 ¥{clean['unit_price']}，金额 ¥{amount:.2f}，"
            f"供应商 {clean['supplier_name']}，用途：{clean['purpose']}。申请人 U01。"
        )
        return self._set_pending(state, "create", request_id, clean, supplier, None, None, summary)

    def _catalog_product(self, state: SessionState, item: str) -> dict[str, Any]:
        data = self._call_api(state, "search_products", self.api.search_products, query=item, status="all").data
        rows = data.get("products", [])
        candidates = self._product_candidates(rows, item)
        if len(candidates) != 1:
            # 复用现有可信只读上下文保存待选目录，不把“选候选”误当另一笔商品切换。
            public = model_safe_result("search_products", {**data, "products": candidates or rows})
            self._remember_read_context(state, "product", "search_products", {"query": item}, public)
        # 子串召回不等于对象相同；仅弱匹配时仍保留召回供用户明确选择，绝不自动写入。
        if not candidates and rows:
            labels='、'.join(f"{p.get('name')}（{p.get('code')}）" for p in rows[:10])
            raise ValueError("CATALOG_PRODUCT_AMBIGUOUS:" + labels)
        if len(candidates) != 1:
            if not candidates:raise ValueError("CATALOG_PRODUCT_REQUIRED")
            labels='、'.join(f"{p.get('name')}（{p.get('code')}）" for p in candidates[:10])
            raise ValueError("CATALOG_PRODUCT_AMBIGUOUS:"+labels)
        product = candidates[0]
        supplier = product.get("supplier") or {}
        price = Decimal(str(product.get("purchase_price") or 0))
        if product.get("status") != "active" or supplier.get("status") not in {"active", "new"} or not supplier.get("id") or not price.is_finite() or price <= 0:
            raise ValueError("CATALOG_DATA_REQUIRED")
        return product

    @staticmethod
    def _product_candidates(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        """名称中心词/完整目录字段可作候选，前缀子串只保留搜索召回，不自动认作商品。"""
        needle = query.strip().casefold()
        if not needle:
            return rows
        exact = [p for p in rows if needle in {str(p.get(k, "")).casefold() for k in ("name", "code")}]
        if exact:
            return exact
        return [p for p in rows if str(p.get("name", "")).casefold().endswith(needle)
                or any(needle == token for k in ("category", "brand_model", "specification")
                       for token in re.split(r"[\s/，,；;、]+", str(p.get(k, "")).casefold()))]

    def _amount_conflict(
        self, state: SessionState, computed: Decimal
    ) -> dict[str, Any] | None:
        stated = state.slots.get("stated_total_amount")
        if stated is not None and Decimal(stated) != computed:
            self._record(state, "AMOUNT_CONFLICT_DETECTED", stated_total=stated, computed_total=f"{computed:.2f}")
            return self._response(
                state, "ASK",
                f"数量 × 单价的总金额为 ¥{computed:.2f}，与您给出的总金额 ¥{stated} 冲突。"
                "请确认正确的数量、单价或总金额，核实前不会生成可执行确认。",
                "AMBIGUOUS",
            )
        return None

    def _explicit_request_ids(
        self, state: SessionState, proposal: dict[str, Any], latest_user_message: str
    ) -> set[str]:
        proposed = str(proposal.get("request_id") or "").strip()
        known_ids = set(state.known_requests) | set(state.recent_request_ids)
        known_ids.update(filter(None, (state.target_request_id, proposed)))
        if state.suspended_task and state.suspended_task.target_request_id:
            known_ids.add(state.suspended_task.target_request_id)
        explicit = {request_id for request_id in known_ids if re.search(
            r"(?<![A-Za-z0-9_-])" + re.escape(request_id) + r"(?![A-Za-z0-9_-])",
            latest_user_message,
        )}
        # Backend 允许文本 ID；识别当前明确编号，即使模型只给出了旧编号。
        tokens = re.findall(
            r"(?:申请(?:编号|ID)?|草稿(?:编号)?|查询|修改|提交|把|将)\s*[:：#]?\s*"
            r"([A-Za-z0-9][A-Za-z0-9_-]*)", latest_user_message,
        )
        reserved = self.SUBMIT_TERMINAL_STATES | {"DRAFT", "PO", "CNY", "M01", "M02", "U01"}
        explicit.update(token for token in tokens if token not in reserved)
        return explicit

    def _resolve_request_id(
        self, state: SessionState, proposal: dict[str, Any], latest_user_message: str
    ) -> str | None:
        proposed = str(proposal.get("request_id") or "").strip()
        explicit = self._explicit_request_ids(state, proposal, latest_user_message)
        if explicit:
            return next(iter(explicit)) if len(explicit) == 1 else None
        candidates = list(dict.fromkeys(state.candidate_request_ids))
        selected = self._candidate_from_message(state, candidates, latest_user_message)
        if selected and proposed:
            # 当前输入与模型提案必须共同收敛到同一个候选，避免 Runtime 代替用户猜选。
            return selected
        if len(candidates) > 1:
            # 模型仅从历史状态带回 request_id 不代表用户完成了候选选择。
            return None
        if len(candidates) == 1:
            return candidates[0]
        if state.target_request_id:
            return state.target_request_id
        if len(state.known_requests) == 1:
            return next(iter(state.known_requests))
        return None

    @staticmethod
    def _candidate_from_message(
        state: SessionState, candidates: list[str], message: str
    ) -> str | None:
        if not candidates:
            return None
        explicit = [request_id for request_id in candidates if request_id in message]
        if len(explicit) == 1:
            return explicit[0]
        ordinal_words = ("一", "二", "三", "四", "五", "六", "七", "八")
        for index, request_id in enumerate(candidates[:8]):
            number = index + 1
            markers = (
                f"第{number}个", f"第{number}条",
                f"第{ordinal_words[index]}个", f"第{ordinal_words[index]}条",
            )
            if any(marker in message for marker in markers):
                return request_id
        described: list[str] = []
        for request_id in candidates:
            detail = state.known_requests.get(request_id) or {}
            supplier = detail.get("supplier") or {}
            values = (
                detail.get("item"), detail.get("purpose"), supplier.get("name")
            )
            if any(
                isinstance(value, str) and len(value.strip()) >= 2 and value.strip() in message
                for value in values
            ):
                described.append(request_id)
        return described[0] if len(described) == 1 else None

    def _read_request_runtime(self, state: SessionState, request_id: str):
        result = self._call_api(state, "get_request", self.api.get_request, request_id=request_id)
        if result.data.get("request_id") != request_id:
            raise ProcurementTransportError("REQUEST_RECEIPT_ID_MISMATCH", uncertain=False)
        state.known_requests[request_id] = result.data
        state.target_request_id = request_id
        state.last_read_version = result.data.get("version")
        self._remember_reference(state, request_id)
        # 最新读取已经把当前对象收敛为唯一目标，不再保留旧的候选歧义。
        state.candidate_request_ids = []
        if state.uncertain_request_id == request_id:
            state.uncertain_request_id = None
        return result.data

    def _complete_query(
        self, state: SessionState, proposal: dict[str, Any], latest_user_message: str,
        *, finalize: bool = True
    ) -> dict[str, Any]:
        previous_query = (state.trusted_read_contexts.get("request") or {}).get("query") or {}
        # “查询预算不足申请的状态”仍是状态查询；只有实际询问余额/缺口或不足原因才扩展读取。
        budget_question = bool(re.search(
            r"预算.*(?:余额|还剩|剩余|缺口|差额|还差|多少|超)"
            r"|(?:为什么|为何).*预算.*(?:不足|不够)|预算.*(?:不足|不够).*(?:原因|怎么回事)"
            r"|(?:余额|缺口|超了多少钱)", latest_user_message))
        budget_follow_up = (previous_query.get("query_topic") == "budget"
                            and proposal.get("dialogue_relation") in {"follow_up", "correction"}
                            and re.search(r"预算|余额|还剩|还差|缺口|差多少|超了|多少钱|够不够", latest_user_message))
        request_id = self._resolve_request_id(state, proposal, latest_user_message)
        if (proposal.get("query_topic") == "budget" or budget_question or budget_follow_up
                or (request_id and "request_budget:" + request_id in state.turn_read_cache)):
            if not request_id or proposal.get("query_scope") == "collection":
                return self._response(state, "ASK", "请指定要解释预算的唯一申请编号；不提供任意或批量预算查询。",
                                      "REQUEST_OBJECT_NOT_UNIQUE", finalize=finalize)
            return self._complete_budget_query(state, request_id, latest_user_message, finalize=finalize)
        if proposal.get("query_scope") == "collection":
            try:
                data = state.turn_read_cache.get("list_requests")
                if data is None:
                    data = self._execute_read_tool(state, "list_requests", {}, latest_user_message)
            except ProcurementApiError as exc:
                return self._response(state, "STOP", f"无法查询采购申请列表：{exc.code}。", exc.code,
                                      finalize=finalize)
            except ProcurementTransportError:
                return self._response(state, "STOP", "查询服务暂不可用，请稍后重试。",
                                      "READ_SERVICE_UNAVAILABLE", finalize=finalize)
            requests = data.get("requests", [])
            # 后续序号选择须与本次展示顺序一致；唯一对象仍由原解析及写前核验决定。
            state.candidate_request_ids = [item["request_id"] for item in requests[:8]]
            lines = [f"当前身份可查看的采购申请，共 {len(requests)} 笔："]
            for index, item in enumerate(requests, 1):
                approval, po = item.get("approval") or {}, item.get("po") or {}
                lines.append(
                    f"{index}. 申请 {item['request_id']}：{item.get('item')} × {item.get('quantity')}，"
                    f"金额 ¥{item.get('amount')}，状态 {item.get('state')}，版本 v{item.get('version')}；"
                    f"当前审批人 {approval.get('current_approver') or '无待审批节点'}，最终审批级别 {approval.get('required_approver')}，PO：{po.get('po_id') or '尚未创建'}。"
                    f"{self._status_explanation(item)}"
                )
            if not requests:
                lines = ["当前身份没有可查看的采购申请。"]
            previous_query = (state.trusted_read_contexts.get("request") or {}).get("query") or {}
            user_goal = latest_user_message
            if (proposal.get("dialogue_relation") in {"follow_up", "correction"}
                    and previous_query.get("query_scope") == "collection"):
                user_goal = previous_query.get("user_goal") or user_goal
            self._remember_read_context(
                state, "request", "list_requests", {"query_scope": "collection", "user_goal": user_goal}, data
            )
            response = self._response(
                state, "STOP", "\n".join(lines), "READ_VERIFIED",
                result={"result_type": "requests", "query_scope": "collection", "requests": requests},
                finalize=finalize,
            )
            self._finish_task(state)
            return response
        request_id = self._resolve_request_id(state, proposal, latest_user_message)
        if not request_id:
            question = str(proposal.get("question") or "请提供申请编号；如果不确定，也可以描述要查询的是哪一笔。")
            return self._response(
                state, "ASK", question, "REQUEST_OBJECT_NOT_UNIQUE", finalize=finalize
            )
        try:
            detail = self._read_request_runtime(state, request_id)
        except ProcurementApiError as exc:
            reason = "REQUEST_NOT_FOUND" if exc.status == 404 else "REQUEST_ACCESS_DENIED" if exc.status == 403 else exc.code
            return self._response(
                state, "STOP", f"无法查询申请 {request_id}：{exc.code}。", reason,
                finalize=finalize,
            )
        except ProcurementTransportError:
            return self._response(
                state, "STOP", "查询服务暂不可用，请稍后重试。", "READ_SERVICE_UNAVAILABLE",
                finalize=finalize,
            )
        # 单笔读取覆盖此前集合范围，避免后续纠错错误恢复更早的列表目标。
        self._remember_read_context(state, "request", "get_request", {"request_id": request_id},
                                    model_safe_result("get_request", detail))
        approval = detail.get("approval") or {}
        po = detail.get("po")
        supplier = detail.get("supplier") or {}
        text = (
            f"申请 {request_id} 当前状态为 {detail.get('state')}，版本 v{detail.get('version')}；"
            f"{detail.get('item')} × {detail.get('quantity')}，金额 ¥{detail.get('amount')}；"
            f"供应商 {supplier.get('name') or '数据未提供'}"
            f"（状态 {supplier.get('status') or '数据未提供'}）；"
            f"当前审批人 {approval.get('current_approver') or '无待审批节点'}，最终审批级别 {approval.get('required_approver')}，"
            f"PO：{po.get('po_id') if isinstance(po, dict) else '尚未创建'}。"
            f"{self._status_explanation(detail)}"
        )
        response = self._response(
            state, "STOP", text, "READ_VERIFIED",
            result=self._result_view(detail), finalize=finalize,
        )
        self._finish_task(state)
        return response

    def _complete_budget_query(self, state: SessionState, request_id: str, message: str,
                               *, finalize: bool = True) -> dict[str, Any]:
        try:
            data = state.turn_read_cache.get("request_budget:" + request_id)
            if data is None:
                data = self._execute_read_tool(state, "get_request_budget", {"request_id": request_id}, message)
            amount, balance, shortfall = (Decimal(str(data[key])) for key in
                                          ("request_amount", "available_budget", "shortfall"))
            queried = datetime.fromisoformat(str(data["queried_at"]).replace("Z", "+00:00"))
            if (data["request_id"] != request_id or data.get("currency") != "CNY"
                    or not data.get("department") or not data.get("budget_id") or not data.get("request_state")
                    or queried.utcoffset() is None
                    or any(not x.is_finite() or x < 0 or x.as_tuple().exponent < -2 for x in (amount, balance, shortfall))
                    or shortfall != max(amount-balance, Decimal(0))):
                raise ValueError("INVALID_BUDGET_VIEW")
        except (ProcurementApiError, ProcurementTransportError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
            return self._response(state, "STOP", f"无法读取申请 {request_id} 的关联部门预算（{getattr(exc,'code',type(exc).__name__)}）。"
                                  "未取得完整可信数据，不能提供余额或计算当前缺口。", "BUDGET_READ_FAILED", finalize=finalize)
        text = (f"申请 {request_id} 关联{data['department']}预算 {data['budget_id']}，当前申请状态 {data['request_state']}。"
                f"余额查询时点 {data['queried_at']}（UTC）：当前可用预算 ¥{balance:.2f}，本次申请金额 ¥{amount:.2f}；"
                f"按此时余额计算的预算缺口为 ¥{shortfall:.2f}（申请金额减当前余额，最低为0）。")
        if data["request_state"] == "BLOCKED_BUDGET":
            text += "申请因预算不足被阻断，尚未正常提交审批。"
        text += (f"按当前余额还差 ¥{shortfall:.2f}。" if shortfall > 0 else "当前余额已足够覆盖本次金额，但不会自动改变申请状态或重新提交。")
        text += "这是查询时的余额和缺口，不是提交时的历史缺口；本次只读查询未扣减预算或执行任何业务写入。"
        total_question = bool(re.search(r"预算总额|总预算|(?:不是|原本|原来|不是说).*(?:预算|万)|预算.*(?:总共|总计|一共)", message))
        if total_question:
            text = ("当前可用余额不等于预算总额。现有授权读取只提供当前余额，不能核实您提到的预算总额，"
                    "也不能据此计算已使用金额；需要采购负责人提供预算总额依据。\n" + text)
        self._remember_reference(state, request_id)
        self._remember_read_context(state, "request", "get_request_budget",
                                    {"request_id": request_id, "query_topic": "budget"}, data)
        self._record(state, "BUDGET_READ_VERIFIED", request_id=request_id, budget=data)
        response = self._response(state, "ASK" if total_question else "STOP", text,
                                  "BUDGET_TOTAL_UNVERIFIED" if total_question else "READ_VERIFIED",
                                  result={"result_type": "request_budget", **data}, finalize=finalize)
        if not total_question:
            self._finish_task(state)
        return response

    def _complete_consultation(
        self, state: SessionState, *, finalize: bool = True, direct_answer: str | None = None,
        deferred_write: bool = False,
        history_fields: list[str] | None = None, reference_index: int | None = None,
        clarification_question: str | None = None,
        target_object: str | None = None,
    ) -> dict[str, Any]:
        """只根据本轮工具证据组织咨询答复，不接受模型自由生成价格或制度。"""
        # 缺证据的咨询补问需留存；已有证据仍走原回答聚合，不能因模型 question 丢弃已取得的事实。
        # 已完成的历史列表查询不缺物品条件；字段/序号歧义仍由下方校验处理。
        tool_objects = {"search_products": "product", "search_purchase_history": "purchase_history",
                        "search_knowledge": "knowledge", "get_procurement_rules": "knowledge"}
        evidence = state.consultation_evidence
        if target_object in set(tool_objects.values()):
            evidence = [e for e in evidence if tool_objects.get(e["tool"]) == target_object]
            excluded = len(state.consultation_evidence) - len(evidence)
            if excluded:
                self._record(state, "READ_EVIDENCE_EXCLUDED", target_object=target_object, count=excluded)
        elif len({tool_objects.get(e["tool"]) for e in evidence}) > 1:
            return self._response(state, "ASK", "本轮读取了不同对象的信息，请明确要回答商品目录、历史成交还是制度问题。",
                                  "READ_GOAL_NOT_UNIQUE", finalize=finalize)
        if not evidence:
            return self._response(
                state, "ASK", str(clarification_question or "请说明要查询的物品、历史价格或采购制度主题。"),
                "CONSULTATION_SOURCE_REQUIRED", finalize=finalize,
            )
        sections: list[str] = []
        result: dict[str, Any] = {
            "result_type": "consultation", "history": [], "rules": [],
            "products": [], "knowledge": [], "citations": [],
        }
        knowledge_hits: dict[str, dict[str, Any]] = {}
        knowledge_query = ""
        knowledge_requested = False
        history_evidence = [e for e in evidence if e["tool"] == "search_purchase_history"]
        if history_evidence:
            if (history_fields is not None and (not isinstance(history_fields, list)
                    or any(not isinstance(key, str) or key not in HISTORY_FIELDS for key in history_fields))):
                return self._response(state, "ASK", "请明确需要了解的历史成交字段。",
                                      "HISTORY_FIELDS_INVALID", finalize=finalize)
            fields = list(dict.fromkeys(history_fields or []))
            # 只对可信工具证据去重和投影；模型回答以及提案里的其他值都不是成交事实。
            records = list({json.dumps(record, sort_keys=True, ensure_ascii=False): record
                            for e in history_evidence for record in e["data"].get("records", [])}.values())
            if reference_index is not None:
                if type(reference_index) is not int or not 1 <= reference_index <= len(records):
                    return self._response(state, "ASK", "请指出可信成交列表中的有效记录序号。",
                                          "READ_REFERENCE_INDEX_INVALID", finalize=finalize)
                records = [records[reference_index - 1]]
            if fields and len(records) > 1:
                return self._response(state, "ASK", "当前有多条历史成交记录，请明确要追问第几条。",
                                      "READ_REFERENCE_NOT_UNIQUE", finalize=finalize)
            result["history"] = records
            result["display_mode"] = "fields" if fields else "full"
            result["requested_fields"] = fields
            unfiltered = all(not e["data"].get("query") for e in history_evidence)
            scope_note = "未限定物品：按后端排序查询当前身份可见、已创建 PO 的最近最多20条记录，仅为最近记录列表，非全量历史。"
            if unfiltered:
                result["query_scope"] = {"item": None, "limit": 20, "completed_po_only": True}
            if records:
                summary = "历史成交记录（仅供参考，不代表实时市场价）："
                if unfiltered and not fields:
                    summary = scope_note + "\n" + summary
                result["summary"] = summary
                if fields:
                    record = records[0]
                    parts = []
                    for key in fields:
                        value = ((record.get("source") or {}).get(key) or record.get(key)
                                 if key in {"request_id", "po_id"} else record.get(key))
                        if value is None or value == "":
                            parts.append(f"{HISTORY_FIELDS[key]}：可信成交记录中未提供")
                        elif key == "quantity":
                            parts.append(f"采购数量：{value} 件（{record.get('item_spec') or '该物品'}）")
                        elif key in {"unit_price", "amount"}:
                            parts.append(f"{HISTORY_FIELDS[key]}：¥{value} {record.get('currency') or ''}".rstrip())
                        else:
                            parts.append(f"{HISTORY_FIELDS[key]}：{value}")
                    answer = "；".join(parts) + "。"
                    if {"unit_price", "amount"} & set(fields):
                        source = record.get("source") or {}
                        answer += ("仅供历史参考，不代表实时市场价；来源：申请 "
                                   f"{source.get('request_id') or '未记录'} / PO {source.get('po_id') or '未记录'}。")
                    sections.append(answer)
                else:
                    lines = []
                    for record in records:
                        source = record.get("source") or {}
                        lines.append(
                            f"{record.get('item_spec')}，{record.get('quantity')} 件，"
                            f"历史成交单价 ¥{record.get('unit_price')} {record.get('currency')}，"
                            f"成交日期 {record.get('purchase_date') or '日期未记录'}，供应商 {record.get('supplier_name') or '未记录'}；"
                            f"来源：申请 {source.get('request_id')} / PO {source.get('po_id')}"
                        )
                    sections.append(summary + "\n" + "\n".join(lines))
            else:
                sections.append((scope_note + "\n" if unfiltered else "")
                                + "未查询到符合条件且已创建 PO 的历史成交记录，因此不提供推测价格。")
            self._record(state, "HISTORY_ANSWER_PREPARED", requested_fields=fields,
                         record_count=len(records), display_mode=result["display_mode"])
        needs_clarification = False
        for entry in evidence:
            data = entry["data"]
            if entry["tool"] == "search_purchase_history":
                continue
            elif entry["tool"] == "get_procurement_rules":
                rules = data.get("rules", [])
                result["rules"].extend(rules)
                source = f"{data.get('source_name')} {data.get('document_id')} v{data.get('version')}"
                if rules:
                    lines = [
                        f"{rule.get('rule_id')} {rule.get('title')}：{rule.get('content')}（来源：{source}）"
                        for rule in rules
                    ]
                    sections.append("已确认采购规则：\n" + "\n".join(lines))
                else:
                    sections.append(f"可信规则文档 {source} 中没有匹配条目。")
                    needs_clarification = True
            elif entry["tool"] == "search_products":
                products = self._product_candidates(data.get("products", []), data.get("query") or "")
                result["products"].extend(products)
                if products:
                    lines = [
                        f"{item.get('code')} {item.get('name')}，{item.get('brand_model')}，"
                        f"{item.get('specification')}，采购价 ¥{item.get('purchase_price')} {item.get('currency')} / {item.get('unit')}，"
                        f"目录供应商 {(item.get('supplier') or {}).get('name') or '未维护'}，"
                        f"状态 {item.get('status')}"
                        for item in products
                    ]
                    sections.append("商品目录（采购价与供应商由采购负责人维护）：\n"+"\n".join(lines))
                else:
                    recalled = data.get("products", [])
                    labels = '、'.join(f"{p.get('name')}（{p.get('code')}）" for p in recalled[:10])
                    sections.append((f"仅找到关键词召回：{labels}，不能确定为您所需商品，请指定完整商品名称或编码。"
                                     if recalled else "商品目录中没有匹配条目，请核对商品名称或编码。"))
                    needs_clarification = True
            elif entry["tool"] == "search_knowledge":
                knowledge_requested = True
                hits = data.get("hits", [])
                result["knowledge"].extend(hits)
                if data.get("evidence_sufficient") and hits:
                    knowledge_query = data.get("query") or knowledge_query
                    # 同轮检索针对同一用户问题：汇合有效证据后只生成一次，避免局部缺证答复互相矛盾。
                    for hit in hits:
                        key = hit.get("chunk_id") or json.dumps(hit, ensure_ascii=False, sort_keys=True)
                        knowledge_hits.setdefault(key, hit)
        if knowledge_requested:
            hits = list(knowledge_hits.values())
            if not hits:
                self._record(state, "KNOWLEDGE_ANSWER_REFUSED", reason="INSUFFICIENT_EVIDENCE",
                             direct_turn_received=direct_answer is not None)
                sections.append("当前已审校知识库中没有足够证据支持该问题，因此不作推测性回答。")
                needs_clarification = True
            else:
                if direct_answer is not None:
                    answer = direct_answer
                    self._record(state, "KNOWLEDGE_ANSWER_GENERATED", answer_source="direct_read_turn",
                                 model_name=self.model_name, cited_chunk_ids=[hit.get("chunk_id") for hit in hits])
                else:
                    answer = self._generate_knowledge_answer(state, knowledge_query, hits)
                result["citations"] = [
                    {key: hit.get(key) for key in (
                        "chunk_id", "doc_id", "title", "heading", "class",
                        "source", "source_url", "score",
                    )} for hit in hits
                ]
                source_lines = [
                    f"- [{hit.get('doc_id')}｜{hit.get('title')}｜{hit.get('heading')}]"
                    + (f" {hit.get('source_url')}" if hit.get("source_url") else f" {hit.get('source')}")
                    for hit in hits
                ]
                if any(hit.get("class") == "A" for hit in hits):
                    answer += "\n\n说明：A 类文档解释已确认规则；如与当前后端实时校验结果不一致，以后端现行规则和真实业务返回为准。"
                sections.append(answer + "\n\n来源：\n" + "\n".join(source_lines))
        # 商品和成交事实只由工具结果投影，不追加未经核验的模型自由文本。
        if deferred_write:
            sections.append("已保留您的写入请求和已核实字段。本轮仅完成只读咨询，未生成写入确认、未执行写入。请下一轮明确继续办理，再核验并确认具体操作。")
        populated = [
            name for name in ("history", "products", "knowledge", "rules") if result[name]
        ]
        result["result_type"] = populated[0] if len(populated) == 1 else "consultation"
        response = self._response(
            state, "ASK" if deferred_write or needs_clarification else "STOP", "\n\n".join(sections),
            "MIXED_WRITE_DEFERRED" if deferred_write else "READ_GOAL_UNRESOLVED" if needs_clarification else "READ_VERIFIED", result=result,
            finalize=finalize,
        )
        if not deferred_write and not needs_clarification:
            self._finish_task(state)
        return response

    def _generate_knowledge_answer(
        self, state: SessionState, query: str, hits: list[dict[str, Any]]
    ) -> str:
        """使用现有模型基于已检索证据生成答案；来源清单仍由 Runtime 确定性追加。"""
        evidence = [
            {
                key: hit.get(key) for key in (
                    "doc_id", "title", "heading", "class", "text", "source", "source_url"
                )
            }
            for hit in hits
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是采购知识回答模块。只能依据给定证据，用中文简洁回答；不得补充证据中没有的制度、价格、"
                    "库存、供应商状态或业务状态。A 类是模拟企业已确认规则，B 类仅是通用建议。"
                    "若证据不足，明确说知识资料不足。不要声称执行了任何写操作。"
                ),
            },
            {
                "role": "user",
                "content": "问题：" + query + "\n证据：" + json.dumps(evidence, ensure_ascii=False),
            },
        ]
        try:
            turn = self.model.complete(messages, [])
        except Exception as exc:
            self._record(
                state, "KNOWLEDGE_ANSWER_FAILED", outcome_status=type(exc).__name__
            )
            return "已检索到相关知识，但模型未能完成基于证据的回答；请根据下列来源人工核对。"
        self._record(
            state, "KNOWLEDGE_ANSWER_GENERATED", model_name=self.model_name,
            token_usage=turn.usage, cited_chunk_ids=[hit.get("chunk_id") for hit in hits],
        )
        if turn.tool_calls or not (turn.content or "").strip():
            return "已检索到相关知识，但模型未返回可验证的文字答案；请根据下列来源人工核对。"
        return (turn.content or "").strip()

    def _prepare_submit(
        self, state: SessionState, proposal: dict[str, Any], latest_user_message: str
    ) -> dict[str, Any]:
        request_id = self._resolve_request_id(state, proposal, latest_user_message)
        if not request_id:
            return self._response(
                state, "ASK", str(proposal.get("question") or "请提供要提交的 DRAFT 申请编号。"),
                "REQUEST_OBJECT_NOT_UNIQUE",
            )
        try:
            detail = self._read_request_runtime(state, request_id)
        except ProcurementApiError as exc:
            reason = "REQUEST_NOT_FOUND" if exc.status == 404 else "REQUEST_ACCESS_DENIED" if exc.status == 403 else exc.code
            return self._response(state, "STOP", f"无法读取申请 {request_id}：{exc.code}。", reason)
        except ProcurementTransportError:
            return self._response(state, "STOP", "读取最新草稿状态失败，未执行提交。", "READ_SERVICE_UNAVAILABLE")
        if detail.get("po"):
            return self._response(state, "STOP", "该申请已创建 PO，不能作为草稿再次提交。", "PO_ALREADY_CREATED")
        if detail.get("state") != "DRAFT":
            return self._response(
                state, "STOP",
                f"申请 {request_id} 当前状态为 {detail.get('state')}，不是可续办提交的 DRAFT。",
                "INVALID_STATE",
            )
        missing = detail.get("missing_fields") or []
        if missing:
            return self._response(
                state, "ASK",
                f"申请 {request_id} 仍缺少字段：{'、'.join(missing)}。请先补全草稿后再提交。",
                "MISSING_REQUIRED_SLOTS",
            )
        supplier = detail.get("supplier") or {}
        summary = (
            f"提交已有草稿 {request_id}（当前 v{detail.get('version')}）："
            f"{detail.get('item')} × {detail.get('quantity')}，单价 ¥{detail.get('unit_price')}，"
            f"金额 ¥{detail.get('amount')}，供应商 {supplier.get('name') or '未记录'}，"
            f"用途：{detail.get('purpose')}。提交后将按规则进入业务校验和人工审批。"
        )
        return self._set_pending(
            state, "submit", request_id, {}, supplier,
            int(detail["version"]), detail, summary,
        )

    def _prepare_update(
        self, state: SessionState, proposal: dict[str, Any], latest_user_message: str
    ) -> dict[str, Any]:
        request_id = self._resolve_request_id(state, proposal, latest_user_message)
        if not request_id:
            return self._response(
                state, "ASK", str(proposal.get("question") or "请提供要修改的申请编号。"),
                "REQUEST_OBJECT_NOT_UNIQUE",
            )
        try:
            detail = self._read_request_runtime(state, request_id)
        except ProcurementApiError as exc:
            return self._response(state, "STOP", f"无法读取申请 {request_id}：{exc.code}。", exc.code)
        except ProcurementTransportError:
            return self._response(state, "STOP", "读取最新申请状态失败，未执行修改。", "READ_SERVICE_UNAVAILABLE")
        if detail.get("po") or detail.get("state") == "PO_CREATED":
            return self._response(state, "STOP", "该申请已创建 PO，不能修改关键字段。", "PO_ALREADY_CREATED")

        changes: dict[str, Any] = {}
        # 首次补齐对象后继续使用同一未完成任务中已收集的变更；切换对象已在上游隔离。
        effective = {**state.slots, **proposal}
        for name in self.CHANGE_FIELDS:
            if name in effective:
                changes[name] = effective[name]
        if "relative_quantity_delta" in effective:
            current = detail.get("quantity")
            delta = effective["relative_quantity_delta"]
            if type(current) is not int or type(delta) is not int:
                return self._response(state, "ASK", "无法确定当前数量，请明确新的最终数量。", "RELATIVE_VALUE_UNRESOLVED")
            changes["quantity"] = current + delta
        if not changes:
            return self._response(state, "ASK", str(proposal.get("question") or "请说明要修改的字段和新值。"), "NO_UPDATE_FIELDS")
        try:
            changes = self._validate_changes(changes)
        except ValueError as exc:
            return self._response(state, "ASK", f"修改参数无效：{exc}。", "INVALID_UPDATE_VALUE")
        if changes.get("supplier_name") == detail.get("supplier", {}).get("name"):
            # 最新业务事实中的未变供应商不是变更，不要求重复匹配；真正换供应商仍按原安全规则核实。
            changes.pop("supplier_name", None)
        if "supplier_name" in changes:
            supplier = self._supplier_candidate(state, changes["supplier_name"])
            if not supplier:
                return self._response(state, "ASK", "需要先核实新的供应商完整名称。", "SUPPLIER_NOT_UNIQUELY_VERIFIED")
            changes["supplier_name"] = supplier["name"]
        else:
            supplier = None
        diffs = []
        for name, value in changes.items():
            old = detail.get("supplier", {}).get("name") if name == "supplier_name" else detail.get(name)
            if str(old) != str(value):
                diffs.append(f"{name}: {old} → {value}")
        if not diffs:
            return self._response(state, "STOP", "申请字段没有实际变化。", "NO_CHANGE")
        new_quantity = changes.get("quantity", detail.get("quantity"))
        new_price = changes.get("unit_price", detail.get("unit_price"))
        amount = Decimal(str(new_price)) * int(new_quantity)
        conflict = self._amount_conflict(state, amount)
        if conflict:
            return conflict
        summary = (
            f"修改申请 {request_id}（当前 v{detail.get('version')}）："
            + "；".join(diffs)
            + f"。修改后金额 ¥{amount:.2f}，已有审批将失效并按规则重新审批。"
        )
        return self._set_pending(
            state, "update", request_id, changes, supplier,
            int(detail["version"]), detail, summary,
        )

    def _validate_changes(self, changes: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for name, value in changes.items():
            if name not in self.CHANGE_FIELDS:
                raise ValueError("不支持的字段")
            if name == "quantity":
                if type(value) is not int or value <= 0:
                    raise ValueError("数量必须为正整数")
                clean[name] = value
            elif name == "unit_price":
                try:
                    price = Decimal(str(value))
                    if not price.is_finite() or price < 0 or price.as_tuple().exponent < -2:
                        raise InvalidOperation
                except (InvalidOperation, ValueError, TypeError):
                    raise ValueError("单价必须是最多两位小数的非负金额") from None
                clean[name] = f"{price:.2f}"
            else:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{name} 不能为空")
                clean[name] = value.strip()
        return clean

    def _set_pending(
        self,
        state: SessionState,
        task_type: str,
        request_id: str,
        args: dict[str, Any],
        supplier: dict[str, Any] | None,
        expected_version: int | None,
        old_snapshot: dict[str, Any] | None,
        summary: str,
    ) -> dict[str, Any]:
        action_id = "act-" + uuid.uuid4().hex
        base = {
            "actor": state.actor_identity, "task_type": task_type,
            "target_request_id": request_id, "args": args,
            "supplier_match": supplier, "expected_version": expected_version,
        }
        pending = PendingAction(
            action_id=action_id,
            run_id=state.current_run_id or ("run-" + uuid.uuid4().hex),
            actor=state.actor_identity,
            task_type=task_type,
            target_request_id=request_id,
            args=args,
            supplier_match=supplier,
            expected_version=expected_version,
            old_snapshot=old_snapshot,
            created_at=time.time(),
            expires_at=time.time() + self.confirm_ttl_seconds,
            fingerprint=self._fingerprint_payload(base),
            summary=summary,
        )
        state.pending_action = pending
        state.confirmation_state = "PENDING"
        state.operation_key = action_id
        state.target_request_id = request_id
        self._record(state, "CONFIRMATION_ISSUED", action_id=action_id, fingerprint=pending.fingerprint)
        return self._response(
            state, "CONFIRM", summary, "READY_FOR_CONFIRMATION",
            confirmation={
                "action_id": action_id, "summary": summary,
                "expires_at": datetime.fromtimestamp(pending.expires_at, timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )

    def confirm(self, state: SessionState, action_id: str, decision: str) -> dict[str, Any]:
        if action_id in state.completed_operations:
            cached = state.completed_operations[action_id]
            request_id = (cached.get("result") or {}).get("request_id")
            if not request_id:
                return cached
            # 重复确认只查最新事实，不把历史成功回执当作当前状态，也不改变活动任务。
            replay = self.new_session(state.session_id)
            replay.current_run_id = "run-" + uuid.uuid4().hex
            detail = self._try_get(replay, request_id)
            if detail is None:
                return self._unknown_result(replay, request_id, "REPLAY_STATE_UNAVAILABLE")
            state.known_requests[request_id] = detail
            if state.uncertain_request_id == request_id:
                state.uncertain_request_id = None
            if state.target_request_id == request_id and state.active_task_type is None:
                state.last_read_version = detail.get("version")
            view = self._result_view(detail)
            if (view == cached.get("result") and cached.get("outcome_status") in {"SUCCESS", "BLOCKED"}
                    and state.active_task_type is None and state.uncertain_request_id is None):
                return cached
            return self._response(
                replay, "STOP",
                f"该确认已处理，未重复执行写入。申请 {request_id} 当前状态 {detail.get('state')}，"
                f"版本 v{detail.get('version')}。{self._status_explanation(detail)}",
                "READ_VERIFIED", result=view,
            )
        pending = state.pending_action
        if not pending or pending.action_id != action_id:
            return self._response(state, "STOP", "确认已失效或不存在。", "CONFIRMATION_NOT_FOUND")
        state.current_run_id = pending.run_id
        if decision == "cancel":
            self._record(state, "CONFIRMATION_REJECTED", action_id=action_id)
            state.pending_action = None
            state.confirmation_state = "REJECTED"
            response = self._response(state, "STOP", "已取消，未执行任何写入。", "USER_REJECTED")
            self._finish_task(state, clear_target=pending.task_type == "create")
            return response
        if decision != "confirm":
            return self._response(state, "ASK", "请明确确认或取消。", "INVALID_CONFIRMATION_DECISION")
        if time.time() > pending.expires_at:
            self._invalidate_confirmation(state, "CONFIRMATION_EXPIRED")
            return self._response(state, "STOP", "确认已过期，未执行写入，请重新发起。", "CONFIRMATION_EXPIRED")
        if pending.actor != state.actor_identity or pending.fingerprint != self._pending_fingerprint(pending):
            self._invalidate_confirmation(state, "CONFIRMATION_BINDING_MISMATCH")
            return self._response(state, "STOP", "确认绑定信息已变化，未执行写入。", "CONFIRMATION_BINDING_MISMATCH")

        state.confirmation_state = "CONSUMED"
        state.write_started = False
        self._record(state, "CONFIRMATION_ACCEPTED", action_id=action_id, fingerprint=pending.fingerprint)
        try:
            if pending.task_type == "create":
                response = self._act_create(state, pending)
            elif pending.task_type == "submit":
                response = self._act_submit(state, pending)
            else:
                response = self._act_update(state, pending)
        except Exception as exc:  # 写后异常不能被报告成确定失败，也不能直接重放。
            self._record(state, "ACT_FAILED", action_id=action_id, outcome_status=type(exc).__name__)
            if state.write_started:
                response = self._unknown_result(state, pending.target_request_id, "WRITE_VERIFICATION_UNAVAILABLE")
            else:
                response = self._response(
                    state, "STOP", "写入前校验未完成，本次未执行写入，请重新查询后操作。",
                    "PRE_WRITE_CHECK_FAILED", outcome_status="FAILED",
                )
        state.pending_action = None
        state.completed_operations[action_id] = response
        retry_request = state.retry_submit_request_id if response.get("route_reason_code") == "CREATE_SUBMIT_PARTIAL" else None
        state.retry_submit_request_id = None
        self._finish_task(state)
        if retry_request:
            state.retry_submit_request_id = retry_request
            state.active_task_type = "submit"
            state.target_request_id = retry_request
        return response

    def _act_create(self, state: SessionState, pending: PendingAction) -> dict[str, Any]:
        try:
            product = self._catalog_product(state, pending.args["product_code"])
        except (ValueError, ProcurementApiError, ProcurementTransportError):
            return self._response(state,"STOP","目录数据已失效或无法核实，请联系采购负责人维护并重新生成确认；未创建草稿。","CATALOG_CHANGED")
        if (product["version"] != pending.args["catalog_version"] or product["name"] != pending.args["item"]
                or product["purchase_price"] != pending.args["unit_price"]
                or product["supplier"]["id"] != (pending.supplier_match or {}).get("id")):
            return self._response(state,"STOP","商品目录已变化，原确认失效，请重新发起确认；未创建草稿。","CATALOG_CHANGED")
        # 执行前重新匹配，避免确认后供应商对象变化。
        match = self._call_api(
            state, "match_supplier", self.api.match_supplier,
            name=pending.args["supplier_name"],
        ).data.get("matches", [])
        exact = [item for item in match if item.get("name") == pending.args["supplier_name"]]
        if len(exact) != 1 or exact[0].get("id") != (pending.supplier_match or {}).get("id"):
            return self._response(state, "STOP", "供应商匹配已变化，原确认失效。", "SUPPLIER_CHANGED")
        payload = {"request_id": pending.target_request_id, **pending.args}
        try:
            created = self._create_with_recovery(state, payload)
        except ProcurementApiError as exc:
            if exc.status >= 500:
                return self._unknown_result(state, pending.target_request_id, "CREATE_RESULT_UNCERTAIN")
            return self._response(
                state, "STOP", f"创建申请被服务端拒绝：{exc.code}。本次未完成创建。",
                "CREATE_REJECTED", outcome_status="FAILED",
            )
        if not created:
            return self._unknown_result(state, pending.target_request_id, "CREATE_RESULT_UNCERTAIN")
        detail = self._read_request_runtime(state, pending.target_request_id)
        if not self._matches_create(detail, pending.args):
            return self._unknown_result(state, pending.target_request_id, "CREATE_SNAPSHOT_MISMATCH")
        version = detail.get("version")
        return self._submit_and_verify(state, pending.target_request_id, version, created=True)

    def _create_with_recovery(self, state: SessionState, payload: dict[str, Any]) -> bool:
        for attempt in range(self.write_retry_limit + 1):
            try:
                self._call_api(state, "create_request", self.api.create_request, payload=payload)
                return True
            except ProcurementApiError as exc:
                if exc.status >= 500:
                    detail = self._try_get(state, payload["request_id"])
                    return bool(detail and self._matches_create(detail, payload))
                if exc.code != "REQUEST_EXISTS":
                    raise
                detail = self._try_get(state, payload["request_id"])
                return bool(detail and self._matches_create(detail, payload))
            except ProcurementTransportError:
                detail = self._try_get(state, payload["request_id"])
                if detail:
                    return self._matches_create(detail, payload)
                # 未取得可靠回读时终止本次操作；保留原 ID，先查证再续办。
                return False
        return False

    def _submit_with_recovery(
        self, state: SessionState, request_id: str, expected_version: int,
        *, surface_deterministic_error: bool = False,
    ) -> bool:
        terminal = self.SUBMIT_TERMINAL_STATES
        for attempt in range(self.write_retry_limit + 1):
            try:
                self._call_api(
                    state, "submit_request", self.api.submit_request,
                    request_id=request_id, expected_version=expected_version,
                )
                return True
            except ProcurementApiError as exc:
                if exc.status >= 500:
                    detail = self._try_get(state, request_id)
                    return bool(detail and detail.get("version") == expected_version
                                and detail.get("state") in terminal)
                if exc.code in {"VERSION_CONFLICT", "INVALID_STATE"}:
                    detail = self._try_get(state, request_id)
                    completed = bool(
                        detail and detail.get("version") == expected_version
                        and detail.get("state") in terminal
                    )
                    if completed:
                        return True
                    if surface_deterministic_error:
                        raise
                    return False
                raise
            except ProcurementTransportError:
                detail = self._try_get(state, request_id)
                if detail and detail.get("version") == expected_version and detail.get("state") in terminal:
                    return True
                return False
        return False

    def _submit_and_verify(
        self, state: SessionState, request_id: str, expected_version: int, *, created: bool = False
    ) -> dict[str, Any]:
        error: str | None = None
        retry_allowed = True
        try:
            completed = self._submit_with_recovery(
                state, request_id, expected_version, surface_deterministic_error=True
            )
        except ProcurementApiError as exc:
            if exc.status >= 500:
                return self._unknown_result(state, request_id, "SUBMIT_RESULT_UNCERTAIN", created=created)
            completed = True
            error = exc.code
            retry_allowed = exc.status != 403
        if not completed:
            return self._unknown_result(state, request_id, "SUBMIT_RESULT_UNCERTAIN", created=created)
        final = self._try_get(state, request_id)
        if final is None:
            return self._unknown_result(state, request_id, "SUBMIT_VERIFICATION_UNAVAILABLE", created=created)
        if final.get("version") != expected_version:
            if error == "VERSION_CONFLICT":
                return self._submission_failed(state, final, error, created=created, retry_allowed=retry_allowed)
            return self._unknown_result(state, request_id, "SUBMIT_VERSION_MISMATCH", created=created)
        if final.get("state") not in self.SUBMIT_TERMINAL_STATES:
            if final.get("state") != "DRAFT" and error is None:
                return self._unknown_result(state, request_id, "SUBMIT_FINAL_STATE_MISMATCH", created=created)
            return self._submission_failed(
                state, final, error or "SUBMIT_FINAL_STATE_MISMATCH", created=created, retry_allowed=retry_allowed
            )
        # HTTP 成功只代表有回执；用户目标是否完成由后端终态决定。
        business_state = final["state"]
        outcome = "SUCCESS" if business_state in {"SUBMITTED", "PENDING_HIGH_APPROVAL"} else "BLOCKED"
        text = (
            ("采购申请已经创建。" if created else "")
            + f"申请 {request_id} 已核验，当前状态 {business_state}，版本 v{final.get('version')}，"
            + f"当前审批人 {(final.get('approval') or {}).get('current_approver') or '无待审批节点'}，"
            + f"最终审批级别 {(final.get('approval') or {}).get('required_approver')}。"
            + self._status_explanation(final)
        )
        return self._response(
            state, "STOP", text, "WRITE_VERIFIED", result=self._result_view(final), outcome_status=outcome
        )

    def _submission_failed(
        self, state: SessionState, final: dict[str, Any], reason: str, *, created: bool, retry_allowed: bool = True
    ) -> dict[str, Any]:
        request_id = final["request_id"]
        can_retry = created and retry_allowed and final.get("state") == "DRAFT" and not final.get("po")
        if can_retry:
            state.retry_submit_request_id = request_id
        text = (
            ("采购申请已经创建，已保留原申请。" if created else "")
            + f"申请编号 {request_id}，当前实际状态 {final.get('state')}，版本 v{final.get('version')}。"
            + f"{'自动' if created else ''}提交失败，原因：{reason}。"
            + ("是否重新发起提交？请回复“重新提交”或“暂不提交”；重提仍需再次确认。" if can_retry else
               "请先查询并处理当前状态，再重新发起操作。")
        )
        return self._response(
            state, "ASK" if can_retry else "STOP", text,
            "CREATE_SUBMIT_PARTIAL" if created else reason,
            result=self._result_view(final), outcome_status="PARTIAL_SUCCESS" if created else "FAILED",
        )

    def _act_submit(self, state: SessionState, pending: PendingAction) -> dict[str, Any]:
        current = self._read_request_runtime(state, pending.target_request_id)
        if current.get("po"):
            return self._response(state, "STOP", "该申请已创建 PO，原确认失效。", "PO_ALREADY_CREATED")
        if current.get("version") != pending.expected_version:
            self._invalidate_confirmation(state, "VERSION_CONFLICT")
            return self._response(
                state, "STOP", "申请版本已变化，原确认失效，请重新读取后确认。", "VERSION_CONFLICT"
            )
        if current.get("state") != "DRAFT":
            self._invalidate_confirmation(state, "INVALID_STATE")
            return self._response(
                state, "STOP",
                f"申请当前状态已变为 {current.get('state')}，不再允许按原确认提交。",
                "INVALID_STATE",
            )
        if current.get("missing_fields"):
            self._invalidate_confirmation(state, "MISSING_REQUIRED_SLOTS")
            return self._response(
                state, "STOP", "申请字段已变化且不完整，原确认失效。", "MISSING_REQUIRED_SLOTS"
            )
        return self._submit_and_verify(state, pending.target_request_id, int(pending.expected_version or 0))

    def _act_update(self, state: SessionState, pending: PendingAction) -> dict[str, Any]:
        current = self._read_request_runtime(state, pending.target_request_id)
        if current.get("po") or current.get("state") == "PO_CREATED":
            return self._response(state, "STOP", "该申请已创建 PO，原确认失效。", "PO_ALREADY_CREATED")
        if current.get("version") != pending.expected_version:
            self._invalidate_confirmation(state, "VERSION_CONFLICT")
            return self._response(state, "STOP", "申请版本已变化，原确认失效，请重新核实后确认。", "VERSION_CONFLICT")
        try:
            success = self._update_with_recovery(state, pending, current)
        except ProcurementApiError as exc:
            if exc.status >= 500:
                return self._unknown_result(state, pending.target_request_id, "UPDATE_RESULT_UNCERTAIN")
            latest = self._try_get(state, pending.target_request_id)
            if latest is None:
                return self._unknown_result(state, pending.target_request_id, "UPDATE_VERIFICATION_UNAVAILABLE")
            return self._response(
                state, "STOP", f"修改被服务端拒绝：{exc.code}。当前状态 {latest.get('state')}，版本 v{latest.get('version')}。", exc.code,
                result=self._result_view(latest), outcome_status="FAILED",
            )
        if not success:
            return self._unknown_result(state, pending.target_request_id, "UPDATE_RESULT_UNCERTAIN")
        final = self._try_get(state, pending.target_request_id)
        if final is None:
            return self._unknown_result(state, pending.target_request_id, "UPDATE_VERIFICATION_UNAVAILABLE")
        if not self._matches_update_invariants(final, current, pending.args):
            return self._unknown_result(state, pending.target_request_id, "UPDATE_SNAPSHOT_MISMATCH")
        text = (
            f"申请 {pending.target_request_id} 已修改并核验，当前版本 v{final.get('version')}，"
            f"状态 {final.get('state')}，金额 ¥{final.get('amount')}。"
        )
        return self._response(state, "STOP", text, "WRITE_VERIFIED", result=self._result_view(final))

    def _update_with_recovery(
        self, state: SessionState, pending: PendingAction, old_detail: dict[str, Any]
    ) -> bool:
        expected = int(pending.expected_version or 0)
        for attempt in range(self.write_retry_limit + 1):
            try:
                self._call_api(
                    state, "update_request", self.api.update_request,
                    request_id=pending.target_request_id,
                    fields=pending.args,
                    expected_version=expected,
                )
                return True
            except ProcurementApiError as exc:
                if exc.status >= 500:
                    detail = self._try_get(state, pending.target_request_id)
                    return bool(detail and self._matches_update_invariants(detail, old_detail, pending.args))
                if exc.code != "VERSION_CONFLICT":
                    raise
                detail = self._try_get(state, pending.target_request_id)
                if (
                    detail and detail.get("version") == expected + 1
                    and self._matches_update_invariants(detail, old_detail, pending.args)
                ):
                    return True
                raise
            except ProcurementTransportError:
                detail = self._try_get(state, pending.target_request_id)
                if detail and self._matches_update_invariants(detail, old_detail, pending.args):
                    return True
                return False
        return False

    def _try_get(self, state: SessionState, request_id: str) -> dict[str, Any] | None:
        try:
            return self._read_request_runtime(state, request_id)
        except ProcurementApiError:
            return None
        except ProcurementTransportError:
            return None

    @staticmethod
    def _matches_create(detail: dict[str, Any], args: dict[str, Any]) -> bool:
        supplier = detail.get("supplier") or {}
        return (
            detail.get("applicant") == "U01"
            and detail.get("item") == args.get("item")
            and detail.get("quantity") == args.get("quantity")
            and detail.get("unit_price") == args.get("unit_price")
            and detail.get("purpose") == args.get("purpose")
            and supplier.get("name") == args.get("supplier_name")
        )

    @staticmethod
    def _matches_changes(detail: dict[str, Any], changes: dict[str, Any]) -> bool:
        for name, expected in changes.items():
            actual = (detail.get("supplier") or {}).get("name") if name == "supplier_name" else detail.get(name)
            if str(actual) != str(expected):
                return False
        return True

    @staticmethod
    def _same_business_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
        fields = ("item", "quantity", "unit_price", "purpose", "version", "state")
        return all(left.get(name) == right.get(name) for name in fields) and (
            (left.get("supplier") or {}).get("name") == (right.get("supplier") or {}).get("name")
        )

    @classmethod
    def _matches_update_invariants(
        cls, final: dict[str, Any], old: dict[str, Any], changes: dict[str, Any]
    ) -> bool:
        # 对齐 Store.update_request：只改指定字段、版本+1、重算金额、清空审批与凭证。
        fields = {name: changes.get(name, (old.get("supplier") or {}).get("name")
                                  if name == "supplier_name" else old.get(name))
                  for name in cls.CHANGE_FIELDS}
        expected_state = "DRAFT" if old.get("state") in {
            "DRAFT", "NEED_INFO", "BLOCKED_VENDOR", "BLOCKED_BUDGET", "PENDING_VENDOR_REVIEW"
        } else "REAPPROVAL_REQUIRED"
        approval = final.get("approval") or {}
        try:
            amount = Decimal(str(final.get("unit_price"))) * int(final.get("quantity"))
            amount_matches = Decimal(str(final.get("amount"))) == amount
        except (InvalidOperation, ValueError, TypeError):
            amount_matches = False
        return (
            cls._matches_changes(final, fields)
            and final.get("request_id") == old.get("request_id")
            and final.get("version") == old.get("version", 0) + 1
            and final.get("state") == expected_state and amount_matches
            and all(final.get(name) == old.get(name) for name in ("applicant", "department", "budget_id", "currency"))
            and approval.get("required_approver") == ("M02" if amount > 10000 else "M01")
            and approval.get("approved_by") is None and approval.get("approved_version") is None
            and approval.get("approval_valid") is False and approval.get("evidence_verified") is False
            and not final.get("po")
        )

    def _unknown_result(
        self, state: SessionState, request_id: str, reason: str, *, created: bool = False
    ) -> dict[str, Any]:
        state.uncertain_request_id = request_id
        state.target_request_id = request_id
        state.known_requests.pop(request_id, None)
        state.last_read_version = None
        self._record(state, "WRITE_RESULT_UNCERTAIN", request_id=request_id, outcome_status=reason)
        return self._response(
            state, "STOP",
            ("采购申请已经创建。" if created else "")
            + f"申请 {request_id} 的执行结果未知，无法核验最终业务状态；请先查询真实状态，不要直接重复写入。",
            reason,
            result={"request_id": request_id},
            outcome_status="UNKNOWN",
        )

    @staticmethod
    def _status_explanation(detail: dict[str, Any]) -> str:
        state = detail.get("state")
        if state == "BLOCKED_BUDGET":
            return (
                "申请已创建，但因预算不足未正常提交审批；显示的审批去向只是按金额计算的所需级别，"
                "当前尚未进入审批队列。可修改数量或单价后重新提交，或由授权人员处理预算。"
            )
        if state == "BLOCKED_VENDOR":
            return "申请因供应商黑名单或不可用状态被阻断，未进入采购审批。"
        if state == "PENDING_VENDOR_REVIEW":
            return (
                "申请因新供应商准入审核未完成而暂停；需要授权管理员处理供应商准入待办，"
                "准入通过不等于采购审批通过。"
            )
        if state == "NEED_INFO":
            return "申请信息不完整，尚未提交审批；请补全缺失字段后重新确认提交。"
        if state in {"SUBMITTED", "PENDING_HIGH_APPROVAL"}:
            return "申请已提交，正在等待对应人工审批人处理；Agent 不执行审批或创建 PO。"
        return ""

    @staticmethod
    def _finish_task(state: SessionState, *, clear_target: bool = False) -> None:
        """任务结束后清除可写字段，保留历史消息和已核验申请供连续会话查询。"""
        state.active_task_type = None
        state.slots = {}
        state.missing_slots = []
        state.task_messages = []
        state.clarification = None
        state.supplier_matches = {}
        state.supplier_match_runs = {}
        if clear_target:
            state.target_request_id = None

    @staticmethod
    def _result_view(detail: dict[str, Any]) -> dict[str, Any]:
        return {
            "result_type": "request",
            "request_id": detail.get("request_id"),
            "state": detail.get("state"),
            "version": detail.get("version"),
            "amount": detail.get("amount"),
            "supplier": detail.get("supplier"),
            "approval": detail.get("approval"),
            "po": ({key: value for key, value in detail["po"].items() if key != "idempotency_key"}
                   if isinstance(detail.get("po"), dict) else None),
        }

    def _save_clarification(self, state: SessionState, message: str, reason: str) -> None:
        """各 ASK 分支共享同一份有界澄清描述；业务槽位和暂停快照仍由原机制管理。"""
        latest_user = next((m["content"] for m in reversed(state.conversation_tail) if m["role"] == "user"), "")
        pending = dict(state.clarification or {})
        pending.update({
            "question": message[:1500], "reason": reason,
            "intent": state.active_task_type or pending.get("intent"),
            "target_object": pending.get("target_object", "current_task" if state.resume_prompt_pending else "unknown"),
            "request_id": state.target_request_id,
            "known_slots": dict(state.slots) if state.active_task_type else dict(pending.get("known_slots") or {}),
            "missing_slots": list(state.missing_slots),
            "topic": pending.get("topic") or latest_user[:1000],
        })
        if (not state.active_task_type and not state.suspended_task
                and pending.get("intent") in {None, "ambiguous"}):
            # 意图未定时模型可能只提问而省略字段；严格三字段原文可保存为候选，不能授予写权限。
            parts = [part.strip() for part in re.split(r"[，,]", latest_user)]
            quantity = re.fullmatch(r"([1-9]\d*)\s*(?:[\u4e00-\u9fff]{1,2})?", parts[1]) if len(parts) == 3 else None
            if quantity and parts[0] and parts[2]:
                known = pending["known_slots"]
                for key, value in {"item": parts[0], "quantity": int(quantity.group(1)), "purpose": parts[2]}.items():
                    known.setdefault(key, value)
                pending["intent"] = "ambiguous"
        if reason == "INTERRUPTED_TASK_WAITING_RESUME" and state.suspended_task:
            snapshot = state.suspended_task
            pending.update({"intent": snapshot.task_type, "target_object": "current_task",
                            "request_id": snapshot.target_request_id, "known_slots": dict(snapshot.slots),
                            "missing_slots": list(snapshot.missing_slots),
                            "topic": next((m["content"][:1000] for m in snapshot.task_messages if m["role"] == "user"), "")})
        state.clarification = pending
        if reason != "INTERRUPTED_TASK_WAITING_RESUME":
            self._remember_task_message(state, "user", latest_user)
            self._remember_task_message(state, "assistant", message)
        self._record(state, "CLARIFICATION_SAVED", clarification=pending)

    def _response(
        self,
        state: SessionState,
        route_action: str,
        message: str,
        reason: str,
        *,
        confirmation: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        outcome_status: str | None = None,
        finalize: bool = True,
    ) -> dict[str, Any]:
        self._append_message(state, "assistant", message)
        if route_action == "ASK":
            self._save_clarification(state, message, reason)
        elif route_action in {"STOP", "CONFIRM"}:
            state.clarification = None
        if result is not None:
            # 页面刷新后可按原类型恢复结果卡；这里只保存已净化的公开响应，不保存工具原始载荷。
            state.conversation_tail[-1]["result"] = json.loads(
                json.dumps(result, ensure_ascii=False)
            )
        if route_action != "ASK" and state.active_task_type in self.WRITE_TASKS:
            self._remember_task_message(state, "assistant", message)
        response = {
            "message": message,
            "route_action": route_action,
            "route_reason_code": reason,
            "trace_id": state.current_run_id,
            "confirmation": confirmation,
            "result": result,
            "outcome_status": outcome_status or ("SUCCESS" if reason in {"READ_VERIFIED", "WRITE_VERIFIED"} else None),
        }
        if finalize:
            self._route(state, route_action, reason, outcome_status=response["outcome_status"])
            self._record(
                state, "FINAL_RESPONSE", final_response=message,
                outcome_status=response["outcome_status"], request_id=state.target_request_id,
                version=(result or {}).get("version"),
            )
        else:
            # 中断查询的内容稍后与恢复询问合并，不能提前写成 run 的最终事件。
            self._record(
                state, "INTERMEDIATE_RESULT_PREPARED",
                route_action=route_action, route_reason_code=reason,
                outcome_status=response["outcome_status"],
            )
        return response
