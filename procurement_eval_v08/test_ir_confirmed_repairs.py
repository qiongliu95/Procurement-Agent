"""IR 已确认误归因与依赖配置回归；原始历史记录只读，不调用外部模型。"""
import gc
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from agent_runtime import AgentRuntime, ModelToolCall, ModelTurn, TraceWriter
from backend import Store
from evaluation.case_loader import CaseLoader
from evaluation.unified_adapters import NativeAdapters
from evaluation.unified_reporting import summarize
import intent_routing_validation as ir
from test_context_management import CapturingModel, tool
from test_multiturn_dialogue import FakeKnowledgeIndex


class IrConfirmedRepairTests(unittest.TestCase):
    def test_missing_model_intent_is_missing_evidence_not_model_failure(self):
        self.check_missing_intent(route_matches=True)

    def test_missing_intent_does_not_hide_observed_route_failure(self):
        self.check_missing_intent(route_matches=False)

    def check_missing_intent(self, *, route_matches):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Store(root / "ir.sqlite")
            writer = TraceWriter(root / "traces")
            model = CapturingModel([ModelTurn(None, [tool("propose_task_state", {"intent": "create", "item": "电脑"})])])
            api = Mock()
            runtime = AgentRuntime(model, api, writer)
            real_write = writer.write

            def legacy_write(event):
                if event["event_type"] not in {"MODEL_TASK_PROPOSAL_RECEIVED", "TASK_PROPOSAL_VALIDATED"}:
                    real_write(event)

            case = next(c for c in ir.CASES if c["id"] == "IR07")
            case = {**case, "routes": [{"ASK"} if route_matches else {"STOP"}]}
            try:
                with patch.object(writer, "write", side_effect=legacy_write):
                    result = ir.evaluate_case(case, runtime, root / "traces" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl"),
                                              root / "ir.sqlite")
                self.assertIsNone(result["checks"]["intent"])
                self.assertEqual(result["missing_checks"], ["intent"])
                self.assertNotEqual(result["root_cause"], "MODEL_INTENT")
                self.assertEqual(result["status"], "EVIDENCE_MISSING" if route_matches else "FAIL")
                self.assertEqual(result["root_cause"], "EVIDENCE_MISSING" if route_matches else "RUNTIME_ROUTING")
                self.assertEqual(result["failed_checks"], [] if route_matches else ["route"])
                self.assertTrue(result["checks"]["confirmation_and_write_safety"])
                self.assertEqual(api.mock_calls, [])
            finally:
                gc.collect()

    def test_raw_intent_has_priority_over_runtime_accepted_intent(self):
        events = [
            {"event_type": "MODEL_TASK_PROPOSAL_RECEIVED", "proposal": {"intent": "consult"}},
            {"event_type": "TASK_INTENT_ACCEPTED", "accepted_intent": "create"},
            {"event_type": "TASK_PROPOSAL_VALIDATED", "proposal": {"intent": "create"}},
        ]
        self.assertEqual(ir.intent_from_events(events), "consult")

    def test_known_wrong_raw_intent_remains_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Store(root / "ir.sqlite")
            trace = TraceWriter(root / "traces")
            runtime = AgentRuntime(CapturingModel([
                ModelTurn(None, [tool("propose_task_state", {"intent": "ambiguous", "question": "请明确目标"})])
            ]), Mock(), trace)
            try:
                result = ir.evaluate_case(next(c for c in ir.CASES if c["id"] == "IR07"), runtime,
                                          root / "traces" / (datetime.now().strftime("%Y-%m-%d") + ".jsonl"), root / "ir.sqlite")
                self.assertIs(result["checks"]["intent"], False)
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(result["root_cause"], "MODEL_INTENT")
            finally:
                gc.collect()

    def test_missing_intent_is_excluded_from_decisive_intent_metric(self):
        state = {"records": {
            str(index): {"module": "ir", "final_result": status,
                         "score": {"original_ir_result": {"checks": {"intent": value}}}}
            for index, (value, status) in enumerate(((True, "PASS"), (False, "FAIL"), (None, "EVIDENCE_MISSING")))
        }}
        result = summarize(state)
        metric = result["modules"]["ir"]["module_specific_metrics"]["intent"]
        self.assertEqual((metric["numerator"], metric["denominator"]), (1, 2))
        self.assertEqual(result["pending_count"], 1)

    def test_unified_ir03_has_knowledge_dependency_and_current_tool_expectation(self):
        model = CapturingModel([
            ModelTurn(None, [tool("search_knowledge", {"query": "公司采购超过一万元需要什么审批？"})]),
            ModelTurn(None, [tool("propose_task_state", {"intent": "consult", "dialogue_relation": "new_task", "target_object": "knowledge"})]),
            ModelTurn("金额达到一万元由 M02 审批。", []),
        ])
        model.client = Mock()
        index = FakeKnowledgeIndex()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("evaluation.unified_adapters.default_real_model_factory", return_value=(model, {"model_name": "offline-ir"})), \
                    patch("evaluation.unified_adapters.build_default_index", return_value=(index, {})) as builder:
                result = NativeAdapters(CaseLoader(), root, root / "reports").execute("ir", "IR03")
            original = result["score"]["original_ir_result"]
            self.assertEqual(result["score"]["final_result"], "PASS")
            self.assertEqual(original["expected_required_tools"], ["search_knowledge"])
            self.assertIn("search_knowledge", original["actual_tool_calls"])
            self.assertTrue(all(original["checks"].values()))
            self.assertEqual(len(index.calls), 1)
            builder.assert_called_once()
            model.client.close.assert_called_once()

    def test_standalone_ir03_uses_same_knowledge_dependency(self):
        model = CapturingModel([
            ModelTurn(None, [tool("search_knowledge", {"query": "一万元审批"})]),
            ModelTurn(None, [tool("propose_task_state", {"intent": "consult", "target_object": "knowledge"})]),
            ModelTurn("金额达到一万元由 M02 审批。", []),
        ])
        index = FakeKnowledgeIndex()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                with patch.object(ir, "OUTPUT_ROOT", Path(temporary)), \
                        patch.object(sys, "argv", ["intent_routing_validation.py", "optimized", "--case", "IR03"]), \
                        patch.dict(os.environ, {"AGENT_MODEL_API_KEY": "offline", "AGENT_MODEL_BASE_URL": "http://unused", "AGENT_MODEL_NAME": "offline-ir"}), \
                        patch.object(ir.OpenAICompatibleModel, "from_env", return_value=model), \
                        patch.object(ir, "build_default_index", return_value=(index, {})) as builder, \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(ir.main(), 0)
                builder.assert_called_once()
                self.assertEqual(len(index.calls), 1)
            finally:
                gc.collect()

    def test_original_ir06_ir13_ir18_model_outputs_pass_without_changing_expectations(self):
        archive = Path(__file__).parent / "evaluation/runs/unified-20261014T144438-03a3c998/ir"
        for case_id in ("IR06", "IR13", "IR18"):
            with self.subTest(case=case_id), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder = next(archive.glob(case_id + "-*"))
                turns = []
                for path in sorted((folder / "model_calls").glob("*.json")):
                    raw = json.loads(path.read_text(encoding="utf-8"))["model_turn"]
                    turns.append(ModelTurn(raw["content"], [ModelToolCall(**call) for call in raw["tool_calls"]]))
                model = CapturingModel(turns)
                model.client = Mock()
                with patch("evaluation.unified_adapters.default_real_model_factory", return_value=(model, {"model_name": "archived-ir-replay"})), \
                        patch("evaluation.unified_adapters.build_default_index", return_value=(FakeKnowledgeIndex(), {})):
                    result = NativeAdapters(CaseLoader(), root, root / "reports").execute("ir", case_id)
                original = result["score"]["original_ir_result"]
                self.assertEqual(result["score"]["final_result"], "PASS", original)
                self.assertEqual(original["turns"][0]["actual_intent"], "update" if case_id == "IR18" else "query")
                self.assertEqual(original["write_tool_count"], 0)
                self.assertTrue(original["checks"]["confirmation_and_write_safety"])
