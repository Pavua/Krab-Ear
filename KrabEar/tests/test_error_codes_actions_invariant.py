"""Wave 164: Invariant tests between error_codes.py and error_actions.py.

Ensures the two modules stay in sync:
- Every action_id referenced in ERROR_REGISTRY has a handler in ACTION_HANDLERS.
- No orphan handlers in ACTION_HANDLERS (handler with no referencing code).
- action_id format validation (dot-separated lowercase snake_case components).
- Required _Entry keys are present in every ERROR_REGISTRY entry.
- dedupe_seconds is a positive integer.
- severity is in the allowed Literal set.
"""
from __future__ import annotations

import re
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.error_codes import ERROR_REGISTRY  # noqa: E402
from backend.error_actions import ACTION_HANDLERS  # noqa: E402

# All severity levels defined in error_bus.py Severity Literal
_VALID_SEVERITIES = frozenset({"info", "warn", "error", "critical"})

# action_id format: lowercase letters, digits, underscores only (snake_case)
_ACTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# Required keys in every _Entry
_REQUIRED_ENTRY_KEYS = frozenset({
    "user_msg_ru",
    "actionable",
    "action_id",
    "action_label",
    "severity",
    "dedupe_seconds",
})


class EveryActionIdHasHandlerTest(unittest.TestCase):

    def test_every_action_id_in_codes_has_handler(self):
        """Every non-None action_id referenced in ERROR_REGISTRY must exist
        in ACTION_HANDLERS. A missing handler means clicking the toast button
        silently fails with 'unknown action_id'."""
        referenced = {
            entry["action_id"]
            for entry in ERROR_REGISTRY.values()
            if entry.get("action_id")
        }
        registered = set(ACTION_HANDLERS.keys())
        missing = referenced - registered
        self.assertSetEqual(
            missing,
            set(),
            f"action_ids in ERROR_REGISTRY but missing from ACTION_HANDLERS "
            f"({len(missing)} missing): {sorted(missing)}\n"
            f"Fix: add the missing handlers to backend/error_actions.py",
        )

    def test_every_actionable_entry_has_non_null_action_id(self):
        """Every entry with actionable=True must have a non-empty action_id."""
        bad = [
            code
            for code, entry in ERROR_REGISTRY.items()
            if entry.get("actionable") and not entry.get("action_id")
        ]
        self.assertEqual(
            bad, [],
            f"Actionable entries missing action_id: {bad}",
        )

    def test_every_actionable_entry_has_non_empty_action_label(self):
        """Every entry with actionable=True must have a non-empty action_label
        (used as the toast button label in the UI)."""
        bad = [
            code
            for code, entry in ERROR_REGISTRY.items()
            if entry.get("actionable") and not entry.get("action_label")
        ]
        self.assertEqual(
            bad, [],
            f"Actionable entries missing action_label: {bad}",
        )


class NoOrphanHandlerTest(unittest.TestCase):

    def test_no_orphan_action_handler(self):
        """Every handler in ACTION_HANDLERS must be referenced by at least
        one ERROR_REGISTRY entry — otherwise it is dead code that cannot be
        triggered through the normal IPC path."""
        referenced = {
            entry["action_id"]
            for entry in ERROR_REGISTRY.values()
            if entry.get("action_id")
        }
        registered = set(ACTION_HANDLERS.keys())
        orphans = registered - referenced
        self.assertSetEqual(
            orphans,
            set(),
            f"Handlers in ACTION_HANDLERS with no referencing ERROR_REGISTRY entry "
            f"({len(orphans)} orphan(s)): {sorted(orphans)}\n"
            f"Fix: add an ERROR_REGISTRY entry or remove the orphan handler.",
        )


class ActionIdFormatValidationTest(unittest.TestCase):

    def test_action_id_format_validation(self):
        """All non-None action_ids must follow snake_case format:
        lowercase letters, digits, underscores, starting with a letter.
        action_ids like 'Open URL' or 'action-id' would break Swift dispatch."""
        bad = []
        for code, entry in ERROR_REGISTRY.items():
            action_id = entry.get("action_id")
            if action_id is None:
                continue
            if not _ACTION_ID_PATTERN.match(action_id):
                bad.append(
                    f"{code}: action_id={action_id!r} does not match "
                    f"snake_case pattern"
                )
        self.assertEqual(
            bad, [],
            "Malformed action_ids:\n  " + "\n  ".join(bad),
        )

    def test_action_id_no_leading_underscore(self):
        """action_ids must not start with underscore (private convention)."""
        bad = [
            f"{code}: {entry['action_id']!r}"
            for code, entry in ERROR_REGISTRY.items()
            if entry.get("action_id") is not None
            and entry["action_id"].startswith("_")
        ]
        self.assertEqual(bad, [], f"action_ids starting with underscore: {bad}")

    def test_handler_keys_match_action_id_format(self):
        """Keys in ACTION_HANDLERS must also satisfy the snake_case format."""
        bad = [
            k for k in ACTION_HANDLERS
            if not _ACTION_ID_PATTERN.match(k)
        ]
        self.assertEqual(
            bad, [],
            f"ACTION_HANDLERS keys with invalid format: {bad}",
        )


