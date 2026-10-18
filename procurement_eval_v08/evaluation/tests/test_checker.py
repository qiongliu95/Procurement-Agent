import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.case_loader import CaseLoader, resolve_case_dir
from evaluation.checker.context import resolve_pointer
from evaluation.checker.scorer import OfflineScorer


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = resolve_case_dir()
RUNS_DIR = PROJECT_ROOT / "evaluation" / "runs"
RUN_IDS = {
    "E01": "E01-20261013T234731-b81f0545",
    "E02B": "E02B-20261013T234738-798b08c7",
    "A02_BC03": "A02_BC03-20261013T234802-36bf07d4",
}


class CheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scorer = OfflineScorer(CaseLoader(SUITE_DIR))

    @staticmethod
    def evidence_path(case_id):
        return RUNS_DIR / RUN_IDS[case_id] / "evidence.json"

    def test_real_e01_is_strict_pass(self):
        score = self.scorer.score_file(self.evidence_path("E01"))
        self.assertEqual(score["final_result"], "PASS")
        self.assertTrue(score["strict_pass"])
        self.assertEqual([item["status"] for item in score["criteria"]], ["PASS", "PASS"])

    def test_real_e02b_detects_missing_final_confirmation(self):
        score = self.scorer.score_file(self.evidence_path("E02B"))
        self.assertEqual(score["final_result"], "FAIL")
        self.assertFalse(score["strict_pass"])
        self.assertEqual(
            [item["status"] for item in score["criteria"]],
            ["FAIL", "FAIL", "PASS"],
        )
        observed = score["criteria"][0]["observed"]
        self.assertEqual(observed["route"]["route_reason_code"], "SUSPENDED_TASK_DECISION_REQUIRED")

    def test_real_a02_requires_semantic_review_but_proves_isolation(self):
        score = self.scorer.score_file(self.evidence_path("A02_BC03"))
        self.assertEqual(score["final_result"], "REVIEW_REQUIRED")
        self.assertEqual(
            [item["status"] for item in score["criteria"]],
            ["PASS", "REVIEW_REQUIRED", "PASS"],
        )
        self.assertEqual(score["manual_review"]["required_criterion_ids"], ["SC-02"])
        self.assertEqual(score["criteria"][2]["observed"]["inherited_forbidden_fields"], {})

    def test_all_evidence_references_resolve_against_original_evidence(self):
        for case_id in RUN_IDS:
            with self.subTest(case_id=case_id):
                path = self.evidence_path(case_id)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                score = self.scorer.score_file(path)
                for criterion in score["criteria"]:
                    for ref in criterion["evidence_refs"]:
                        resolve_pointer(evidence, ref["pointer"])

    def test_score_records_exact_evidence_hash_and_is_repeatable(self):
        path = self.evidence_path("E01")
        expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        first = self.scorer.score_file(path)
        second = self.scorer.score_file(path)
        self.assertEqual(first["evidence"]["sha256"], expected_hash)
        self.assertEqual(second["evidence"]["sha256"], expected_hash)
        self.assertEqual(first["final_result"], second["final_result"])
        self.assertNotEqual(first["score_id"], second["score_id"])

    def test_missing_trace_is_evidence_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence = json.loads(self.evidence_path("E01").read_text(encoding="utf-8"))
            del evidence["trace"]["events"]
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            score = self.scorer.score_file(path)
            self.assertEqual(score["final_result"], "EVIDENCE_MISSING")

    def test_modified_success_criteria_is_invalid_case(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence = json.loads(self.evidence_path("E01").read_text(encoding="utf-8"))
            evidence["case"]["success_criteria"][0] = "被篡改的标准"
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            score = self.scorer.score_file(path)
            self.assertEqual(score["final_result"], "INVALID_CASE")

    def test_unexpected_write_is_a_hard_violation(self):
        with tempfile.TemporaryDirectory() as temp:
            evidence = json.loads(self.evidence_path("E01").read_text(encoding="utf-8"))
            evidence["business_state"]["after"]["tables"]["requests"].append({"id": "ILLEGAL"})
            evidence["business_state"]["delta"]["created_request_ids"] = ["ILLEGAL"]
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            score = self.scorer.score_file(path)
            self.assertEqual(score["final_result"], "FAIL")
            self.assertEqual(score["hard_violations"][0]["code"], "UNEXPECTED_BUSINESS_WRITE")

    def test_valid_manual_review_can_resolve_review_required(self):
        with tempfile.TemporaryDirectory() as temp:
            review_path = Path(temp) / "review.json"
            evidence_hash = hashlib.sha256(self.evidence_path("A02_BC03").read_bytes()).hexdigest()
            review_path.write_text(json.dumps({
                "schema_version": "manual-review-1.0",
                "case_id": "A02_BC03",
                "run_id": RUN_IDS["A02_BC03"],
                "evidence_sha256": evidence_hash,
                "reviews": [{
                    "criterion_id": "SC-02",
                    "decision": "FAIL",
                    "reviewer": "QA-Reviewer-01",
                    "reviewed_at": "2026-10-14T00:00:00+08:00",
                    "rationale": "额外意图澄清没有直接补问正式标准指定的三个字段。",
                    "evidence_refs": ["/steps/2/agent_output", "/steps/2/runtime_state_after/missing_slots"],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            score = self.scorer.score_file(
                self.evidence_path("A02_BC03"),
                review_files=[review_path],
            )
            self.assertEqual(score["final_result"], "FAIL")
            self.assertEqual(score["criteria"][1]["evaluation_mode"], "manual_review")
            self.assertEqual(score["manual_review"]["applied_criterion_ids"], ["SC-02"])

    def test_invalid_manual_reference_does_not_override_deterministic_result(self):
        with tempfile.TemporaryDirectory() as temp:
            review_path = Path(temp) / "review.json"
            evidence_hash = hashlib.sha256(self.evidence_path("A02_BC03").read_bytes()).hexdigest()
            review_path.write_text(json.dumps({
                "schema_version": "manual-review-1.0",
                "case_id": "A02_BC03",
                "run_id": RUN_IDS["A02_BC03"],
                "evidence_sha256": evidence_hash,
                "reviews": [{
                    "criterion_id": "SC-02",
                    "decision": "PASS",
                    "reviewer": "QA-Reviewer-01",
                    "reviewed_at": "2026-10-14T00:00:00+08:00",
                    "rationale": "测试非法引用。",
                    "evidence_refs": ["/steps/999"],
                }],
            }, ensure_ascii=False), encoding="utf-8")
            score = self.scorer.score_file(
                self.evidence_path("A02_BC03"),
                review_files=[review_path],
            )
            self.assertEqual(score["final_result"], "REVIEW_REQUIRED")
            self.assertTrue(score["manual_review"]["errors"])

    def test_persist_appends_scored_jsonl_and_writes_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            score = self.scorer.score_file(self.evidence_path("A02_BC03"))
            reports = directory / "reports"
            first = self.scorer.persist(score, directory, report_dir=reports)
            second_score = self.scorer.score_file(self.evidence_path("A02_BC03"))
            self.scorer.persist(second_score, directory, report_dir=reports)
            lines = (directory / "scored.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(Path(first["score_report"]).exists())
            self.assertEqual(Path(first["score_report"]).parent, reports)
            self.assertFalse((directory / "score_report.md").exists())
            self.assertTrue(Path(first["manual_review_template"]).exists())



if __name__ == "__main__":
    unittest.main()
