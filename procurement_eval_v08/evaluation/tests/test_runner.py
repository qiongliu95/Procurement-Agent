from pathlib import Path
import tempfile
import unittest

from agent_runtime import ModelToolCall, ModelTurn
from evaluation.case_loader import CaseLoader, resolve_case_dir
from evaluation.fixture import FixtureNotReady
from evaluation.runner import EvaluationRunner


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = resolve_case_dir()


def tool(name, arguments, index):
    return ModelToolCall(f"eval-model-call-{index}", name, arguments)


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)

    def complete(self, _messages, _tools):
        if not self.turns:
            raise AssertionError("Scripted model exhausted")
        return self.turns.pop(0)


def scripted_model_factory(loaded):
    case_id = loaded.case.case_id
    if case_id == "E01":
        turns = [ModelTurn(None, [tool("propose_task_state", {
            "intent": "create",
            "question": "请补充物品、数量、单价、供应商和用途。",
        }, 1)])]
    elif case_id == "E02B":
        turns = [
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create", "question": "请补充物品、数量、单价、供应商和用途。",
            }, 1)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create", "question": "请继续补充物品、数量、单价、供应商和用途。",
            }, 2)]),
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"}, 3)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create",
                "item": "无线鼠标",
                "quantity": 5,
                "unit_price": "80.00",
                "supplier_name": "启明办公",
                "purpose": "新员工日常办公使用",
            }, 4)]),
        ]
    elif case_id == "A02_BC03":
        turns = [
            ModelTurn(None, [tool("match_supplier", {"name": "启明办公"}, 1)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create",
                "item": "电脑",
                "quantity": 2,
                "unit_price": "3500.00",
                "supplier_name": "启明办公",
                "purpose": "新员工办公",
            }, 2)]),
            ModelTurn(None, [tool("propose_task_state", {
                "intent": "create",
                "item": "办公电脑",
                "quantity": 2,
                "question": "请补充单价、供应商和用途。",
            }, 3)]),
        ]
    else:
        raise AssertionError(case_id)
    return ScriptedModel(turns), {
        "adapter": "ScriptedModel",
        "model_name": "scripted-evaluation-test",
        "real_model": False,
    }


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.loader = CaseLoader(SUITE_DIR)
        self.runner = EvaluationRunner(
            self.loader,
            Path(self.temp.name),
            model_factory=scripted_model_factory,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_e01_records_single_turn_and_no_write(self):
        evidence = self.runner.run_case("E01")
        self.assertEqual(evidence["run"]["status"], "COMPLETED_AWAITING_SCORING")
        self.assertEqual(evidence["steps"][0]["agent_output"]["route_action"], "ASK")
        self.assertEqual(evidence["business_state"]["after"]["counts"]["requests"], 0)
        self.assertTrue(evidence["evidence_completeness"]["complete"])

    def test_e02b_keeps_one_session_and_reaches_confirmation(self):
        evidence = self.runner.run_case("E02B")
        self.assertEqual(evidence["run"]["status"], "COMPLETED_AWAITING_SCORING")
        session_ids = {
            step["runtime_state_after"]["session_id"] for step in evidence["steps"]
        }
        self.assertEqual(len(session_ids), 1)
        final = evidence["steps"][-1]
        self.assertEqual(final["agent_output"]["route_action"], "CONFIRM")
        self.assertIn("金额 ¥400.00", final["agent_output"]["message"])
        self.assertEqual(evidence["business_state"]["after"]["counts"]["requests"], 0)

    def test_a02_cancel_then_new_task_does_not_inherit_fields(self):
        evidence = self.runner.run_case("A02_BC03")
        self.assertEqual(evidence["run"]["status"], "COMPLETED_AWAITING_SCORING")
        cancel = evidence["steps"][1]
        self.assertEqual(cancel["action_status"], "EXECUTED")
        self.assertEqual(cancel["agent_output"]["route_reason_code"], "USER_REJECTED")
        second_task = evidence["steps"][2]
        state = second_task["runtime_state_after"]
        self.assertEqual(state["slots"], {"item": "办公电脑", "quantity": 2})
        self.assertEqual(set(state["missing_slots"]), {"unit_price", "supplier_name", "purpose"})
        self.assertIsNone(second_task["agent_output"]["confirmation"])
        self.assertEqual(evidence["business_state"]["after"]["counts"]["requests"], 0)

    def test_each_run_has_independent_ids_and_databases(self):
        first = self.runner.run_case("E01")
        second = self.runner.run_case("E01")
        self.assertNotEqual(first["run"]["run_id"], second["run"]["run_id"])
        self.assertNotEqual(
            first["business_state"]["after"]["database_path"],
            second["business_state"]["after"]["database_path"],
        )

    def test_missing_environment_is_not_reported_as_agent_failure(self):
        def unavailable(_loaded):
            raise FixtureNotReady("测试模型未配置")

        runner = EvaluationRunner(self.loader, Path(self.temp.name), model_factory=unavailable)
        evidence = runner.run_case("E01")
        self.assertEqual(evidence["run"]["status"], "ENVIRONMENT_NOT_READY")
        self.assertIn("测试模型未配置", evidence["run"]["status_reason"])

    def test_execution_exception_is_recorded(self):
        class BrokenModel:
            def complete(self, _messages, _tools):
                raise RuntimeError("synthetic execution error")

        runner = EvaluationRunner(
            self.loader,
            Path(self.temp.name),
            model_factory=lambda _loaded: (BrokenModel(), {
                "adapter": "BrokenModel", "model_name": "broken", "real_model": False,
            }),
        )
        evidence = runner.run_case("E01")
        self.assertEqual(evidence["run"]["status"], "RUN_ERROR")
        self.assertEqual(evidence["exceptions"][0]["phase"], "execution")


if __name__ == "__main__":
    unittest.main()
