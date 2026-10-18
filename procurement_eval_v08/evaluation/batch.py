"""Agent / Backend Suite 批量执行、评分和报告。"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backend_runner import BackendCaseRunner
from .case_loader import CaseLoader
from .checker.scorer import OfflineScorer
from .metrics import calculate_metrics
from .reporting import evaluation_markdown
from .runner import EvaluationRunner, load_env_file


def _batch_id() -> str:
    return "batch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


class BatchEvaluator:
    def __init__(self, loader: CaseLoader, runs_dir: Path, reports_dir: Path):
        self.loader = loader
        self.runs_dir = runs_dir.resolve()
        self.reports_dir = reports_dir.resolve()

    def execute(self, *, suite: str, case_ids: list[str] | None = None) -> dict[str, Any]:
        selected = set(case_ids or [])
        agent_ids = [cid for cid in self.loader.list_case_ids() if not selected or cid in selected]
        backend_ids = [cid for cid in self.loader.list_backend_case_ids() if not selected or cid in selected]
        unknown = selected - set(agent_ids) - set(backend_ids)
        if unknown:
            raise ValueError("未知 Case ID：" + ", ".join(sorted(unknown)))
        evidence: list[dict[str, Any]] = []
        if suite in {"agent", "all"}:
            evidence.extend(EvaluationRunner(self.loader, self.runs_dir).run_many(agent_ids))
        if suite in {"backend", "all"}:
            evidence.extend(BackendCaseRunner(self.loader, self.runs_dir).run_many(backend_ids))
        scorer = OfflineScorer(self.loader)
        scores = []
        expected_cases = (agent_ids if suite in {"agent", "all"} else []) + (backend_ids if suite in {"backend", "all"} else [])
        for expected_case_id, item in zip(expected_cases, evidence):
            path = self.runs_dir / item["run"]["run_id"] / "evidence.json"
            score = scorer.score_file(path, expected_context={
                "case_id": expected_case_id, "run_id": item["run"]["run_id"],
                "session_id": item["run"]["session_id"],
            })
            scorer.persist(score, path.parent, report_dir=self.reports_dir / path.parent.name)
            scores.append(score)
        return self.persist(scores, requested_suite=suite)

    def score_existing(self, evidence_paths: list[Path]) -> dict[str, Any]:
        scorer = OfflineScorer(self.loader)
        scores = []
        seen: set[Path] = set()
        for path in evidence_paths:
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            score = scorer.score_file(path)
            if path.exists():
                scorer.persist(score, path.parent, report_dir=self.reports_dir / path.parent.name)
            scores.append(score)
        return self.persist(scores, requested_suite="offline-existing-evidence")

    def persist(self, scores: list[dict[str, Any]], *, requested_suite: str) -> dict[str, Any]:
        batch_id = _batch_id()
        directory = self.reports_dir / batch_id
        directory.mkdir(parents=True, exist_ok=False)
        metrics = calculate_metrics(scores)
        report = evaluation_markdown(scores)
        with (directory / "scored.jsonl").open("w", encoding="utf-8") as stream:
            for score in scores:
                stream.write(json.dumps(score, ensure_ascii=False, sort_keys=True) + "\n")
        (directory / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (directory / "report.md").write_text(report, encoding="utf-8")
        manifest = {
            "batch_id": batch_id,
            "requested_suite": requested_suite,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_ids": [score.get("run_id") for score in scores],
            "score_count": len(scores),
            "metrics_path": str((directory / "metrics.json").resolve()),
            "report_path": str((directory / "report.md").resolve()),
        }
        (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        # 稳定别名方便 CI 和人工验收读取，同时批次目录仍保留不可混淆的历史结果。
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        (self.reports_dir / "latest_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (self.reports_dir / "latest_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (self.reports_dir / "latest_report.md").write_text(report, encoding="utf-8")
        return {"manifest": manifest, "metrics": metrics}


def batch_main(argv: list[str], default_suite_dir: Path | None, default_runs_dir: Path) -> int:
    parser = argparse.ArgumentParser(description="批量执行并评分采购 Agent v4 Case")
    parser.add_argument("--suite", choices=("agent", "backend", "all"), default="all")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--run-id", action="append", default=[], help="离线重评分现有 Run，可重复")
    parser.add_argument("--evidence", action="append", type=Path, default=[], help="离线重评分 Evidence，可重复")
    parser.add_argument("--batch-manifest", action="append", type=Path, default=[], help="读取既有批次的全部 Run ID")
    parser.add_argument("--case-dir", "--suite-dir", dest="suite_dir", type=Path, default=default_suite_dir)
    parser.add_argument("--runs-dir", type=Path, default=default_runs_dir)
    parser.add_argument("--reports-dir", type=Path, default=default_runs_dir.parent / "reports")
    parser.add_argument("--env-file", type=Path, default=default_runs_dir.parent.parent / ".env")
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args(argv)
    offline_paths = list(args.evidence)
    offline_paths.extend(args.runs_dir / run_id / "evidence.json" for run_id in args.run_id)
    for manifest_path in args.batch_manifest:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        offline_paths.extend(args.runs_dir / run_id / "evidence.json" for run_id in manifest.get("run_ids", []))
    if offline_paths:
        result = BatchEvaluator(CaseLoader(args.suite_dir), args.runs_dir, args.reports_dir).score_existing(offline_paths)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.suite in {"agent", "all"} and not args.no_env_file:
        load_env_file(args.env_file)
    result = BatchEvaluator(CaseLoader(args.suite_dir), args.runs_dir, args.reports_dir).execute(
        suite=args.suite, case_ids=args.case_ids,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
