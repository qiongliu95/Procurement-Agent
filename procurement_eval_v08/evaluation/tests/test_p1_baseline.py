"""P1可信度回归；仅生成临时 Evidence，正式 Case、历史证据及业务数据均只读。"""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import evaluation.tests.test_runner as runner_helpers
from evaluation.__main__ import run_main
from evaluation.batch import batch_main
from evaluation.case_loader import CaseLoader, CaseLoadError, resolve_case_dir
from evaluation.checker.cli import score_main
from evaluation.checker.scorer import OfflineScorer
from evaluation.metrics import calculate_metrics
from evaluation.evidence import EvidenceCollector
from evaluation.runner import EvaluationRunner


class TraceAssociationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.loader = CaseLoader()
        cls.scorer = OfflineScorer(cls.loader)
        runner = EvaluationRunner(cls.loader, cls.root / "runs", model_factory=runner_helpers.scripted_model_factory)
        cls.first = runner.run_case("E01")
        cls.other_run = runner.run_case("E01")
        cls.multi = runner.run_case("E02B")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def score(self, evidence, **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            return self.scorer.score_file(path, **kwargs)

    def assert_invalid(self, evidence, **kwargs):
        score = self.score(evidence, **kwargs)
        self.assertEqual(score["final_result"], "INVALID_EVIDENCE", score["failure_reasons"])
        self.assertFalse(score["strict_pass"])
        self.assertEqual(score["criteria"], [])
        self.assertEqual(score["hard_violations"], [])
        return score

    def test_valid_single_and_multi_turn_trace_pass(self):
        for evidence in (self.first, self.multi):
            with self.subTest(case=evidence["case"]["case_id"]):
                self.assertEqual(self.score(evidence)["final_result"], "PASS")
                identity = evidence["trace"]["context"]
                self.assertEqual(identity["case_id"], evidence["case"]["case_id"])
                self.assertEqual(identity["run_id"], evidence["run"]["run_id"])
                self.assertEqual(identity["session_id"], evidence["run"]["session_id"])
                self.assertTrue(all(event["evaluation_context"] == identity for event in evidence["trace"]["events"]))

    def test_replacing_trace_with_other_actual_run_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["trace"] = copy.deepcopy(self.other_run["trace"])
        self.assert_invalid(evidence)

    def test_foreign_runtime_run_id_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["trace"]["events"][0]["run_id"] = "run-foreign"
        self.assert_invalid(evidence)

    def test_incomplete_status_cannot_bypass_run_binding(self):
        evidence = copy.deepcopy(self.first)
        evidence["run"]["status"] = "EVIDENCE_INCOMPLETE"
        evidence["trace"]["events"][0]["run_id"] = "run-foreign"
        self.assert_invalid(evidence)

    def test_collector_does_not_overwrite_foreign_existing_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "E01-collector-test"
            collector = EvidenceCollector(directory, directory.name, self.loader.load("E01"), {}, {})
            event = copy.deepcopy(self.first["trace"]["events"][0])
            event["evaluation_context"]["case_id"] = "E02B"
            (directory / "traces" / "test.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            evidence = collector.finish(status="EVIDENCE_INCOMPLETE", status_reason=None,
                                        session_id=f"eval-{directory.name}",
                                        initial_state=self.first["business_state"]["before"],
                                        final_state=self.first["business_state"]["after"])
            self.assertEqual(evidence["trace"]["events"][0]["evaluation_context"]["case_id"], "E02B")
            self.assert_invalid(evidence)

    def test_foreign_session_id_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["trace"]["events"][0]["session_id"] = "foreign-session"
        self.assert_invalid(evidence)

    def test_foreign_case_in_event_binding_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["trace"]["events"][0]["evaluation_context"]["case_id"] = "E02B"
        self.assert_invalid(evidence)

    def test_foreign_case_in_legacy_event_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["trace"]["events"][0]["case_id"] = "E02B"
        self.assert_invalid(evidence)

    def test_foreign_evaluation_run_binding_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["trace"]["events"][0]["evaluation_context"]["run_id"] = "E01-other"
        self.assert_invalid(evidence)

    def test_foreign_step_session_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["steps"][0]["runtime_state_after"]["session_id"] = "foreign-session"
        self.assert_invalid(evidence)

    def test_foreign_step_trace_reference_is_rejected(self):
        evidence = copy.deepcopy(self.first)
        evidence["steps"][0]["agent_output"]["trace_id"] = "run-foreign"
        self.assert_invalid(evidence)

    def test_case_fixture_or_scenario_mismatch_is_rejected(self):
        for field in ("scenario", "fixture_spec"):
            with self.subTest(field=field):
                evidence = copy.deepcopy(self.first)
                evidence[field]["case_id"] = "E02B"
                self.assert_invalid(evidence)

    def test_expected_context_mismatch_is_rejected(self):
        for field, value in (("case_id", "E02B"), ("run_id", "other-run"), ("session_id", "other-session")):
            with self.subTest(field=field):
                self.assert_invalid(self.first, expected_context={field: value})

    def test_score_cli_requested_run_rejects_wrong_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / "E01-wrong-folder"
            directory.mkdir()
            (directory / "evidence.json").write_text(json.dumps(self.first), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(score_main(["--run-id", directory.name, "--no-persist"], None, root), 0)
            self.assertEqual(json.loads(output.getvalue())["scores"][0]["final_result"], "INVALID_EVIDENCE")
            self.assertFalse((directory / "scored.jsonl").exists())

    def test_manifest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps(self.first), encoding="utf-8")
            manifest = {"case_id": "E02B", **self.first["run"]}
            (path.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(self.scorer.score_file(path)["final_result"], "INVALID_EVIDENCE")

    def test_valid_legacy_trace_remains_readable_without_rewriting(self):
        evidence = copy.deepcopy(self.first)
        del evidence["trace"]["context"]
        for event in evidence["trace"]["events"]:
            del event["evaluation_context"]
        before = copy.deepcopy(evidence)
        self.assertEqual(self.score(evidence)["final_result"], "PASS")
        self.assertEqual(evidence, before)

    def test_invalid_evidence_never_becomes_agent_failure_metrics(self):
        evidence = copy.deepcopy(self.first)
        evidence["trace"]["events"][0]["run_id"] = "run-foreign"
        metrics = calculate_metrics([self.assert_invalid(evidence)])
        self.assertEqual(metrics["agent"]["status_counts"], {"INVALID_EVIDENCE": 1})
        self.assertEqual(metrics["agent"]["task_success_rate"]["denominator"], 0)
        self.assertEqual(metrics["agent"]["case_coverage"]["numerator"], 0)


def score(case_id, run_id, result="PASS", suite="agent", **extra):
    return {"case_id": case_id, "run_id": run_id, "final_result": result,
            "suite": suite, "category": "N", "criteria": [], **extra}


class MetricsCoverageTests(unittest.TestCase):
    def test_34_runs_of_33_cases_cover_only_33_cases(self):
        scores = [score(f"CASE-{index}", f"run-{index}") for index in range(33)]
        scores.append(score("CASE-0", "retry-0"))
        metrics = calculate_metrics(scores)["agent"]
        self.assertEqual(metrics["evaluation_coverage"], {"numerator": 33, "denominator": 33, "value": 1.0})
        self.assertEqual(metrics["total_runs"], 34)
        self.assertEqual(metrics["retry_count"], 1)
        self.assertEqual(metrics["task_success_rate"]["numerator"], 33)

    def test_latest_failed_retry_is_not_hidden_by_prior_pass(self):
        metrics = calculate_metrics([score("E01", "first"), score("E01", "retry", "FAIL")])["agent"]
        self.assertEqual(metrics["executed_case_count"], 1)
        self.assertEqual(metrics["task_success_rate"]["value"], 0)
        self.assertEqual(metrics["status_counts"], {"FAIL": 1})
        self.assertLessEqual(metrics["evaluation_coverage"]["value"], 1)

    def test_old_run_rescore_does_not_replace_new_execution(self):
        old = score("E01", "first", run_started_at="2026-10-14T01:00:00Z")
        new = score("E01", "retry", "FAIL", run_started_at="2026-10-14T02:00:00Z")
        metrics = calculate_metrics([new, old, {**old, "score_id": "rescore"}])["agent"]
        self.assertEqual(metrics["total_runs"], 2)
        self.assertEqual(metrics["retry_count"], 1)
        self.assertEqual(metrics["status_counts"], {"FAIL": 1})

    def test_success_rate_counts_executed_cases_including_review(self):
        metrics = calculate_metrics([score("E01", "first"), score("E02B", "second", "REVIEW_REQUIRED")])["agent"]
        self.assertEqual(metrics["task_success_rate"], {"numerator": 1, "denominator": 2, "value": 0.5})

    def test_backend_duplicate_runs_do_not_inflate_coverage(self):
        metrics = calculate_metrics([score("C07", "first", suite="backend"), score("C07", "retry", suite="backend")])["backend"]
        self.assertEqual(metrics["evaluation_coverage"]["numerator"], 1)
        self.assertEqual(metrics["pass_rate"]["denominator"], 1)
        self.assertEqual(metrics["total_runs"], 2)
        self.assertEqual(metrics["retry_count"], 1)

    def test_same_run_rescore_is_not_a_retry(self):
        metrics = calculate_metrics([score("E01", "first", "FAIL"), score("E01", "first")])["agent"]
        self.assertEqual(metrics["total_runs"], 1)
        self.assertEqual(metrics["retry_count"], 0)
        self.assertEqual(metrics["task_success_rate"]["value"], 1)

    def test_invalid_retry_does_not_erase_prior_case_coverage(self):
        metrics = calculate_metrics([score("E01", "first"), score("E01", "retry", "INVALID_EVIDENCE")])["agent"]
        self.assertEqual(metrics["case_coverage"]["numerator"], 1)
        self.assertEqual(metrics["total_runs"], 2)
        self.assertEqual(metrics["retry_count"], 1)
        self.assertEqual(metrics["task_success_rate"]["numerator"], 0)
        self.assertNotIn("FAIL", metrics["status_counts"])


class CasePathTests(unittest.TestCase):
    def test_default_cli_lists_all_cases(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"PROCUREMENT_EVAL_CASE_DIR": ""}), redirect_stdout(output):
            self.assertEqual(run_main(["--list"]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["case_ids"]), 33)

    def test_explicit_case_dir_and_legacy_alias_start(self):
        for option in ("--case-dir", "--suite-dir"):
            with self.subTest(option=option), redirect_stdout(io.StringIO()):
                with patch.dict(os.environ, {"PROCUREMENT_EVAL_CASE_DIR": "missing-config"}):
                    self.assertEqual(run_main([option, str(runner_helpers.SUITE_DIR), "--list"]), 0)

    def test_config_path_and_explicit_precedence(self):
        with patch.dict(os.environ, {"PROCUREMENT_EVAL_CASE_DIR": str(runner_helpers.SUITE_DIR)}):
            self.assertEqual(CaseLoader().suite_dir, runner_helpers.SUITE_DIR)
            self.assertEqual(resolve_case_dir("explicit-dir"), Path("explicit-dir").resolve())

    def test_current_and_legacy_layout_fallback(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"PROCUREMENT_EVAL_CASE_DIR": ""}):
            project = Path(temp) / "app"
            legacy = project.parent / "procurement_eval_v4"
            legacy.mkdir()
            (legacy / "agent_cases.json").write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_case_dir(project_root=project), legacy)
            current = project.parent / "评估case" / "procurement_eval_v4"
            current.mkdir(parents=True)
            (current / "agent_cases.json").write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_case_dir(project_root=project), current)

    def test_invalid_explicit_path_is_not_silently_replaced(self):
        with self.assertRaises(CaseLoadError):
            CaseLoader("does-not-exist-test-suite")

    def test_default_run_executes_in_temporary_database(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()), \
                patch("evaluation.__main__.EvaluationRunner", side_effect=lambda loader, runs: EvaluationRunner(
                    loader, runs, model_factory=runner_helpers.scripted_model_factory)):
            result = run_main(["--case", "E01", "--runs-dir", temp, "--no-env-file"])
            self.assertEqual(result, 0)
            evidence = json.loads(next(Path(temp).glob("*/evidence.json")).read_text(encoding="utf-8"))
            self.assertTrue(evidence["evidence_completeness"]["complete"])

    def test_batch_and_score_accept_case_dir(self):
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(io.StringIO()):
            root = Path(temp)
            with patch("evaluation.batch.BatchEvaluator.execute", return_value={}) as execute:
                self.assertEqual(batch_main(["--case-dir", str(runner_helpers.SUITE_DIR), "--suite", "backend"], None, root), 0)
                execute.assert_called_once()
            runner = EvaluationRunner(CaseLoader(), root, model_factory=runner_helpers.scripted_model_factory)
            evidence = runner.run_case("E01")
            path = root / evidence["run"]["run_id"] / "evidence.json"
            self.assertEqual(score_main(["--case-dir", str(runner_helpers.SUITE_DIR), "--evidence", str(path), "--no-persist"], None, root), 0)
