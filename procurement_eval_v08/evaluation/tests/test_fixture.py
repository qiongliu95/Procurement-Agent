from pathlib import Path
import tempfile
import unittest

from evaluation.case_loader import CaseLoader, resolve_case_dir
from evaluation.fixture import FixtureEnvironment


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUITE_DIR = resolve_case_dir()


class FixtureTests(unittest.TestCase):
    def test_each_case_gets_independent_seeded_database(self):
        loader = CaseLoader(SUITE_DIR)
        with tempfile.TemporaryDirectory() as temp:
            first = FixtureEnvironment(loader.load("E01"), Path(temp) / "e01.sqlite")
            second = FixtureEnvironment(loader.load("E02B"), Path(temp) / "e02b.sqlite")
            try:
                first.prepare()
                second.prepare()
                self.assertNotEqual(first.db_path, second.db_path)
                self.assertEqual(first.initial_state["counts"]["requests"], 0)
                self.assertEqual(second.initial_state["counts"]["requests"], 0)
                active = {
                    row["name"]: row["status"]
                    for row in first.initial_state["tables"]["suppliers"]
                }
                self.assertEqual(active["启明办公"], "active")
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
