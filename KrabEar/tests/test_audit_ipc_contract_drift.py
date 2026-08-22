"""Regression tests for the static Swift-to-Python IPC contract auditor."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = PROJECT_ROOT / "scripts" / "audit_ipc_contract_drift.py"

_spec = importlib.util.spec_from_file_location("ipc_contract_drift_audit", AUDIT_PATH)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = audit
_spec.loader.exec_module(audit)


class DispatchTableExtractionTest(unittest.TestCase):
    def test_reads_only_backend_service_returned_table_including_lambda_handler(self):
        """Non-dispatch dicts must not become IPC methods; lambda entries must."""
        fixture = textwrap.dedent(
            """
            class BackendService:
                def _build_dispatch_table(self):
                    instrumentation = {
                        "not_a_method": self._record,
                    }
                    return {
                        "bound_handler": self._handle_bound,
                        "lambda_handler": lambda params: self._handle_lambda(params),
                    }

            class OtherService:
                def _build_dispatch_table(self):
                    return {
                        "also_not_a_method": self._handle_other,
                    }
            """
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            service_py = Path(tmpdir) / "service.py"
            service_py.write_text(fixture, encoding="utf-8")

            methods = audit._enumerate_python_dispatch_methods(service_py)

        self.assertEqual(set(methods), {"bound_handler", "lambda_handler"})
