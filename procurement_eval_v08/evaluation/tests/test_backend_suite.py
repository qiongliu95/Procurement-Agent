from pathlib import Path
import json
import tempfile
import unittest

from evaluation.backend_runner import BackendCaseRunner
from evaluation.case_loader import CaseLoader, resolve_case_dir
from evaluation.checker.context import resolve_pointer
from evaluation.checker.scorer import OfflineScorer
from evaluation.metrics import calculate_metrics, unique_scores


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = resolve_case_dir()


class BackendSuiteTests(unittest.TestCase):
    def test_all_backend_cases_execute_and_strict_pass(self):
        loader = CaseLoader(SUITE_DIR)
        with tempfile.TemporaryDirectory() as temp:
            runs_dir = Path(temp)
            evidence = BackendCaseRunner(loader, runs_dir).run_many(loader.list_backend_case_ids())
            scorer = OfflineScorer(loader)
            scores = []
            for item in evidence:
                self.assertEqual(item["run"]["status"], "COMPLETED_AWAITING_SCORING")
                path = runs_dir / item["run"]["run_id"] / "evidence.json"
                score = scorer.score_file(path)
                scores.append(score)
                self.assertEqual(score["final_result"], "PASS", score["case_id"])
                for criterion in score["criteria"]:
                    for ref in criterion["evidence_refs"]:
                        resolve_pointer(item, ref["pointer"])
            metrics = calculate_metrics(scores)
            self.assertEqual(metrics["backend"]["evaluation_coverage"]["value"], 1.0)
            self.assertEqual(metrics["backend"]["pass_rate"]["value"], 1.0)

    def test_same_run_is_counted_once_after_rescore(self):
        score = {
            "score_id": "one", "suite": "agent", "run_id": "run-1",
            "case_id": "E01", "category": "N", "final_result": "FAIL",
            "criteria": [], "evidence": {},
        }
        replacement = {**score, "score_id": "two", "final_result": "PASS"}
        values = unique_scores([score, replacement])
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["final_result"], "PASS")

    def test_full_agent_batch_has_checker_and_resolvable_evidence_for_all_cases(self):
        manifest_path = PROJECT_ROOT / "evaluation" / "reports" / "batch-20261013T164257-61b9351f" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        loader = CaseLoader(SUITE_DIR)
        scorer = OfflineScorer(loader)
        results = []
        for run_id in manifest["run_ids"]:
            evidence_path = PROJECT_ROOT / "evaluation" / "runs" / run_id / "evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            score = scorer.score_file(evidence_path)
            results.append(score["final_result"])
            self.assertNotIn(score["final_result"], {"INVALID_CASE", "EVIDENCE_MISSING"})
            for criterion in score["criteria"]:
                for ref in criterion["evidence_refs"]:
                    resolve_pointer(evidence, ref["pointer"])
        self.assertEqual(len(results), 33)
        self.assertIn("PASS", results)
        self.assertIn("FAIL", results)
        self.assertIn("REVIEW_REQUIRED", results)


if __name__ == "__main__":
    unittest.main()
