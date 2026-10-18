"""统一评估命令行入口。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checker.cli import score_main
from .batch import batch_main
from .case_loader import CaseLoader
from .runner import EvaluationRunner, load_env_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE_DIR = None
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent / "runs"
DEFAULT_CASES = ["E01", "E02B", "A02_BC03"]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="采购 Agent v4 自动化评估 Harness")
    result.add_argument("--case", action="append", dest="case_ids", help="Case ID，可重复指定")
    result.add_argument("--case-dir", "--suite-dir", dest="suite_dir", type=Path, default=DEFAULT_SUITE_DIR,
                        help="评估集目录；默认读取 PROCUREMENT_EVAL_CASE_DIR 或当前/旧版目录")
    result.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    result.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    result.add_argument("--no-env-file", action="store_true")
    result.add_argument("--list", action="store_true", help="只列出正式 v4 Agent Case")
    return result


def run_main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    loader = CaseLoader(args.suite_dir)
    if args.list:
        print(json.dumps({"case_ids": loader.list_case_ids()}, ensure_ascii=False, indent=2))
        return 0
    if not args.no_env_file:
        load_env_file(args.env_file)
    case_ids = args.case_ids or DEFAULT_CASES
    results = EvaluationRunner(loader, args.runs_dir).run_many(case_ids)
    summary = [{
        "case_id": item["case"]["case_id"],
        "run_id": item["run"]["run_id"],
        "status": item["run"]["status"],
        "status_reason": item["run"]["status_reason"],
        "evidence_dir": str((args.runs_dir / item["run"]["run_id"]).resolve()),
    } for item in results]
    print(json.dumps({"runs": summary}, ensure_ascii=False, indent=2))
    success = {"COMPLETED_AWAITING_SCORING", "EVIDENCE_INCOMPLETE"}
    return 0 if all(item["status"] in success for item in summary) else 2


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "full":
        from .unified import full_main
        return full_main(argv[1:])
    if argv and argv[0] == "batch":
        return batch_main(argv[1:], DEFAULT_SUITE_DIR, DEFAULT_RUNS_DIR)
    if argv and argv[0] == "score":
        return score_main(argv[1:], DEFAULT_SUITE_DIR, DEFAULT_RUNS_DIR)
    if argv and argv[0] == "run":
        argv = argv[1:]
    return run_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
