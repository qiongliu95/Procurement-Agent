"""只测试Golden数据完整性和故障拒绝，不运行Agent、Backend或任何模型。"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evaluation import golden_dataset as g
from evaluation.golden_blueprints import BLUEPRINTS


class GoldenDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = g.load_sources()
        cls.by_id = {bp["id"]: bp for bp in BLUEPRINTS}

    def case(self, name="RGV1-A01"):
        return g.build_case(self.by_id[name], self.sources)

    def assert_rejected(self, case, message):
        result = g.validate_case(case, self.sources)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any(message in e for e in result["errors"]), result)

    def test_all_candidates_have_exact_sources(self):
        for bp in BLUEPRINTS:
            with self.subTest(bp["id"]):
                result = g.validate_case(g.build_case(bp, self.sources), self.sources, bp)
                self.assertEqual(result["status"], "PASS", result)

    def test_changed_source_hash_rejected(self):
        case = self.case()
        case["golden_evidence"][0]["source_hash"] = "0" * 64
        self.assert_rejected(case, "source_mismatch")

    def test_wrong_character_span_rejected(self):
        case = self.case()
        case["golden_evidence"][0]["source_span"]["char_start"] += 1
        self.assert_rejected(case, "source_mismatch")

    def test_wrong_line_or_section_rejected(self):
        for field, value in [("section", "错误章节"), ("source_path", "../outside.md")]:
            case = self.case()
            case["golden_evidence"][0][field] = value
            self.assert_rejected(case, "source_mismatch")

    def test_unreleased_document_rejected(self):
        case = self.case()
        case["golden_evidence"][0]["doc_id"] = "NON_RELEASED"
        self.assert_rejected(case, "source_mismatch")

    def test_numeric_rule_rewrite_rejected(self):
        case = self.case()
        case["required_facts"][0]["text"] = case["required_facts"][0]["text"].replace("大于或等于", "大于")
        case["golden_answer"] = g.render_answer(case)
        self.assert_rejected(case, "unsupported_fact")

    def test_fabricated_answer_rejected(self):
        case = self.case()
        case["golden_answer"] += "\n采购必须取得三家报价。"
        self.assert_rejected(case, "golden_contains_unmapped")

    def test_removed_exception_rejected(self):
        case = self.case("RGV1-A03")
        case["required_facts"][0]["text"] = case["required_facts"][0]["text"].split("重复请求")[0]
        case["golden_answer"] = g.render_answer(case)
        self.assert_rejected(case, "unsupported_fact")

    def test_guide_cannot_be_relabelled_company_rule(self):
        case = self.case("RGV1-A11")
        case["knowledge_type"] = "A"
        self.assert_rejected(case, "knowledge_authority_mismatch")

    def test_question_anchor_missing_rejected(self):
        case = self.case()
        case["question"] = "给我讲讲流程"
        self.assert_rejected(case, "question_condition_unbound")

    def test_uncovered_numeric_input_rejected(self):
        case = self.case()
        case["question"] += "那么12345.67元呢？"
        self.assert_rejected(case, "question_numeric_condition")

    def test_cross_document_cannot_count_scope_as_fact(self):
        case = self.case()
        case["scenario_type"] = "C"
        case["cross_document_reason"] = "只增加文档名并不构成必要证据"
        case["checker_spec"] = g.checker_spec(case)
        self.assert_rejected(case, "scenario_cardinality")

    def test_no_missing_information_mislabelling(self):
        case = self.case("RGV1-B07")
        case["answerability"] = "FULL"
        case["checker_spec"] = g.checker_spec(case)
        self.assert_rejected(case, "answerability_mismatch")

    def test_realtime_without_fixture_rejected(self):
        case = self.case()
        case["real_time_dependency"] = True
        self.assert_rejected(case, "const")

    def test_removed_scope_rejected(self):
        case = self.case("RGV1-D01")
        case["golden_evidence"] = [e for e in case["golden_evidence"] if e["evidence_id"] != "S2"]
        case["golden_answer"] = g.render_answer(case)
        self.assert_rejected(case, "scope_coverage")

    def test_checker_document_only_recall_not_accepted(self):
        case = self.case()
        case["checker_spec"]["top_k_necessary_evidence_recall"]["rule"] = "命中文档就计全事实召回"
        self.assert_rejected(case, "checker_contract_changed")

    def test_schema_missing_required_field(self):
        case = self.case()
        del case["golden_answer"]
        self.assert_rejected(case, "missing")

    def test_schema_unknown_nested_key_rejected(self):
        case = self.case()
        case["golden_evidence"][0]["chunk_id"] = "index-dependent"
        self.assert_rejected(case, "unexpected")

    def test_csv_preserves_nested_lists_and_multiline_chinese(self):
        cases = [self.case("RGV1-D01"), self.case("RGV1-B07")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.csv"
            path.write_text(g.csv_text(cases), encoding="utf-8-sig", newline="")
            self.assertTrue(g.validate_csv(cases, path))

    def test_utf8_crlf_unicode_span(self):
        doc = dict(id="sample", title="示例", path="sample.md", **{"class": "B"},
                   hash="fake", text="# 标题\r\n\r\n## 条款\r\n中文🙂条件。\r\n")
        doc["lines"] = doc["text"].splitlines(keepends=True)
        e = g.locate(doc, 4, 4)
        self.assertEqual(e["evidence_text"], "中文🙂条件。")
        self.assertEqual(doc["text"][e["source_span"]["char_start"]:e["source_span"]["char_end"]], e["evidence_text"])

    def test_formal_document_mutation_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "kb"
            shutil.copytree(g.KB, target)
            path = target / self.sources["KB-003"]["path"]
            path.write_bytes(path.read_bytes() + b"\nmutation")
            with self.assertRaisesRegex(ValueError, "哈希"):
                g.load_sources(target)

    def test_overview_covers_all_body_lines(self):
        self.assertEqual(g.overview_check(self.sources), [])

    def test_unanswerable_topic_found_in_corpus_is_excluded(self):
        bp = copy.deepcopy(self.by_id["RGV1-U01"])
        bp["unknown_topic_terms"] = ["审批"]
        case = g.build_case(bp, self.sources)
        result = g.validate_case(case, self.sources, bp)
        self.assertIn("unanswerable_topic_requires_reselection", result["errors"])


if __name__ == "__main__":
    unittest.main()
