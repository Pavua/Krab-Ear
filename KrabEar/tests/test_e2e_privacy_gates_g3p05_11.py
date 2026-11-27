"""G3-privacy-05 / 05-11: e2e-канарейка не принимает ошибку за «защищено»."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "e2e_privacy_gates.py"


def _load():
    spec = importlib.util.spec_from_file_location("e2e_privacy_gates_g3p05_11", _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class GateAnsweredTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_empty_gate_result_counts_as_answered(self):
        self.assertTrue(self.mod.gate_answered({"id": "x", "result": {}}))
        self.assertTrue(self.mod.gate_answered({"id": "x", "result": {"items": []}}))

    def test_error_reply_is_not_a_held_gate(self):
        err = {"id": "x", "error": {"code": "internal_error", "message": "boom"}}
        self.assertFalse(self.mod.gate_answered(err))
        # …хотя канарейки в нём нет — именно это раньше давало ложный PASS
        self.assertFalse(self.mod.response_contains_secret(err))

    def test_missing_or_null_result_is_not_answered(self):
        self.assertFalse(self.mod.gate_answered({"id": "x"}))
        self.assertFalse(self.mod.gate_answered({"id": "x", "result": None}))
        self.assertFalse(self.mod.gate_answered(None))

    def test_error_alongside_result_is_not_answered(self):
        self.assertFalse(self.mod.gate_answered({"result": {}, "error": {"code": "x"}}))


if __name__ == "__main__":
    unittest.main()
