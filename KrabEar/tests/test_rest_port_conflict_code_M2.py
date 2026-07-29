"""M2: код rest.port_conflict зарегистрирован (EADDRINUSE при in-process старте)."""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.error_codes import ERROR_REGISTRY  # noqa: E402


class RestPortConflictCodeTest(unittest.TestCase):
    def test_code_registered(self):
        self.assertIn("rest.port_conflict", ERROR_REGISTRY)

    def test_entry_has_required_fields(self):
        entry = ERROR_REGISTRY["rest.port_conflict"]
        self.assertIn("user_msg_ru", entry)
        self.assertTrue(entry["user_msg_ru"].strip())
        self.assertIn("actionable", entry)


if __name__ == "__main__":
    unittest.main()
