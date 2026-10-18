"""离线评分命令行入口。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.case_loader import CaseLoader

from .reporter import combined_markdown
from .scorer import CHECKER_VERSION, OfflineScorer


def build_parser(default_suite_dir: Path | None, default_runs_dir: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="对已保存的采购 Agent Evidence 进行离线 Strict Pass 评分")
    parser.add_argument("--run-id", action="append", default=[], help="evaluation/runs 下的 Run ID，可重复")
    parser.add_argument("--evidence", action="append", type=Path, default=[], help="evidence.json 路径，可重复")
    parser.add_argument("--review-file", action="append", type=Path, default=[], help="人工复核 JSON，可重复")
    parser.add_argument("--case-dir", "--suite-dir", dest="suite_dir", type=Path, default=default_suite_dir)
    parser.add_argument("--runs-dir", type=Path, default=default_runs_dir)
    parser.add_argument("--report", type=Path, default=None, help="合并 Markdown 报告路径")
    parser.add_argument("--no-persist", action="store_true", help="只输出结果，不写 scored.jsonl")
    return parser


def score_main(argv: list[str], default_suite_dir: Path | None, default_runs_dir: Path) -> int:
    args = build_parser(default_suite_dir, default_runs_dir).parse_args(argv)
    paths = [args.runs_dir / run_id / "evidence.json" for run_id in args.run_id]
    expected_runs = {path.resolve(): run_id for path, run_id in zip(paths, args.run_id)}
    paths.extend(args.evidence)
    # 同一路径只评分一次，保持调用顺序。
    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            unique_paths.append(resolved)
            seen.add(resolved)
    if not unique_paths:
        raise SystemExit("必须至少提供一个 --run-id 或 --evidence")

    scorer = OfflineScorer(CaseLoader(args.suite_dir))
    scores: list[dict] = []
    outputs: list[dict] = []
    for path in unique_paths:
        expected = {"run_id": expected_runs[path]} if path in expected_runs else None
        score = scorer.score_file(path, review_files=args.review_file, expected_context=expected)
        scores.append(score)
        output = {}
        if not args.no_persist and path.exists():
            output = scorer.persist(score, path.parent,
                                    report_dir=args.runs_dir.parent / "reports" / path.parent.name)
        outputs.append({
            "case_id": score.get("case_id"),
            "run_id": score.get("run_id"),
            "final_result": score["final_result"],
            "strict_pass": score["strict_pass"],
            "evidence_sha256": score["evidence"]["sha256"],
            **output,
        })

    report_path = args.report or (args.runs_dir.parent / "reports" / "score_report.md")
    if not args.no_persist:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(combined_markdown(scores), encoding="utf-8")
    print(json.dumps({
        "checker_version": CHECKER_VERSION,
        "scores": outputs,
        "combined_report": str(report_path.resolve()) if not args.no_persist else None,
    }, ensure_ascii=False, indent=2))
    return 0
