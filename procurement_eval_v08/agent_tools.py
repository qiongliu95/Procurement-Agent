"""受控采购 API 适配器与 LLM 只读工具定义。"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any


READ_TOOL_NAMES = frozenset({
    "match_supplier", "list_requests", "get_request", "get_request_po", "get_request_budget",
    "search_purchase_history", "get_procurement_rules", "search_products", "search_knowledge",
})


READ_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "查询已审校放行的采购中文知识库，用于公司采购制度、操作指南和通用采购专业知识。"
                "该工具不查询商品价格、申请状态、供应商当前状态、审批状态或历史成交价；这些事实必须使用对应业务 API 工具。"
                "工具只读，咨询不能触发采购申请写入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户当前的采购知识问题。"},
                    "top_k": {
                        "type": "integer", "minimum": 1, "maximum": 8,
                        "description": "向量候选数；核心制度咨询另补必要的已审校原文节，最终最多8个片段。",
                    },
                    "use_previous_query": {
                        "type": "boolean",
                        "description": "连续追问同一知识主题，或回答 pending_clarification 中知识咨询的条件补问时为 true；Runtime 会补入原主题及已确认的条件，不必先有检索结果。",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "查询模拟器中的可信商品目录，可按编码、名称、分类、品牌型号或规格关键词搜索。"
                "采购价及supplier由采购负责人维护；purchase_price为采购价，reference_price是兼容字段，不代表另一个价格，缺失不能创建。"
                "该工具只读。新建时Runtime自动读取唯一目录记录，模型不必重复调用或要求员工填单价供应商。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "当前用户或有效任务中的商品关键词；省略时仅可使用当前已确认商品，或用户明确要求全部目录。"},
                    "status": {"type": "string", "enum": ["active", "inactive", "all"]},
                    "use_previous_query": {
                        "type": "boolean",
                        "description": "仅连续追问同一商品目录结果时为 true，不能用于猜测新商品。",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "match_supplier",
            "description": "按用户明确给出的供应商名称查询可信主数据。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_requests",
            "description": (
                "列出当前可信用户 U01 有权查看的采购申请及进度；全部申请查询可直接使用此列表，"
                "也可用于单笔查询或修改任务中解析不明确的已有对象。"
                "不要用它替代新建申请，也不要因为候选不唯一而改变已经明确的 query/update 意图。"
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_request",
            "description": "读取一个已由用户或可信上下文确定的采购申请详情。",
            "parameters": {
                "type": "object",
                "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_request_po",
            "description": "查询一个已绑定采购申请的真实 PO 状态。",
            "parameters": {
                "type": "object",
                "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_request_budget",
            "description": "只读查询当前用户有权访问的申请所关联部门预算，解释预算不足、余额和缺口。只传申请编号；不得指定任意预算、调用管理员接口或让用户提供预算上限替代部门余额。每次追问重新读取，缺口按queried_at时点计算，不是提交时缺口。完成后提出query + request + query_topic=budget。",
            "parameters": {
                "type": "object", "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_purchase_history",
            "description": (
                "仅在用户咨询历史采购或价格参考时，查询当前用户有权查看的已完成采购历史与历史成交单价。"
                "结果来自真实采购申请和 PO，不代表实时市场价。"
                "未限定物品时允许省略 item 或传空字符串，按当前身份权限读取最近最多20条已创建PO记录，不是全部历史。"
                "不得为明确的新建申请补齐或推断单价。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "可选的物品或型号关键词。"},
                    "use_previous_query": {
                        "type": "boolean",
                        "description": "仅连续追问同一历史成交结果时为 true；Runtime 只复用已验证的查询条件。",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_procurement_rules",
            "description": (
                "仅在用户咨询制度、审批规则或业务可行性时，查询本地版本化、带来源的已确认采购业务规则。"
                "不得用它替代明确的创建、修改或查询任务。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "可选的制度主题或关键词。"}
                },
                "additionalProperties": False,
            },
        },
    },
]


HISTORY_FIELDS = {
    "item_spec": "采购物品", "quantity": "采购数量", "unit_price": "历史成交单价",
    "amount": "成交金额", "currency": "币种", "purchase_date": "成交日期",
    "supplier_name": "供应商", "request_id": "采购申请编号", "po_id": "PO 编号",
}


TASK_PROPOSAL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "propose_task_state",
        "description": (
            "基于用户目标、会话上下文和工具结果提出下一步业务任务状态。"
            "该工具无副作用，不能确认或执行写操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["create", "query", "update", "submit", "consult", "ambiguous", "unsupported"],
                    "description": (
                        "用户当前业务目标：明确要求发起申请=create；明确要求改变已有申请=update；"
                        "明确查询采购申请当前状态或能否修改=query；历史成交、商品目录、计划、价格、"
                        "制度了解及对这些只读结果的连续追问=consult；"
                        "只有操作目标本身不明确=ambiguous；取消、审批、创建PO等=unsupported。"
                        "仅描述字段填错等问题但未要求修改时，不应判为update。"
                        "缺字段或对象不唯一不改变已经明确的 create/query/update 意图。"
                    ),
                },
                "request_id": {"type": "string"},
                "query_scope": {
                    "type": "string", "enum": ["single", "collection"],
                    "description": "仅申请 query 使用：查询全部申请及进度用 collection，无需指定唯一编号；单笔查询用 single。修改、提交仍须唯一申请，不支持集合写入。",
                },
                "query_topic": {
                    "type": "string", "enum": ["status", "budget"],
                    "description": "申请单笔查询的回答目标：状态为status；询问部门预算不足原因、余额或缺口为budget，必须使用get_request_budget读取实时值，不用固定状态摘要代答。",
                },
                "item": {"type": "string"},
                "quantity": {"type": "integer"},
                "unit_price": {"type": "string", "description": "仅保留已有申请修改的兼容字段；新建忽略模型填价，由Runtime读取目录有效采购价。预算不能推算成单价。"},
                "supplier_name": {"type": "string", "description":"新建不由员工填写，Runtime读取目录指定供应商。"},
                "purpose": {"type": "string"},
                "relative_quantity_delta": {"type": "integer"},
                "dialogue_relation": {
                    "type": "string",
                    "enum": [
                        "new_task", "follow_up", "supplement", "correction", "confirm",
                        "cancel", "switch_task", "pause", "resume", "unclear"
                    ],
                    "description": (
                        "当前话语与既有对话的关系；与业务 intent 分开判断。对已有只读结果询问其字段"
                        "属于 follow_up，不因提到申请编号字段就变成新的申请查询。"
                        "回答 pending_clarification 是 supplement；同类业务明确另起一笔也必须是 new_task/switch_task，不能沿用旧字段。"
                        "放弃当前会话的未完成临时任务用 cancel，和取消已落库申请不同。"
                    ),
                },
                "target_object": {
                    "type": "string",
                    "enum": ["request", "purchase_history", "product", "knowledge", "current_task", "unknown"],
                    "description": (
                        "当前话语要引用的对象类型，不同类型候选不得混用。连续追问沿用对应 trusted_read_contexts"
                        " 的对象类型；只有明确查询申请当前状态时才选 request。"
                    ),
                },
                "reference_index": {
                    "type": "integer", "minimum": 1, "maximum": 8,
                    "description": "用户明确说第几个时，给出在对应可信结果中的一基序号；不明确则省略。",
                },
                "history_fields": {
                    "type": "array", "uniqueItems": True,
                    "items": {"type": "string", "enum": list(HISTORY_FIELDS)},
                    "description": (
                        "历史成交的回答范围：根据当前问题语义选择要回答的字段，可选多个；"
                        "对已有结果的字段追问必须填写，只选当前询问的字段，不携带上轮范围。"
                        "首次整体查询历史成交价列表或要求完整记录时省略或填空数组。"
                        "事实由 Runtime 从可信记录提取，不在提案中编写答案；"
                        "回答范围与证据来源分开：Runtime 会在结构化结果和 Trace 保留来源关联，"
                        "不要为了标注来源把用户未询问的申请编号、PO 或其他字段加入此列表。"
                        "已存在所需可信证据时直接提出 follow_up，无需重复检索。"
                    ),
                },
                "question": {
                    "type": "string",
                    "description": "确实需要澄清时，给用户的最少必要问题。",
                },
            },
            "required": ["intent", "dialogue_relation", "target_object"],
            "additionalProperties": False,
        },
    },
}


# 旧的 JSON 规则查询保留给既有测试和兼容路径；真实模型统一使用 RAG 知识工具。
MODEL_TOOL_SCHEMAS = [
    *[
        schema for schema in READ_TOOL_SCHEMAS
        if schema["function"]["name"] != "get_procurement_rules"
    ],
    TASK_PROPOSAL_SCHEMA,
]


@dataclass(frozen=True)
class ApiCallResult:
    data: dict[str, Any]
    status: int
    tool_call_id: str
    latency_ms: int


class ProcurementApiError(Exception):
    def __init__(self, status: int, code: str, data: dict[str, Any] | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.data = data or {"error_code": code}


class ProcurementTransportError(Exception):
    """调用结果可能未知；写调用者必须先核验服务端状态。"""

    def __init__(self, message: str, *, uncertain: bool):
        super().__init__(message)
        self.uncertain = uncertain


class ProcurementApiClient:
    def __init__(self, base_url: str, token: str, timeout_seconds: float = 10):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("INVALID_PROCUREMENT_API_BASE_URL")
        self.base_url = base_url.rstrip("/")
        self.__token = token
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def new_tool_call_id() -> str:
        return "call-" + uuid.uuid4().hex

    def _request(
        self,
        method: str,
        path: str,
        *,
        run_id: str,
        tool_call_id: str,
        data: dict[str, Any] | None = None,
        uncertain_on_failure: bool = False,
    ) -> ApiCallResult:
        if not path.startswith("/") or ".." in path:
            raise ValueError("INVALID_API_PATH")
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=body,
            headers={
                "Authorization": "Bearer " + self.__token,
                "Content-Type": "application/json",
                "X-Run-ID": run_id,
                "X-Tool-Call-ID": tool_call_id,
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ProcurementTransportError(
                        "INVALID_JSON_RESPONSE", uncertain=uncertain_on_failure
                    ) from exc
                if not isinstance(parsed, dict):
                    raise ProcurementTransportError(
                        "NON_OBJECT_RESPONSE", uncertain=uncertain_on_failure
                    )
                return ApiCallResult(
                    parsed,
                    response.status,
                    tool_call_id,
                    int((time.monotonic() - started) * 1000),
                )
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {"error_code": "HTTP_ERROR"}
            code = payload.get("error_code", "HTTP_ERROR") if isinstance(payload, dict) else "HTTP_ERROR"
            raise ProcurementApiError(exc.code, str(code), payload if isinstance(payload, dict) else None) from exc
        except ProcurementTransportError:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, RemoteDisconnected, OSError) as exc:
            raise ProcurementTransportError(type(exc).__name__, uncertain=uncertain_on_failure) from exc

    def match_supplier(self, name: str, *, run_id: str, tool_call_id: str) -> ApiCallResult:
        query = urllib.parse.urlencode({"name": name})
        return self._request("GET", "/suppliers/match?" + query, run_id=run_id, tool_call_id=tool_call_id)

    def list_requests(self, *, run_id: str, tool_call_id: str) -> ApiCallResult:
        return self._request("GET", "/requests", run_id=run_id, tool_call_id=tool_call_id)

    def get_request(self, request_id: str, *, run_id: str, tool_call_id: str) -> ApiCallResult:
        rid = urllib.parse.quote(request_id, safe="")
        return self._request("GET", f"/requests/{rid}", run_id=run_id, tool_call_id=tool_call_id)

    def get_request_po(self, request_id: str, *, run_id: str, tool_call_id: str) -> ApiCallResult:
        rid = urllib.parse.quote(request_id, safe="")
        return self._request("GET", f"/requests/{rid}/po", run_id=run_id, tool_call_id=tool_call_id)

    def get_request_budget(self, request_id: str, *, run_id: str, tool_call_id: str) -> ApiCallResult:
        rid = urllib.parse.quote(request_id, safe="")
        return self._request("GET", f"/requests/{rid}/budget", run_id=run_id, tool_call_id=tool_call_id)

    def search_purchase_history(
        self, item: str = "", *, run_id: str, tool_call_id: str
    ) -> ApiCallResult:
        query = urllib.parse.urlencode({"item": item}) if item else ""
        path = "/history/prices" + (("?" + query) if query else "")
        return self._request("GET", path, run_id=run_id, tool_call_id=tool_call_id)

    def get_procurement_rules(
        self, topic: str = "", *, run_id: str, tool_call_id: str
    ) -> ApiCallResult:
        started = time.monotonic()
        path = Path(__file__).with_name("procurement_rules_v1.json")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcurementTransportError("RULE_SOURCE_UNAVAILABLE", uncertain=False) from exc
        rules = document.get("rules", [])
        query = str(topic or "").strip()
        if query:
            matched = [
                rule for rule in rules
                if query.casefold() in (str(rule.get("title", "")) + str(rule.get("content", ""))).casefold()
                or any(str(keyword).casefold() in query.casefold() for keyword in rule.get("keywords", []))
            ]
            if matched:
                rules = matched
        data = {
            "query": query or None,
            "document_id": document.get("document_id"),
            "version": document.get("version"),
            "source_name": document.get("source_name"),
            "rules": [
                {key: rule.get(key) for key in ("rule_id", "title", "content")}
                for rule in rules
            ],
        }
        return ApiCallResult(data, 200, tool_call_id, int((time.monotonic() - started) * 1000))

    def search_products(
        self, query: str = "", status: str = "active", *, run_id: str, tool_call_id: str
    ) -> ApiCallResult:
        params={"status":status}
        if query:params["query"]=query
        return self._request(
            "GET", "/products?"+urllib.parse.urlencode(params),
            run_id=run_id,tool_call_id=tool_call_id,
        )

    def create_request(self, payload: dict[str, Any], *, run_id: str, tool_call_id: str) -> ApiCallResult:
        return self._request(
            "POST", "/requests", run_id=run_id, tool_call_id=tool_call_id,
            data=payload, uncertain_on_failure=True,
        )

    def submit_request(
        self, request_id: str, expected_version: int, *, run_id: str, tool_call_id: str
    ) -> ApiCallResult:
        rid = urllib.parse.quote(request_id, safe="")
        return self._request(
            "POST", f"/requests/{rid}/submit", run_id=run_id, tool_call_id=tool_call_id,
            data={"expected_version": expected_version}, uncertain_on_failure=True,
        )

    def update_request(
        self,
        request_id: str,
        fields: dict[str, Any],
        expected_version: int,
        *,
        run_id: str,
        tool_call_id: str,
    ) -> ApiCallResult:
        rid = urllib.parse.quote(request_id, safe="")
        payload = {**fields, "expected_version": expected_version}
        return self._request(
            "PATCH", f"/requests/{rid}/update", run_id=run_id, tool_call_id=tool_call_id,
            data=payload, uncertain_on_failure=True,
        )


def model_safe_result(tool_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """只把模型完成当前任务所需的业务字段放回上下文。"""
    if tool_name == "match_supplier":
        return {
            "query": data.get("query"),
            "matches": [
                {"name": item.get("name"), "status": item.get("status")}
                for item in data.get("matches", [])
            ],
        }
    if tool_name == "get_request_po":
        return {key: value for key, value in data.items() if key != "idempotency_key"}
    if tool_name == "get_request_budget":
        return {key: data.get(key) for key in (
            "request_id", "department", "budget_id", "available_budget", "request_amount", "shortfall",
            "currency", "queried_at", "request_state", "request_version")}
    if tool_name == "search_purchase_history":
        return {
            "query": data.get("query"),
            "price_basis": data.get("price_basis"),
            "records": data.get("records", []),
        }
    if tool_name == "get_procurement_rules":
        return {
            key: data.get(key)
            for key in ("query", "document_id", "version", "source_name", "rules")
        }
    if tool_name == "search_knowledge":
        return {
            "query": data.get("query"),
            "model_id": data.get("model_id"),
            "index_fingerprint": data.get("index_fingerprint"),
            "evidence_sufficient": data.get("evidence_sufficient", False),
            "timings_ms": data.get("timings_ms", {}),
            "hits": [
                {key: item.get(key) for key in (
                    "chunk_id", "doc_id", "title", "heading", "class",
                    "source", "source_url", "text", "score",
                )}
                for item in data.get("hits", [])
            ],
        }
    if tool_name == "search_products":
        return {
            "query":data.get("query"),"status":data.get("status"),"total":data.get("total",0),
            "products":[
                {key:item.get(key) for key in (
                    "code","name","category","brand_model","specification","unit",
                    "reference_price","purchase_price","version","currency","status")}
                | {"supplier": {k:item["supplier"].get(k) for k in ("name","status")} if item.get("supplier") else None}
                for item in data.get("products",[])
            ],
        }

    def clean_request(item: dict[str, Any]) -> dict[str, Any]:
        supplier = item.get("supplier")
        clean = {
            key: value for key, value in item.items()
            if key not in {"budget_id", "supplier"}
        }
        clean["supplier"] = (
            {"name": supplier.get("name"), "status": supplier.get("status")}
            if isinstance(supplier, dict) else None
        )
        if isinstance(clean.get("po"), dict):
            clean["po"] = {
                key: value for key, value in clean["po"].items()
                if key != "idempotency_key"
            }
        return clean

    if tool_name == "list_requests":
        return {"requests": [clean_request(item) for item in data.get("requests", [])]}
    if tool_name == "get_request":
        return clean_request(data)
    return data
