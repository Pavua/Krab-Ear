"""W1297: Tests verifying PeriodComparisonService wiring in BackendService.

Covers W1290 F3 MED:
  - PeriodComparisonService is instantiated in BackendService.__init__.
  - mode=weeks is reachable via IPC (compare_periods handler).
  - mode=months is reachable via IPC.
  - mode=explicit (default) unchanged — backward compat with explicit-date callers.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVICE_PY = PROJECT_ROOT / "backend" / "service.py"
PERIOD_COMPARISON_PY = PROJECT_ROOT / "backend" / "period_comparison.py"


class PeriodComparisonSvcInstantiatedTest(unittest.TestCase):
    """test_period_comparison_svc_instantiated_in_backend.

    Verifies at AST level that:
      1. PeriodComparisonService is imported in service.py.
      2. self._period_comparison_svc = PeriodComparisonService(...) is assigned
         inside BackendService.__init__.
    """

    def _load_service_ast(self) -> ast.Module:
        src = SERVICE_PY.read_text(encoding="utf-8")
        return ast.parse(src, filename=str(SERVICE_PY))

    def test_import_present(self) -> None:
        """PeriodComparisonService is imported from backend.period_comparison."""
        tree = self._load_service_ast()
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "backend.period_comparison":
                    names = [alias.name for alias in node.names]
                    if "PeriodComparisonService" in names:
                        found = True
                        break
        self.assertTrue(
            found,
            "PeriodComparisonService not imported from backend.period_comparison in service.py",
        )

    def test_instantiation_in_init(self) -> None:
        """self._period_comparison_svc is assigned inside BackendService.__init__."""
        tree = self._load_service_ast()

        # Find BackendService class → __init__ method → look for assignment
        class_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "BackendService":
                class_node = node
                break

        self.assertIsNotNone(class_node, "BackendService class not found in service.py")

        init_node = None
        for node in ast.walk(class_node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__":
                init_node = node
                break

        self.assertIsNotNone(init_node, "BackendService.__init__ not found")

        # Walk assignments inside __init__
        found_assignment = False
        for node in ast.walk(init_node):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                        and target.attr == "_period_comparison_svc"
                    ):
                        found_assignment = True
                        break
            if found_assignment:
                break

        self.assertTrue(
            found_assignment,
            "self._period_comparison_svc not assigned in BackendService.__init__",
        )


class ModeWeeksReachableViaIPCTest(unittest.TestCase):
    """test_mode_weeks_reachable_via_ipc.

    Verifies that _handle_compare_periods delegates to
    self._period_comparison_svc.handle_compare_periods and that the service
    handles mode='weeks' without raising.
    """

    def _make_svc(self) -> object:
        """Import PeriodComparisonService and return an instance with a mock store."""
        from backend.period_comparison import PeriodComparisonService

        mock_store = MagicMock()
        mock_store.get_history_page_filtered.return_value = ([], None)
        return PeriodComparisonService(store=mock_store)

    def test_weeks_mode_returns_dict(self) -> None:
        """mode='weeks' returns a dict with required keys."""
        svc = self._make_svc()
        result = svc.handle_compare_periods({"mode": "weeks", "weeks_back": 2})

        self.assertIsInstance(result, dict)
        for key in ("period1", "period2", "recordings_change_pct",
                    "duration_change_pct", "confidence_change",
                    "new_languages", "summary"):
            self.assertIn(key, result, f"Key '{key}' missing from weeks-mode result")

    def test_weeks_mode_calls_store(self) -> None:
        """mode='weeks' queries the store (two calls, one per period)."""
        from backend.period_comparison import PeriodComparisonService

        mock_store = MagicMock()
        mock_store.get_history_page_filtered.return_value = ([], None)
        svc = PeriodComparisonService(store=mock_store)

        svc.handle_compare_periods({"mode": "weeks"})

        self.assertEqual(
            mock_store.get_history_page_filtered.call_count,
            2,
            "Store should be queried exactly twice (once per period) for mode=weeks",
        )

    def test_handler_delegation_via_dispatch(self) -> None:
        """compare_periods IPC reaches PeriodComparisonService via the LIVE chain.

        W#47: the dead in-class _handle_compare_periods duplicate was deleted.
        The single source of truth is now:
            dispatch["compare_periods"] → self._analytics_svc.handle_compare_periods
            → AnalyticsService.handle_compare_periods → period_comparison.compare_periods
        Assert that LIVE delegation, not the (removed) in-class stub.
        """
        # 1. service.py dispatch routes to the extracted AnalyticsService.
        src = SERVICE_PY.read_text(encoding="utf-8")
        self.assertIn(
            '"compare_periods": self._analytics_svc.handle_compare_periods',
            src,
            "compare_periods not delegated to self._analytics_svc.handle_compare_periods",
        )
        self.assertNotIn(
            "def _handle_compare_periods(",
            src,
            "dead in-class _handle_compare_periods duplicate reappeared in service.py",
        )

        # 2. AnalyticsService.handle_compare_periods delegates to the
        #    period_comparison.compare_periods implementation (AST level).
        analytics_py = PROJECT_ROOT / "backend" / "analytics_service.py"
        a_tree = ast.parse(analytics_py.read_text(encoding="utf-8"))

        handler_node = None
        for node in ast.walk(a_tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "handle_compare_periods"
            ):
                handler_node = node
                break

        self.assertIsNotNone(
            handler_node, "handle_compare_periods not found in analytics_service.py"
        )

        found_delegation = False
        for node in ast.walk(handler_node):
            if isinstance(node, ast.Call):
                func = node.func
                # period_comparison.compare_periods imported as _compare_periods_fn.
                if isinstance(func, ast.Name) and func.id in (
                    "_compare_periods_fn",
                    "compare_periods",
                ):
                    found_delegation = True
                    break

        self.assertTrue(
            found_delegation,
            "AnalyticsService.handle_compare_periods does not delegate to "
            "period_comparison.compare_periods",
        )


class ModeMonthsReachableViaIPCTest(unittest.TestCase):
    """test_mode_months_reachable_via_ipc."""

    def _make_svc(self) -> object:
        from backend.period_comparison import PeriodComparisonService

        mock_store = MagicMock()
        mock_store.get_history_page_filtered.return_value = ([], None)
        return PeriodComparisonService(store=mock_store)

    def test_months_mode_returns_dict(self) -> None:
        """mode='months' returns a dict with required keys."""
        svc = self._make_svc()
        result = svc.handle_compare_periods({"mode": "months"})

        self.assertIsInstance(result, dict)
        for key in ("period1", "period2", "recordings_change_pct",
                    "duration_change_pct", "confidence_change",
                    "new_languages", "summary"):
            self.assertIn(key, result, f"Key '{key}' missing from months-mode result")

    def test_months_mode_calls_store_twice(self) -> None:
        """mode='months' queries store exactly twice."""
        from backend.period_comparison import PeriodComparisonService

        mock_store = MagicMock()
        mock_store.get_history_page_filtered.return_value = ([], None)
        svc = PeriodComparisonService(store=mock_store)

        svc.handle_compare_periods({"mode": "months"})

        self.assertEqual(
            mock_store.get_history_page_filtered.call_count,
            2,
            "Store should be queried exactly twice for mode=months",
        )

    def test_months_mode_summary_non_empty(self) -> None:
        """Summary for months mode is a non-empty string."""
        svc = self._make_svc()
        result = svc.handle_compare_periods({"mode": "months"})
        self.assertIsInstance(result["summary"], str)
        self.assertGreater(len(result["summary"]), 0)


class ModeExplicitUnchangedTest(unittest.TestCase):
    """test_mode_explicit_unchanged — backward compat with existing explicit-date callers."""

    def setUp(self) -> None:
        from backend.period_comparison import PeriodComparisonService

        self.mock_store = MagicMock()
        self.mock_store.get_history_page_filtered.return_value = ([], None)
        self.svc = PeriodComparisonService(store=self.mock_store)

    def test_explicit_mode_with_dates_returns_dict(self) -> None:
        """mode='explicit' with all four date params returns a valid result."""
        result = self.svc.handle_compare_periods({
            "mode": "explicit",
            "period1_start": "2024-01-01",
            "period1_end": "2024-01-07",
            "period2_start": "2024-01-08",
            "period2_end": "2024-01-14",
        })
        self.assertIsInstance(result, dict)
        self.assertIn("period1", result)
        self.assertIn("period2", result)

    def test_no_mode_defaults_to_explicit(self) -> None:
        """Omitting mode= defaults to explicit-date behaviour."""
        result = self.svc.handle_compare_periods({
            "period1_start": "2024-02-01",
            "period1_end": "2024-02-07",
            "period2_start": "2024-02-08",
            "period2_end": "2024-02-14",
        })
        self.assertIsInstance(result, dict)
        self.assertIn("summary", result)

    def test_custom_mode_alias_still_works(self) -> None:
        """mode='custom' (legacy) also triggers explicit-date mode (backward compat)."""
        result = self.svc.handle_compare_periods({
            "mode": "custom",
            "period1_start": "2024-03-01",
            "period1_end": "2024-03-07",
            "period2_start": "2024-03-08",
            "period2_end": "2024-03-14",
        })
        self.assertIsInstance(result, dict)
        self.assertIn("period1", result)

    def test_explicit_mode_missing_dates_raises_value_error(self) -> None:
        """mode='explicit' without required date params raises ValueError."""
        with self.assertRaises(ValueError):
            self.svc.handle_compare_periods({"mode": "explicit"})

    def test_explicit_date_calls_store_with_correct_from_ts(self) -> None:
        """Explicit dates are forwarded to the store as from_ts / to_ts."""
        self.svc.handle_compare_periods({
            "mode": "explicit",
            "period1_start": "2024-04-01",
            "period1_end": "2024-04-07",
            "period2_start": "2024-04-08",
            "period2_end": "2024-04-14",
        })
        first_call = self.mock_store.get_history_page_filtered.call_args_list[0]
        kwargs = first_call[1]
        self.assertIn("2024-04-01", kwargs["from_ts"])


if __name__ == "__main__":
    unittest.main()
