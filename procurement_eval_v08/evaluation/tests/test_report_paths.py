"""报告输出路径测试；仅使用临时文件和固定评分结果，不执行历史复评。"""
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from evaluation.batch import BatchEvaluator
from evaluation.checker.cli import score_main
from evaluation.checker.scorer import CHECKER_VERSION, OfflineScorer


class ReportPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs = self.root / "evaluation" / "runs"
        self.run_dir = self.runs / "report-path-unit"
        self.run_dir.mkdir(parents=True)
        self.path = self.run_dir / "evidence.json"
        self.evidence = b'{"purpose":"report-path-test-only"}'
        self.path.write_bytes(self.evidence)
        self.reports = self.runs.parent / "reports"
        self.score = {
            "case_id": "PATH_UNIT", "run_id": self.run_dir.name,
            "checker_version": CHECKER_VERSION, "final_result": "REVIEW_REQUIRED",
            "strict_pass": False,
            "evidence": {"path": str(self.path),
                         "sha256": hashlib.sha256(self.evidence).hexdigest()},
            "criteria": [{"criterion_id": "PATH_UNIT-01", "status": "REVIEW_REQUIRED",
                          "evaluation_mode": "manual", "success_criterion": "仅测试输出位置",
                          "checker_id": "path_unit", "evidence_refs": []}],
        }

    def assert_evidence_and_score_preserved(self):
        self.assertEqual(self.path.read_bytes(), self.evidence)
        lines = (self.run_dir / "scored.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(json.loads(line), self.score)
        self.assertTrue((self.run_dir / "manual_review.template.json").exists())
        self.assertFalse((self.run_dir / "score_report.md").exists())

    def test_default_persist_separates_report_and_keeps_review_data(self):
        # 把模块位置映射到临时项目，验证默认路径而不写入真实评估目录。
        module_path = self.root / "evaluation" / "checker" / "scorer.py"
        with patch("evaluation.checker.scorer.__file__", str(module_path)):
            first = OfflineScorer.persist(self.score, self.run_dir)
            OfflineScorer.persist(self.score, self.run_dir)
        self.assertEqual(Path(first["score_report"]), self.reports / self.run_dir.name / "score_report.md")
        self.assertTrue(Path(first["score_report"]).exists())
        self.assertEqual(len((self.run_dir / "scored.jsonl").read_text(encoding="utf-8").splitlines()), 2)
        self.assert_evidence_and_score_preserved()

    def test_score_cli_uses_reports_for_individual_and_combined_reports(self):
        output = io.StringIO()
        with patch("evaluation.checker.cli.CaseLoader"), \
                patch.object(OfflineScorer, "score_file", return_value=self.score), \
                redirect_stdout(output):
            result = score_main(["--run-id", self.run_dir.name], self.root / "suite", self.runs)
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(Path(payload["combined_report"]), self.reports / "score_report.md")
        self.assertTrue((self.reports / self.run_dir.name / "score_report.md").exists())
        self.assertTrue((self.reports / "score_report.md").exists())
        self.assertFalse((self.runs / "score_report.md").exists())
        self.assert_evidence_and_score_preserved()

    def test_batch_respects_configured_reports_directory(self):
        with patch.object(OfflineScorer, "score_file", return_value=self.score):
            result = BatchEvaluator(None, self.runs, self.reports).score_existing([self.path])
        self.assertTrue((self.reports / self.run_dir.name / "score_report.md").exists())
        self.assertTrue(Path(result["manifest"]["report_path"]).is_relative_to(self.reports))
        self.assertTrue(Path(result["manifest"]["metrics_path"]).is_relative_to(self.reports))
        self.assert_evidence_and_score_preserved()


if __name__ == "__main__":
    unittest.main()
