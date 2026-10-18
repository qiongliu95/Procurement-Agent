"""评估结果去重、指标计算与 Bad Case 归因。"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


DECISIVE = {"PASS", "FAIL"}
EXECUTED = DECISIVE | {"REVIEW_REQUIRED"}


def unique_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 Run 只保留最后一次评分，避免离线复评重复计数。"""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for score in scores:
        key = (str(score.get("suite") or "agent"), str(score.get("run_id") or score.get("score_id")))
        if key not in latest:
            order.append(key)
        latest[key] = score
    return [latest[key] for key in order]


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4) if denominator else None,
    }


def calculate_metrics(scores: list[dict[str, Any]], *, agent_total: int = 33, backend_total: int = 20) -> dict[str, Any]:
    values = unique_scores(scores)
    runs = {suite: [score for score in values if (score.get("suite") or "agent") == suite]
            for suite in ("agent", "backend")}
    latest_cases: dict[tuple[str, str], tuple[tuple[str, int], dict[str, Any]]] = {}
    for index, score in enumerate(values):
        if not score.get("case_id"):
            continue
        key = (score.get("suite") or "agent", score["case_id"])
        # 重试以最新执行结果为准，不能挑历史 PASS；离线复评不把旧 Run 提升为最新执行。
        order = (str(score.get("run_started_at") or ""), index)
        if key not in latest_cases or order > latest_cases[key][0]:
            latest_cases[key] = (order, score)
    agent = [score for (suite, _), (_, score) in latest_cases.items() if suite == "agent"]
    backend = [score for (suite, _), (_, score) in latest_cases.items() if suite == "backend"]
    executed_ids = {suite: {score["case_id"] for score in entries
                            if score.get("case_id") and score.get("final_result") in EXECUTED}
                    for suite, entries in runs.items()}
    # 覆盖记录已发生的执行，不因后续无效证据重试而丢失；成功仍必须由最新结果确认。
    agent_executed = [score for score in agent if score["case_id"] in executed_ids["agent"]]
    backend_executed = [score for score in backend if score["case_id"] in executed_ids["backend"]]
    agent_counts = Counter(score.get("final_result") for score in agent)
    backend_counts = Counter(score.get("final_result") for score in backend)
    agent_decisive = [score for score in agent if score.get("final_result") in DECISIVE]
    compliance = [score for score in agent_decisive if score.get("category") in {"B", "E", "P"}]
    auth = [score for score in agent_decisive if score.get("category") == "P"]
    by_category: dict[str, Counter] = defaultdict(Counter)
    tool_calls: Counter = Counter()
    tool_outcomes: Counter = Counter()
    routes: Counter = Counter()
    for score in agent:
        by_category[str(score.get("category") or "UNKNOWN")][str(score.get("final_result"))] += 1
    for score in runs["agent"]:
        tool_calls.update(score.get("diagnostics", {}).get("tool_calls_by_name", {}))
        tool_outcomes.update(score.get("diagnostics", {}).get("tool_outcomes", {}))
        routes.update(score.get("diagnostics", {}).get("routes", {}))
    return {
        "agent": {
            "case_total": agent_total,
            "unique_run_count": len(runs["agent"]),
            "total_runs": len(runs["agent"]),
            "retry_count": sum(count - 1 for count in Counter(score.get("case_id") for score in runs["agent"] if score.get("case_id")).values()),
            "executed_case_count": len(agent_executed),
            "status_counts": dict(agent_counts),
            "task_success_rate": _rate(sum(score["final_result"] == "PASS" for score in agent_executed), len(agent_executed)),
            "business_compliance_rate": _rate(sum(score["final_result"] == "PASS" for score in compliance), len(compliance)),
            "authorization_violation_rate": _rate(sum(score["final_result"] == "FAIL" for score in auth), len(auth)),
            "case_coverage": _rate(len(agent_executed), agent_total),
            "evaluation_coverage": _rate(len(agent_executed), agent_total),
            "category_distribution": {category: dict(counts) for category, counts in sorted(by_category.items())},
            "tool_call_analysis": {
                "total": sum(tool_calls.values()),
                "by_name": dict(tool_calls.most_common()),
                "outcomes": dict(tool_outcomes.most_common()),
                "routes": dict(routes.most_common()),
            },
        },
        "backend": {
            "case_total": backend_total,
            "unique_run_count": len(runs["backend"]),
            "total_runs": len(runs["backend"]),
            "retry_count": sum(count - 1 for count in Counter(score.get("case_id") for score in runs["backend"] if score.get("case_id")).values()),
            "executed_case_count": len(backend_executed),
            "status_counts": dict(backend_counts),
            "pass_rate": _rate(sum(score["final_result"] == "PASS" for score in backend_executed), len(backend_executed)),
            "case_coverage": _rate(len(backend_executed), backend_total),
            "evaluation_coverage": _rate(len(backend_executed), backend_total),
        },
        "bad_cases": bad_cases([score for _, score in latest_cases.values()] + [score for score in values if not score.get("case_id")]),
    }


def bad_cases(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for score in scores:
        if score.get("final_result") == "PASS":
            continue
        failed = []
        for criterion in score.get("criteria", []):
            if criterion.get("status") != "PASS":
                failed.append({
                    "criterion_id": criterion.get("criterion_id"),
                    "criterion": criterion.get("success_criterion"),
                    "status": criterion.get("status"),
                    "reason": criterion.get("reason"),
                    "evidence_refs": criterion.get("evidence_refs", []),
                })
        result.append({
            "suite": score.get("suite") or "agent",
            "case_id": score.get("case_id"),
            "run_id": score.get("run_id"),
            "result": score.get("final_result"),
            "failure_reasons": score.get("failure_reasons", []),
            "criteria": failed,
            "evidence": score.get("evidence", {}),
        })
    return result
