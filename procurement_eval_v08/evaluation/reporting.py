"""统一 EVAL-03 Markdown 报告。"""
from __future__ import annotations

from typing import Any

from .checker.scorer import CHECKER_VERSION
from .metrics import calculate_metrics, unique_scores


def _percent(metric: dict[str, Any]) -> str:
    value = metric.get("value")
    shown = "N/A" if value is None else f"{value * 100:.2f}%"
    return f"{shown}（{metric.get('numerator', 0)}/{metric.get('denominator', 0)}）"


def evaluation_markdown(scores: list[dict[str, Any]], *, title: str = "采购 Agent v4 完整业务评估报告") -> str:
    scores = unique_scores(scores)
    metrics = calculate_metrics(scores)
    agent = metrics["agent"]; backend = metrics["backend"]
    models = sorted({
        str(score.get("model", {}).get("model_name") or score.get("model", {}).get("model") or "见原始 Evidence")
        for score in scores if (score.get("suite") or "agent") == "agent"
    })
    lines = [
        f"# {title}", "",
        "## 配置", "",
        "- 评估集：`procurement_eval_v4`（4.0）",
        f"- Checker：`{CHECKER_VERSION}`",
        f"- Agent 模型：{', '.join(models) if models else '无 Agent Run'}",
        "- 计数口径：同一 Run 仅取最后一次评分；同一 Case 采用最新执行结果。Coverage 按唯一已执行 Case；成功率为最新结果PASS/已执行 Case（含待复核，不将其算成功）；无效或缺失证据不新增覆盖、不算成功。BCR/AVR仍按已确定PASS/FAIL计算。",
        "",
        "## 覆盖与核心指标", "",
        f"- Agent Evaluation Coverage：{_percent(agent['evaluation_coverage'])}",
        f"- Task Success Rate（TSR）：{_percent(agent['task_success_rate'])}",
        f"- Business Compliance Rate（BCR）：{_percent(agent['business_compliance_rate'])}",
        f"- Authorization Violation Rate（AVR）：{_percent(agent['authorization_violation_rate'])}",
        f"- Backend Evaluation Coverage：{_percent(backend['evaluation_coverage'])}",
        f"- Backend Case 通过率：{_percent(backend['pass_rate'])}",
        f"- Agent Run Statistics：total_runs={agent['total_runs']}，retry_count={agent['retry_count']}",
        f"- Backend Run Statistics：total_runs={backend['total_runs']}，retry_count={backend['retry_count']}",
        f"- Agent 状态分布：`{agent['status_counts']}`",
        f"- Backend 状态分布：`{backend['status_counts']}`",
        "",
        "## Agent 分类结果", "",
        "| 分类 | PASS | FAIL | REVIEW_REQUIRED | EVIDENCE_MISSING | INVALID_CASE | INVALID_EVIDENCE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for category, counts in agent["category_distribution"].items():
        lines.append(f"| {category} | {counts.get('PASS', 0)} | {counts.get('FAIL', 0)} | {counts.get('REVIEW_REQUIRED', 0)} | {counts.get('EVIDENCE_MISSING', 0)} | {counts.get('INVALID_CASE', 0)} | {counts.get('INVALID_EVIDENCE', 0)} |")
    tool_analysis = agent["tool_call_analysis"]
    lines += [
        "", "## Tool Calling 诊断", "",
        f"- 调用总数：{tool_analysis['total']}",
        f"- 按工具：`{tool_analysis['by_name']}`",
        f"- 调用结果：`{tool_analysis['outcomes']}`",
        f"- 路由分布：`{tool_analysis['routes']}`",
        "", "## Case 明细", "", "| Suite | Case | Run | Result | Evidence |", "|---|---|---|---|---|",
    ]
    for score in scores:
        evidence = score.get("evidence", {})
        lines.append(f"| {score.get('suite') or 'agent'} | {score.get('case_id')} | {score.get('run_id')} | {score.get('final_result')} | `{evidence.get('path', '')}` |")
    lines += ["", "## Bad Case 与待复核", ""]
    if not metrics["bad_cases"]:
        lines.append("无。")
    for bad in metrics["bad_cases"]:
        lines += [f"### {bad['case_id']} · {bad['result']}", "", f"- Run：`{bad['run_id']}`", f"- Evidence：`{bad['evidence'].get('path', '')}`"]
        for item in bad["criteria"]:
            refs = ", ".join(f"`{ref.get('pointer')}`" for ref in item.get("evidence_refs", []))
            lines.append(f"- {item['criterion_id']} [{item['status']}] {item['criterion']}：{item.get('reason') or '见实际观察'}；证据 {refs}")
        lines.append("")
    lines += [
        "## 已知限制", "",
        "- 自然语言解释质量、供应商消歧表达及字段来源语义，在结构化证据不足时保留为 REVIEW_REQUIRED，需要使用生成的人工复核模板闭环。",
        "- 待复核 Case 不计成功但计已执行；缺失/无效证据不计已执行 Case，独立展示，不能当作 Agent FAIL。",
        "- 工具调用详情作为诊断信息保存在各 Run 的 `trace.jsonl`、`raw.jsonl` 与 SQLite 审计表中。",
        "",
    ]
    return "\n".join(lines)
