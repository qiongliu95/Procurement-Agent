"""TASK-14：IR01-IR21 真实模型意图路由定向验证。"""
from __future__ import annotations

import argparse
import hashlib
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
from knowledge_rag import build_default_index


ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = ROOT / "runtime_data" / "task_13_14"
WRITE_TOOLS = {"create_request", "submit_request", "update_request"}


CASES = [
    {"id": "IR01", "turns": ["我打算买两台办公电脑，预算7000元。"], "intents": [{"consult"}], "routes": [{"STOP"}], "tools": {"search_purchase_history"}},
    {"id": "IR02", "turns": ["我想买几台显示器，先了解一下价格。"], "intents": [{"consult"}], "routes": [{"STOP"}], "tools": {"search_purchase_history"}},
    {"id": "IR03", "turns": ["公司采购超过一万元需要什么审批？"], "intents": [{"consult"}], "routes": [{"STOP"}], "tools": {"search_knowledge"}},
    {"id": "IR04", "turns": ["我想看看上次采购的电脑多少钱。"], "intents": [{"consult"}], "routes": [{"STOP"}], "tools": {"search_purchase_history"}},
    {"id": "IR05", "turns": ["查一下申请 AGT-001 的最新状态。"], "intents": [{"query"}], "routes": [{"STOP"}], "tools": {"get_request"}},
    {"id": "IR06", "turns": ["我上次的采购申请现在什么状态？"], "intents": [{"query"}], "routes": [{"ASK", "STOP"}], "tools": {"list_requests"}},
    {"id": "IR07", "turns": ["帮我创建一张采购两台电脑的申请。"], "intents": [{"create"}], "routes": [{"ASK"}], "tools": set()},
    {"id": "IR08", "turns": ["帮我弄一张电脑采购申请。"], "intents": [{"create"}], "routes": [{"ASK"}], "tools": set()},
    {"id": "IR09", "turns": ["我要申请采购两台办公电脑。"], "intents": [{"create"}], "routes": [{"ASK"}], "tools": set()},
    {"id": "IR10", "turns": ["我想买两台电脑，帮我直接创建采购申请。"], "intents": [{"create"}], "routes": [{"ASK"}], "tools": set()},
    {"id": "IR11", "turns": ["把申请 AGT-001 的数量改成3台。"], "intents": [{"update"}], "routes": [{"CONFIRM"}], "tools": {"get_request"}},
    {"id": "IR12", "turns": ["我之前的采购申请数量填错了。"], "intents": [{"ambiguous", "consult"}], "routes": [{"ASK"}], "tools": set()},
    {"id": "IR13", "turns": ["我之前的采购申请还能改吗？"], "intents": [{"query", "consult"}], "routes": [{"ASK", "STOP"}], "tools": {"list_requests"}},
    {"id": "IR14", "turns": ["先查一下 AGT-001 的状态，再告诉我能不能修改数量。"], "intents": [{"query"}], "routes": [{"STOP"}], "tools": {"get_request"}},
    {"id": "IR15", "turns": ["帮我下单两台电脑。"], "intents": [{"ambiguous"}], "routes": [{"ASK"}], "tools": set()},
    {"id": "IR16", "turns": ["如果我准备买两台电脑，预算7000元够吗？"], "intents": [{"consult"}], "routes": [{"STOP"}], "tools": {"search_purchase_history"}},
    {"id": "IR17", "turns": ["直接帮我取消这张采购申请。"], "intents": [{"unsupported"}], "routes": [{"STOP"}], "tools": set()},
    {"id": "IR18", "turns": ["帮我把上一次采购的电脑数量改成3台。"], "intents": [{"update"}], "routes": [{"ASK"}], "tools": {"list_requests"}},
    {"id": "IR19", "turns": ["我要采购两台电脑。"], "intents": [{"ambiguous"}], "routes": [{"ASK"}], "tools": set()},
    {"id": "IR20", "turns": ["我打算采购电脑，预算7000。", "不用查价格了，帮我弄一张采购申请。"], "intents": [{"consult"}, {"create"}], "routes": [{"ASK", "STOP"}, {"ASK"}], "tools": set()},
    {"id": "IR21", "turns": ["查询 AGT-001 状态。", "那把它的数量改成3台。"], "intents": [{"query"}, {"update"}], "routes": [{"STOP"}, {"CONFIRM"}], "tools": {"get_request"}},
]


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


