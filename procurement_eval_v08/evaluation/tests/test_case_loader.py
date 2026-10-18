from pathlib import Path
import unittest

from evaluation.case_loader import CaseLoader, resolve_case_dir


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = resolve_case_dir()


class CaseLoaderTests(unittest.TestCase):
    def setUp(self):
        self.loader = CaseLoader(SUITE_DIR)

    def test_target_cases_link_scenario_and_fixture(self):
        for case_id in ("E01", "E02B", "A02_BC03"):
            with self.subTest(case_id=case_id):
                loaded = self.loader.load(case_id)
                self.assertEqual(loaded.case.case_id, case_id)
                self.assertEqual(loaded.fixture.case_id, case_id)
                messages = [
                    step["text"] for step in loaded.scenario.steps
                    if step["type"] == "user_message"
                ]
                self.assertEqual(messages, list(loaded.case.task))

    def test_a02_contains_cancel_between_two_messages(self):
        steps = self.loader.load("A02_BC03").scenario.steps
        self.assertEqual(
            [(step["type"], step.get("action")) for step in steps],
            [
                ("user_message", None),
                ("harness_action", "cancel_latest_confirmation"),
                ("user_message", None),
            ],
        )

    def test_formal_suite_counts_and_backend_links(self):
        self.assertEqual(len(self.loader.list_case_ids()), 33)
        self.assertEqual(len(self.loader.list_backend_case_ids()), 20)
        for case_id in self.loader.list_backend_case_ids():
            with self.subTest(case_id=case_id):
                loaded = self.loader.load_backend(case_id)
                self.assertEqual(loaded.scenario.suite, "backend")
                self.assertEqual(loaded.fixture.case_id, case_id)


if __name__ == "__main__":
    unittest.main()
