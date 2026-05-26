"""Wiring guard tests for AnalyticsService — Wave 752.

Prevents W746-class regressions where a service module is instantiated in
BackendService.__init__ but the corresponding import is silently dropped
during a rebase (NameError on fresh process startup).

Verified handlers (6):
  handle_compare_periods
  handle_get_activity_calendar
  handle_get_sentiment_trends
  handle_get_keyword_cloud
  handle_get_analytics_dashboard
  handle_get_timeline_view
"""
from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_PY = os.path.join(HERE, '..', 'backend', 'service.py')

_HANDLER_NAMES = [
    'handle_compare_periods',
    'handle_get_activity_calendar',
    'handle_get_sentiment_trends',
    'handle_get_keyword_cloud',
    'handle_get_analytics_dashboard',
    'handle_get_timeline_view',
]


class TestAnalyticsServiceWiring(unittest.TestCase):
    """Confirm AnalyticsService is imported, instantiated, and all
    6 handlers are delegated in service.py."""

    def setUp(self) -> None:
        with open(SERVICE_PY, encoding='utf-8') as f:
            self.source = f.read()

    # ------------------------------------------------------------------
    # 1. Import guard
    # ------------------------------------------------------------------

    def test_module_imported(self) -> None:
        self.assertIn(
            'from backend.analytics_service import AnalyticsService',
            self.source,
            'AnalyticsService import missing from service.py',
        )

    # ------------------------------------------------------------------
    # 2. Instantiation guard
    # ------------------------------------------------------------------

    def test_svc_instantiated_in_init(self) -> None:
        pattern = re.compile(
            r'self\._analytics_svc\s*=\s*AnalyticsService\(',
        )
        self.assertRegex(
            self.source,
            pattern,
            'self._analytics_svc = AnalyticsService( not found in service.py',
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
                rf'\s*return\s+self\._analytics_svc\.{re.escape(handler_method)}\(',
                re.DOTALL,
            )
            direct_pattern = re.compile(
                rf'self\._analytics_svc\.{re.escape(handler_method)}',
            )
            found = (
                stub_pattern.search(self.source) is not None
                or direct_pattern.search(self.source) is not None
            )
            self.assertTrue(
                found,
                msg=(
                    f'{handler_method} is not wired to _analytics_svc '
                    f'(no delegation in {local_stub} nor direct dispatch)'
                ),
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
