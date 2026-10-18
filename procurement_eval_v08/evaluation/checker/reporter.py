"""Strict Pass Markdown 报告。"""
from __future__ import annotations

import json
from typing import Any


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def score_markdown(score: dict[str, Any]) -> str:
    lines = [
        f"# {score.get('case_id') or 'UNKNOWN'} Strict Pass 评分报告",
        "",
        f"- Run ID：`{score.get('run_id') or 'UNKNOWN'}`",
        f"- 最终结果：**{score['final_result']}**",
        f"- Strict Pass：`{str(score['strict_pass']).lower()}`",
        f"- Checker：`{score['checker_version']}`",
        f"- Evidence SHA-256：`{score['evidence']['sha256'] or 'UNAVAILABLE'}`",
        f"- Evidence：`{score['evidence']['path']}`",
        "",
        "## Success Criteria",
        "",
        "| ID | 结果 | 模式 | Success Criterion | 原因 |",
        "|---|---|---|---|---|",
    ]
    for item in score.get("criteria", []):
        lines.append(
            f"| {item['criterion_id']} | {item['status']} | {item['evaluation_mode']} | "
            f"{_cell(item['success_criterion'])} | {_cell(item.get('reason'))} |"
        )
    lines.extend(["", "## 逐项证据", ""])
    for item in score.get("criteria", []):
        lines.extend([
            f"### {item['criterion_id']} · {item['status']}",
            "",
            f"- Checker：`{item['checker_id']}`",
            f"- 实际观察：`{json.dumps(item.get('observed', {}), ensure_ascii=False, sort_keys=True)}`",
            f"- 原因：{item.get('reason') or '无'}",
            "- Evidence 引用：",
            "",
        ])
        for ref in item.get("evidence_refs", []):
            lines.append(f"  - `{ref['pointer']}`：{ref['description']}")
        if item.get("review_prompt"):
            lines.append(f"  - 人工复核问题：{item['review_prompt']}")
        lines.append("")
    if score.get("hard_violations"):
        lines.extend(["## 硬性违规", ""])
        for violation in score["hard_violations"]:
            lines.append(f"- `{violation['code']}`：{violation['reason']}")
        lines.append("")
    if score.get("failure_reasons"):
        lines.extend(["## 汇总结论", ""])
        lines.extend(f"- {reason}" for reason in score["failure_reasons"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def combined_markdown(scores: list[dict[str, Any]]) -> str:
    lines = [
        "# EVAL-02 离线 Strict Pass 评分报告",
        "",
        "| Case | Run ID | 结果 | Strict Pass | PASS | FAIL | REVIEW | 缺证 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for score in scores:
        counts = score.get("summary", {}).get("criterion_counts", {})
        lines.append(
            f"| {score.get('case_id')} | `{score.get('run_id')}` | **{score['final_result']}** | "
            f"{'是' if score['strict_pass'] else '否'} | {counts.get('PASS', 0)} | "
            f"{counts.get('FAIL', 0)} | {counts.get('REVIEW_REQUIRED', 0)} | "
            f"{counts.get('EVIDENCE_MISSING', 0)} |"
        )
    lines.extend(["", "## 结论", ""])
    for score in scores:
        reasons = "；".join(score.get("failure_reasons") or ["全部 Success Criteria 满足且无硬性违规。"])
        lines.append(f"- **{score.get('case_id')} · {score['final_result']}**：{reasons}")
    return "\n".join(lines).rstrip() + "\n"
