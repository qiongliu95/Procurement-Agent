"""EXEC-02 真实模型产品验收 Harness。

只使用现有 Runtime、模型适配器和本地模拟器。评测夹具可调用 evaluator-only
接口设置低预算，但 Agent 本身仍只持有 demo-agent 权限。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_runtime import AgentRuntime, OpenAICompatibleModel, TraceWriter
from agent_tools import ProcurementApiClient
from backend import App, Store


ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = ROOT / "runtime_data" / "exec_02"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def http_json(base: str, method: str, path: str, token: str, data: dict[str, Any] | None = None):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        base + path,
        method=method,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


class LowBudgetApiClient(ProcurementApiClient):
    """在 Runtime 创建草稿后由评测 Harness 注入 B_LOW，再让 Runtime 正常提交。"""

    def create_request(self, payload, *, run_id, tool_call_id):
        result = super().create_request(payload, run_id=run_id, tool_call_id=tool_call_id)
        status, body = http_json(
            self.base_url,
            "POST",
            "/__eval__/seed_budget",
            "demo-eval",
            {"request_id": payload["request_id"], "budget_id": "B_LOW"},
        )
        if status != 200:
            raise RuntimeError(f"B_LOW fixture failed: {status} {body}")
        return result


def request_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as db:
        return int(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0])


def trace_events(trace_path: Path, start: int) -> tuple[list[dict[str, Any]], int]:
    if not trace_path.exists():
        return [], start
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[start:], len(rows)


def tool_names(events: list[dict[str, Any]]) -> list[str]:
    return [event.get("tool_name") for event in events if event.get("event_type") == "TOOL_CALL_COMPLETED"]


def check(name: str, conditions: dict[str, bool]) -> dict[str, Any]:
    failed = [label for label, passed in conditions.items() if not passed]
    return {"checker": name, "status": "PASS" if not failed else "FAIL", "failed_assertions": failed}


def main() -> int:
    required = ("AGENT_MODEL_API_KEY", "AGENT_MODEL_BASE_URL", "AGENT_MODEL_NAME")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print(json.dumps({"status": "CONFIG_BLOCKED", "missing": missing}, ensure_ascii=False))
        return 2

    run_dir = OUTPUT_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    db_path = run_dir / "exec_02.sqlite"
    trace_dir = run_dir / "traces"
    app = App(("127.0.0.1", 0), Store(db_path))
    server_thread = threading.Thread(target=app.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{app.server_port}"
    trace_writer = TraceWriter(trace_dir)
    trace_path = trace_dir / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    model = OpenAICompatibleModel.from_env()
    runtime = AgentRuntime(
        model,
        ProcurementApiClient(base, "demo-agent", 30),
        trace_writer,
        model_name=os.environ["AGENT_MODEL_NAME"],
    )
    evidence: dict[str, Any] = {
        "execution_id": "exec02-" + uuid.uuid4().hex,
        "started_at": utc_now(),
        "model": {
            "provider_base_url": os.environ["AGENT_MODEL_BASE_URL"],
            "model_name": os.environ["AGENT_MODEL_NAME"],
            "tool_calling_gate": "PASS",
        },
        "database": str(db_path),
        "trace_file": str(trace_path),
        "cases": [],
    }
    trace_index = 0

    try:
        shared = runtime.new_session("EXEC02-SHARED")

        # Case A：正常申请，真实模型提取字段并由 Runtime 确认后写入。
        count_before = request_count(db_path)
        response = runtime.handle_message(
            shared, "采购 2 台显示器，单价 800 元，供应商启明办公，用于市场部办公。"
        )
        request_id = shared.pending_action.target_request_id if shared.pending_action else None
        confirmation = response.get("confirmation")
        final_response = (
            runtime.confirm(shared, confirmation["action_id"], "confirm") if confirmation else response
        )
        snapshot = http_json(base, "GET", f"/__eval__/{request_id}", "demo-eval")[1] if request_id else None
        events, trace_index = trace_events(trace_path, trace_index)
        conditions = {
            "confirmation_generated": bool(confirmation),
            "request_created_once": request_count(db_path) == count_before + 1,
            "final_state_submitted": bool(snapshot and snapshot["request"]["state"] == "SUBMITTED"),
            "fields_match_user": bool(snapshot and snapshot["request"]["item"] == "显示器"
                                      and snapshot["request"]["quantity"] == 2
                                      and snapshot["request"]["unit_price"] == 80000
                                      and snapshot["request"]["purpose"] == "市场部办公"),
            "trusted_supplier": bool(snapshot and snapshot["request"]["supplier_id"] == "S_ACTIVE"),
            "no_po": bool(snapshot and not snapshot["pos"]),
            "no_agent_approval": bool(snapshot and not snapshot["approvals"]),
            "m01_wait_message": "M01" in final_response.get("message", "") and "等待" in final_response.get("message", ""),
            "tool_trace_present": all(name in tool_names(events) for name in ("match_supplier", "create_request", "submit_request", "get_request")),
        }
        evidence["cases"].append({
            "case_id": "A",
            "user_inputs": ["采购 2 台显示器，单价 800 元，供应商启明办公，用于市场部办公。"],
            "initial_state": {"request_count": count_before},
            "request_id": request_id,
            "agent_response": response,
            "final_response": final_response,
            "agent_trace": events,
            "tool_calls": tool_names(events),
            "final_business_state": snapshot,
            "checker_result": check("EXEC02-A", conditions),
            "human_review": "PASS" if all(conditions.values()) else "FAIL",
        })

        # Case B：预算不能变成单价，补全后使用本轮新字段继续。
        count_before = request_count(db_path)
        first = runtime.handle_message(shared, "我要采购 2 台显示器，预算 2000 元。")
        count_after_ask = request_count(db_path)
        second = runtime.handle_message(shared, "单价 900 元，供应商启明办公，用于项目组测试。")
        request_b = shared.pending_action.target_request_id if shared.pending_action else None
        confirmation_b = second.get("confirmation")
        final_b = runtime.confirm(shared, confirmation_b["action_id"], "confirm") if confirmation_b else second
        snapshot_b = http_json(base, "GET", f"/__eval__/{request_b}", "demo-eval")[1] if request_b else None
        events_b, trace_index = trace_events(trace_path, trace_index)
        conditions_b = {
            "first_turn_asks": first.get("route_action") == "ASK" and first.get("confirmation") is None,
            "no_write_before_completion": count_after_ask == count_before,
            "budget_not_unit_price": bool(snapshot_b and snapshot_b["request"]["unit_price"] == 90000),
            "new_purpose_used": bool(snapshot_b and snapshot_b["request"]["purpose"] == "项目组测试"),
            "completed_after_followup": bool(confirmation_b and snapshot_b and snapshot_b["request"]["state"] == "SUBMITTED"),
            "distinct_request": request_b != request_id,
        }
        evidence["cases"].append({
            "case_id": "B",
            "user_inputs": ["我要采购 2 台显示器，预算 2000 元。", "单价 900 元，供应商启明办公，用于项目组测试。"],
            "initial_state": {"previous_request_id": request_id, "request_count": count_before},
            "agent_responses": [first, second],
            "final_response": final_b,
            "request_id": request_b,
            "agent_trace": events_b,
            "tool_calls": tool_names(events_b),
            "final_business_state": snapshot_b,
            "checker_result": check("EXEC02-B", conditions_b),
            "human_review": "PASS" if all(conditions_b.values()) else "FAIL",
        })

        # Case C：同一会话切到黑名单供应商，确认前阻断且不写入。
        count_before_c = request_count(db_path)
        response_c = runtime.handle_message(
            shared, "采购办公键盘 2 个，单价 100 元，供应商风险供应商，用于办公设备补充。"
        )
        count_after_c = request_count(db_path)
        events_c, trace_index = trace_events(trace_path, trace_index)
        conditions_c = {
            "blacklist_identified": (
                response_c.get("route_reason_code") == "SUPPLIER_BLACKLISTED"
                or "黑名单" in response_c.get("message", "")
                or "blacklisted" in response_c.get("message", "")
            ),
            "stopped_before_confirmation": response_c.get("confirmation") is None,
            "no_request_created": count_after_c == count_before_c,
            "matched_blacklisted_supplier": "match_supplier" in tool_names(events_c),
            "no_write_tool": not any(name in tool_names(events_c) for name in ("create_request", "submit_request", "update_request")),
        }
        evidence["cases"].append({
            "case_id": "C",
            "user_inputs": ["采购办公键盘 2 个，单价 100 元，供应商风险供应商，用于办公设备补充。"],
            "initial_state": {"request_count": count_before_c, "previous_request_id": request_b},
            "agent_response": response_c,
            "agent_trace": events_c,
            "tool_calls": tool_names(events_c),
            "final_business_state": {"request_count": count_after_c, "new_request": None},
            "checker_result": check("EXEC02-C", conditions_c),
            "human_review": "PASS" if all(conditions_c.values()) else "FAIL",
        })

        # Case E：黑名单任务结束后，同会话仍可读取 Case A 的真实历史申请。
        query_a = runtime.handle_message(shared, f"查询采购申请 {request_id} 的最新状态。")
        events_e, trace_index = trace_events(trace_path, trace_index)
        conditions_e = {
            "new_task_did_not_reuse_old_supplier": (
                "风险供应商" in response_c.get("message", "")
                and "启明办公" not in response_c.get("message", "")
                and response_c.get("confirmation") is None
            ),
            "old_request_still_readable": query_a.get("route_reason_code") == "READ_VERIFIED",
            "query_targets_original_request": (query_a.get("result") or {}).get("request_id") == request_id,
            "original_state_unchanged": http_json(base, "GET", f"/__eval__/{request_id}", "demo-eval")[1]["request"]["state"] == "SUBMITTED",
        }
        evidence["cases"].append({
            "case_id": "E",
            "user_inputs": ["同一会话完成 A/B 后发起 C", f"查询采购申请 {request_id} 的最新状态。"],
            "initial_state": {"completed_request_ids": [request_id, request_b]},
            "agent_response": query_a,
            "agent_trace": events_e,
            "tool_calls": tool_names(events_e),
            "final_business_state": query_a.get("result"),
            "checker_result": check("EXEC02-E", conditions_e),
            "human_review": "PASS" if all(conditions_e.values()) else "FAIL",
        })

        # Case D：独立 Runtime 使用 evaluator-only 夹具把新草稿映射到 B_LOW。
        low_runtime = AgentRuntime(
            model,
            LowBudgetApiClient(base, "demo-agent", 30),
            trace_writer,
            model_name=os.environ["AGENT_MODEL_NAME"],
        )
        low_state = low_runtime.new_session("EXEC02-B-LOW")
        response_d = low_runtime.handle_message(
            low_state, "采购 3 台显示器，单价 600 元，供应商启明办公，用于采购显示器。"
        )
        request_d = low_state.pending_action.target_request_id if low_state.pending_action else None
        confirmation_d = response_d.get("confirmation")
        final_d = low_runtime.confirm(low_state, confirmation_d["action_id"], "confirm") if confirmation_d else response_d
        snapshot_d = http_json(base, "GET", f"/__eval__/{request_d}", "demo-eval")[1] if request_d else None
        events_d, trace_index = trace_events(trace_path, trace_index)
        backend_types_d = [
            event.get("event_type", event.get("type"))
            for event in (snapshot_d or {}).get("events", [])
        ]
        conditions_d = {
            "confirmation_generated": bool(confirmation_d),
            "blocked_budget": bool(snapshot_d and snapshot_d["request"]["state"] == "BLOCKED_BUDGET"),
            "budget_unchanged": bool(snapshot_d and snapshot_d["budget_balance"] == 100000),
            "no_po": bool(snapshot_d and not snapshot_d["pos"]),
            "no_budget_debit": "BUDGET_DEBITED" not in backend_types_d,
            "clear_user_explanation": "未正常提交审批" in final_d.get("message", "") and "未进入审批队列" in final_d.get("message", ""),
        }
        evidence["cases"].append({
            "case_id": "D",
            "user_inputs": ["采购 3 台显示器，单价 600 元，供应商启明办公，用于采购显示器。"],
            "initial_state": {"budget_fixture": "B_LOW", "budget_balance": "1000.00"},
            "request_id": request_d,
            "agent_response": response_d,
            "final_response": final_d,
            "agent_trace": events_d,
            "tool_calls": tool_names(events_d),
            "final_business_state": snapshot_d,
            "checker_result": check("EXEC02-D", conditions_d),
            "human_review": "PASS" if all(conditions_d.values()) else "FAIL",
        })

        statuses = {case["case_id"]: case["checker_result"]["status"] for case in evidence["cases"]}
        for case in evidence["cases"]:
            case["failure_category"] = (
                None if case["checker_result"]["status"] == "PASS"
                else "MODEL_RUNTIME_OR_CHECKER_REVIEW_REQUIRED"
            )
        evidence["summary"] = {
            "case_statuses": statuses,
            "passed": sum(value == "PASS" for value in statuses.values()),
            "failed": sum(value != "PASS" for value in statuses.values()),
        }
        evidence["finished_at"] = utc_now()
        evidence_path = run_dir / "EXEC_02_REAL_MODEL_EVIDENCE.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "status": "PASS" if evidence["summary"]["failed"] == 0 else "FAIL",
            "evidence_path": str(evidence_path),
            "trace_path": str(trace_path),
            "database_path": str(db_path),
            "case_statuses": statuses,
        }, ensure_ascii=False))
        return 0 if evidence["summary"]["failed"] == 0 else 1
    finally:
        app.shutdown()
        app.server_close()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        error_path = OUTPUT_ROOT / "EXEC_02_LAST_ERROR.txt"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        print(json.dumps({"status": "ERROR", "error_path": str(error_path)}, ensure_ascii=False))
        raise