class EntrySchemaInvariantTest(unittest.TestCase):

    def test_all_required_keys_present_in_every_entry(self):
        """Every ERROR_REGISTRY entry must have all required keys."""
        bad = []
        for code, entry in ERROR_REGISTRY.items():
            missing_keys = _REQUIRED_ENTRY_KEYS - set(entry.keys())
            if missing_keys:
                bad.append(f"{code}: missing keys {sorted(missing_keys)}")
        self.assertEqual(
            bad, [],
            "ERROR_REGISTRY entries with missing required keys:\n  "
            + "\n  ".join(bad),
        )

    def test_dedupe_seconds_is_positive_integer(self):
        """dedupe_seconds must be a positive integer in every entry."""
        bad = []
        for code, entry in ERROR_REGISTRY.items():
            ds = entry.get("dedupe_seconds")
            if not isinstance(ds, int) or ds <= 0:
                bad.append(f"{code}: dedupe_seconds={ds!r} (must be positive int)")
        self.assertEqual(
            bad, [],
            "Entries with invalid dedupe_seconds:\n  " + "\n  ".join(bad),
        )

    def test_severity_is_valid_literal(self):
        """Every entry's severity must be in the ErrorBus Severity Literal set."""
        bad = []
        for code, entry in ERROR_REGISTRY.items():
            sev = entry.get("severity")
            if sev not in _VALID_SEVERITIES:
                bad.append(f"{code}: severity={sev!r} not in {_VALID_SEVERITIES}")
        self.assertEqual(
            bad, [],
            "Entries with invalid severity:\n  " + "\n  ".join(bad),
        )

    def test_non_actionable_entries_have_null_action_id(self):
        """Entries with actionable=False must have action_id=None to avoid
        confusing the UI into showing a non-functional button."""
        bad = []
        for code, entry in ERROR_REGISTRY.items():
            if not entry.get("actionable") and entry.get("action_id"):
                bad.append(
                    f"{code}: actionable=False but action_id={entry['action_id']!r}"
                )
        self.assertEqual(
            bad, [],
            "Non-actionable entries with non-null action_id:\n  "
            + "\n  ".join(bad),
        )

    def test_error_registry_not_empty(self):
        """ERROR_REGISTRY must have at least the core error codes."""
        self.assertGreater(len(ERROR_REGISTRY), 10,
                           "ERROR_REGISTRY appears too small — check import")

    def test_action_handlers_not_empty(self):
        """ACTION_HANDLERS must have at least the core handlers."""
        self.assertGreater(len(ACTION_HANDLERS), 5,
                           "ACTION_HANDLERS appears too small — check import")


class ErrorCodeKeyFormatTest(unittest.TestCase):

    _CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")

    def test_error_code_key_format(self):
        """All ERROR_REGISTRY keys must follow 'component.code' format with
        lowercase alphanumeric components separated by dots."""
        bad = [
            code
            for code in ERROR_REGISTRY
            if not self._CODE_PATTERN.match(code)
        ]
        self.assertEqual(
            bad, [],
            f"ERROR_REGISTRY keys with invalid format (expected 'component.code'): "
            f"{bad}",
        )

    def test_error_code_key_component_matches_entry_component(self):
        """The component prefix in the error code key (before first dot) should
        correspond to a known component in the error_bus Component Literal.

        This is a soft check — we validate against the runtime Literal via
        import rather than hard-coding the list."""
        from backend.error_bus import KrabError
        import typing

        # Extract valid components from the KrabError model's Component Literal
        component_field = KrabError.model_fields["component"]
        try:
            # Pydantic v2: annotation holds the Literal type
            valid_components = set(typing.get_args(component_field.annotation))
        except Exception:
            # Fallback: skip if introspection fails
            return

        bad = []
        for code in ERROR_REGISTRY:
            prefix = code.split(".")[0]
            if prefix not in valid_components:
                bad.append(f"{code}: component prefix '{prefix}' not in Component Literal")

        self.assertEqual(
            bad, [],
            "Error codes with prefix not in Component Literal:\n  "
            + "\n  ".join(bad),
        )


class RegistryCoverageReport(unittest.TestCase):
    """Non-failing informational tests — output useful stats on stderr."""

    def test_registry_stats(self):
        """Report counts by severity and actionable flag (never fails)."""
        by_severity: dict[str, int] = {}
        actionable_count = 0
        for entry in ERROR_REGISTRY.values():
            sev = entry.get("severity", "unknown")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            if entry.get("actionable"):
                actionable_count += 1

        total = len(ERROR_REGISTRY)
        handler_count = len(ACTION_HANDLERS)
        import sys
        print(
            f"\n[wave164 invariant] ERROR_REGISTRY: {total} codes | "
            f"ACTION_HANDLERS: {handler_count} handlers | "
            f"actionable: {actionable_count} | "
            f"by_severity: {dict(sorted(by_severity.items()))}",
            file=sys.stderr,
        )
        # Always passes
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main()