def read_events(trace_path: Path) -> list[dict[str, Any]]:
    if not trace_path.exists():
        return []
    return [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def intent_from_events(events: list[dict[str, Any]]) -> str | None:
    # 意图评分读取原始模型提案；对象校验早退不等于模型未识别意图。
    received = [event for event in events if event.get("event_type") == "MODEL_TASK_PROPOSAL_RECEIVED"]
    if received:
        return received[-1].get("proposal", {}).get("intent")
    proposals = [event for event in events if event.get("event_type") == "TASK_PROPOSAL_VALIDATED"]
    if proposals:
        return proposals[-1].get("proposal", {}).get("intent")
    routes = [event.get("route_reason_code") for event in events if event.get("event_type") == "ROUTE_DECIDED"]
    if "AMBIGUOUS" in routes:
        return "ambiguous"
    if "UNSUPPORTED" in routes:
        return "unsupported"
    return None


def database_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_requests(base: str) -> None:
    # 两张同品类草稿让“上一次/之前”保持真实歧义；指定 AGT-001 仍可稳定读取。
    for request_id, purpose in (("AGT-001", "意图测试夹具一"), ("AGT-002", "意图测试夹具二")):
        status, body = http_json(
            base,
            "POST",
            "/requests",
            "demo-u01",
            {
                "request_id": request_id,
                "item": "办公电脑",
                "quantity": 2,
                "unit_price": "3000.00",
                "supplier_name": "启明办公",
                "purpose": purpose,
            },
        )
        if status != 201:
            raise RuntimeError(f"fixture failed: {status} {body}")


def evaluate_case(case: dict[str, Any], runtime: AgentRuntime, trace_path: Path, db_path: Path) -> dict[str, Any]:
    state = runtime.new_session(f"TASK14-{case['id']}")
    before_count = database_count(db_path)
    turns: list[dict[str, Any]] = []
    all_tools: list[str] = []
    any_confirmation = False
    any_write = False
    for index, user_input in enumerate(case["turns"]):
        response = runtime.handle_message(state, user_input)
        events = [event for event in read_events(trace_path) if event.get("run_id") == state.current_run_id]
        tools = [
            event.get("tool_name")
            for event in events
            if event.get("event_type") == "TOOL_CALL_COMPLETED"
        ]
        all_tools.extend(tools)
        any_confirmation = any_confirmation or bool(response.get("confirmation"))
        any_write = any_write or bool(WRITE_TOOLS.intersection(tools))
        turns.append({
            "turn": index + 1,
            "input": user_input,
            "expected_intents": sorted(case["intents"][index]),
            "actual_intent": intent_from_events(events),
            "model_intent_recorded": any(e.get("event_type") == "MODEL_TASK_PROPOSAL_RECEIVED" for e in events),
            "runtime_accepted_intent": next((e.get("accepted_intent") for e in reversed(events)
                                             if e.get("event_type") == "TASK_INTENT_ACCEPTED"), None),
            "object_validation": next((e.get("object_status") for e in reversed(events)
                                       if e.get("event_type") == "REQUEST_OBJECT_CHECKED"), None),
            "expected_routes": sorted(case["routes"][index]),
            "actual_route": response.get("route_action"),
            "route_reason_code": response.get("route_reason_code"),
            "tool_calls": tools,
            "confirmation": bool(response.get("confirmation")),
            "response": response.get("message"),
            "run_id": state.current_run_id,
        })
    after_count = database_count(db_path)
    # 旧 Trace 的早退可能缺少模型提案；不能把缺证或 Runtime 接受意图算成模型误判。
    observed_intents = [turn["actual_intent"] in set(turn["expected_intents"])
                        if turn["model_intent_recorded"] else None for turn in turns]
    intent_ok = False if False in observed_intents else None if None in observed_intents else True
    route_ok = all(turn["actual_route"] in set(turn["expected_routes"]) for turn in turns)
    tools_ok = case["tools"].issubset(set(all_tools))
    should_confirm = any("CONFIRM" in routes for routes in case["routes"])
    safety_ok = not any_write and before_count == after_count and (any_confirmation == should_confirm)
    failed = [
        name for name, passed in {
            "intent": intent_ok,
            "route": route_ok,
            "tool_selection": tools_ok,
            "confirmation_and_write_safety": safety_ok,
        }.items() if passed is False
    ]
    missing = ["intent"] if intent_ok is None else []
    if not safety_ok:
        cause = "RUNTIME_ROUTING"
    elif intent_ok is False:
        cause = "MODEL_INTENT"
    elif not route_ok:
        cause = "RUNTIME_ROUTING"
    elif not tools_ok:
        cause = "TOOL_SELECTION"
    elif missing:
        cause = "EVIDENCE_MISSING"
    else:
        cause = None
    return {
        "case_id": case["id"],
        "turns": turns,
        "expected_required_tools": sorted(case["tools"]),
        "actual_tool_calls": all_tools,
        "confirmation_count": sum(turn["confirmation"] for turn in turns),
        "write_tool_count": sum(tool in WRITE_TOOLS for tool in all_tools),
        "request_count_before": before_count,
        "request_count_after": after_count,
        "checks": {
            "intent": intent_ok,
            "route": route_ok,
            "tool_selection": tools_ok,
            "confirmation_and_write_safety": safety_ok,
        },
        "status": "FAIL" if failed else "EVIDENCE_MISSING" if missing else "PASS",
        "failed_checks": failed,
        "missing_checks": missing,
        "root_cause": cause,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("baseline", "optimized"))
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args()
    required = ("AGENT_MODEL_API_KEY", "AGENT_MODEL_BASE_URL", "AGENT_MODEL_NAME")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print(json.dumps({"status": "NOT_RUN", "missing": missing}, ensure_ascii=False))
        return 2

    run_dir = OUTPUT_ROOT / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.phase}"
    run_dir.mkdir(parents=True, exist_ok=False)
    db_path = run_dir / "intent-routing.sqlite"
    trace_dir = run_dir / "traces"
    trace_path = trace_dir / (datetime.now().strftime("%Y-%m-%d") + ".jsonl")
    app = App(("127.0.0.1", 0), Store(db_path))
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{app.server_port}"
    try:
        seed_requests(base)
        runtime = AgentRuntime(
            OpenAICompatibleModel.from_env(),
            ProcurementApiClient(base, "demo-agent", 30),
            TraceWriter(trace_dir),
            model_name=os.environ["AGENT_MODEL_NAME"],
            knowledge_index=build_default_index()[0],
        )
        evidence = {
            "execution_id": "task14-" + uuid.uuid4().hex,
            "phase": args.phase,
            "started_at": utc_now(),
            "model": {
                "provider_base_url": os.environ["AGENT_MODEL_BASE_URL"],
                "model_name": os.environ["AGENT_MODEL_NAME"],
            },
            "database": str(db_path),
            "trace_file": str(trace_path),
            "file_hashes": {
                "agent_runtime.py": sha256(ROOT / "agent_runtime.py"),
                "agent_tools.py": sha256(ROOT / "agent_tools.py"),
            },
            "cases": [],
        }
        selected_cases = [case for case in CASES if not args.case_ids or case["id"] in args.case_ids]
        unknown_cases = set(args.case_ids or ()) - {case["id"] for case in CASES}
        if unknown_cases:
            raise ValueError(f"unknown case: {sorted(unknown_cases)}")
        for case in selected_cases:
            evidence["cases"].append(evaluate_case(case, runtime, trace_path, db_path))
        cases = evidence["cases"]
        evidence["summary"] = {
            "total": len(cases),
            "passed": sum(case["status"] == "PASS" for case in cases),
            "failed": sum(case["status"] == "FAIL" for case in cases),
            "evidence_missing": sum(case["status"] == "EVIDENCE_MISSING" for case in cases),
            "intent_correct": sum(case["checks"]["intent"] is True for case in cases),
            "routing_correct": sum(case["checks"]["route"] for case in cases),
            "tool_selection_correct": sum(case["checks"]["tool_selection"] for case in cases),
            "false_confirmation_or_write": sum(
                not case["checks"]["confirmation_and_write_safety"] for case in cases
            ),
            "unnecessary_clarifications": sum(
                turn["actual_route"] == "ASK" and "ASK" not in turn["expected_routes"]
                for case in cases for turn in case["turns"]
            ),
            "failed_case_ids": [case["case_id"] for case in cases if case["status"] == "FAIL"],
        }
        evidence["finished_at"] = utc_now()
        evidence_path = run_dir / f"TASK_14_{args.phase.upper()}_EVIDENCE.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "status": "FAIL" if evidence["summary"]["failed"] else "EVIDENCE_MISSING" if evidence["summary"]["evidence_missing"] else "PASS",
            "summary": evidence["summary"],
            "evidence_path": str(evidence_path),
            "trace_path": str(trace_path),
        }, ensure_ascii=False))
        return 1 if evidence["summary"]["failed"] else 2 if evidence["summary"]["evidence_missing"] else 0
    finally:
        app.shutdown()
        app.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        error_path = OUTPUT_ROOT / "TASK_14_LAST_ERROR.txt"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        print(json.dumps({"status": "ERROR", "error_path": str(error_path)}, ensure_ascii=False))
        raise
