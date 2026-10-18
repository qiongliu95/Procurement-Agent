"""审计交付的离线门禁回归，不调用模型或被测业务系统。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation import audit_golden_v11 as audit
from evaluation import audit_golden_v11_reporting as reporting


class GoldenQualityAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = audit.load_cases()
        cls.sources = audit.golden.load_sources()

    def test_missing_v11_zip_never_falls_back_to_v1(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(audit, "ZIP", Path(folder) / "missing.zip"):
                with self.assertRaises(FileNotFoundError): audit.load_cases()

    def test_preflight_never_calls_a_judge(self):
        with patch.object(audit, "make_adapter", side_effect=AssertionError("模型不应被调用")):
            cases, _ = audit.preflight()
        self.assertEqual(len(cases), 46)

    def test_bad_fact_binding_blocks_high_other_scores(self):
        probes = json.loads((audit.REPORT / "point_probe_results.json").read_text(encoding="utf-8"))
        bindings = json.loads((audit.REPORT / "binding_probe_results.json").read_text(encoding="utf-8"))
        rows = {r["case_id"]: r for r in reporting.make_rows(self.cases, self.sources, probes, bindings)}
        for cid in ["RGV1-B15", "RGV1-C04"]:
            self.assertEqual(rows[cid]["classification"], "SKIPPED")
            self.assertFalse(rows[cid]["quality_gate_pass"])
            self.assertEqual(next(d["score"] for d in rows[cid]["dimension_scores"] if d["dimension"] == "D6"), 0)
        self.assertFalse(rows["RGV1-A01"]["quality_gate_pass"])
        self.assertNotEqual(rows["RGV1-U02"]["classification"], "PASS")

    def test_json_repair_preserves_meaning_and_original_error(self):
        payload = dict(needed_facts=[{"fact": "原事实", "doc_id": "KB-004", "quote": "PO 创建失败时不扣减"}],
                       answerability_estimate="FULL", sufficient_doc_sets=[["KB-004"]])
        malformed = '{\n {"type": "json_object"}\n ' + json.dumps(payload, ensure_ascii=False) + '\n}'
        record = dict(case_id="SAMPLE", dimension="blind_requirements", exception={"type": "JSONDecodeError"},
                      response={"choices": [{"message": {"content": malformed}}]})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "calls" / "sample.json"
            audit.save(path, record)
            before = path.read_bytes()
            with patch.object(audit, "RUN", root): recovered = audit.recover_blind_json(record, path)
            self.assertEqual(recovered, payload)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue((root / "repaired/sample.json").exists())

    def test_multiline_quote_uses_unannotated_source(self):
        case = next(c for c in self.cases if c["case_id"] == "RGV1-A06")
        quote = "## 版本冲突\n" + case["golden_evidence"][0]["evidence_text"]
        result = dict(supporting_evidence=[dict(source="KB-009:L23-L24", quote=quote)], confidence=.9, uncertainty=True)
        verified = audit.quote_validation(result, case, self.sources)
        self.assertTrue(verified["fully_verified"])
        self.assertFalse(verified["uncertainty"])
        self.assertTrue(verified["original_uncertainty"])

    def test_protected_inputs_still_match_initial_baseline(self):
        initial = json.loads((audit.RUN / "protected_baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(audit.protected_snapshot(), initial)


if __name__ == "__main__": unittest.main()
