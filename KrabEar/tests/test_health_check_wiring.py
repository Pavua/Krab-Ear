"""Wiring guard tests for HealthCheckService — Wave 755.

Prevents W746-class regressions where a service module is instantiated in
BackendService.__init__ but the corresponding import is silently dropped
during a rebase (NameError on fresh process startup).

Verified handlers (6):
  handle_ping
  handle_get_diagnostics
  handle_health_check
  handle_probe_llm_http
  handle_get_startup_diagnostics
  handle_check_integrity
"""
from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_PY = os.path.join(HERE, '..', 'backend', 'service.py')

_HANDLER_NAMES = [
    'handle_ping',
    'handle_get_diagnostics',
    'handle_health_check',
    'handle_probe_llm_http',
    'handle_get_startup_diagnostics',
    'handle_check_integrity',
]


class TestHealthCheckWiring(unittest.TestCase):
    """Confirm HealthCheckService is imported, instantiated, and all
    6 handlers are delegated in service.py."""

    def setUp(self) -> None:
        with open(SERVICE_PY, encoding='utf-8') as f:
            self.source = f.read()

    # ------------------------------------------------------------------
    # 1. Import guard
    # ------------------------------------------------------------------

    def test_module_imported(self) -> None:
        self.assertIn(
            'from backend.health_check_service import HealthCheckService',
            self.source,
            'HealthCheckService import missing from service.py',
        )

    # ------------------------------------------------------------------
    # 2. Instantiation guard
    # ------------------------------------------------------------------

    def test_svc_instantiated_in_init(self) -> None:
        pattern = re.compile(
            r'self\._health_check_svc\s*=\s*HealthCheckService\(',
        )
        self.assertRegex(
            self.source,
            pattern,
            'self._health_check_svc = HealthCheckService( not found in service.py',
        )

    # ------------------------------------------------------------------
    # 3. Handler delegation guards
    # ------------------------------------------------------------------

    def test_handlers_delegate_to_svc(self) -> None:
        for handler_method in _HANDLER_NAMES:
            local_stub = '_handle_' + handler_method.removeprefix('handle_')
            stub_pattern = re.compile(
                rf'def\s+{re.escape(local_stub)}\s*\([^)]*\)[^:]*:\s*\n'
                rf'(?:\s*"""[^"]*"""\s*\n)?'
                rf'\s*return\s+self\._health_check_svc\.{re.escape(handler_method)}\(',
                re.DOTALL,
            )
            direct_pattern = re.compile(
                rf'self\._health_check_svc\.{re.escape(handler_method)}',
            )
            found = (
                stub_pattern.search(self.source) is not None
                or direct_pattern.search(self.source) is not None
            )
            self.assertTrue(
                found,
                msg=(
                    f'{handler_method} is not wired to _health_check_svc '
                    f'(no delegation in {local_stub} nor direct dispatch)'
                ),
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
