"""Regression test: ERROR_REGISTRY['action_label'] must reach the toast button.

Live-prod bug (2026-08-19): every actionable error toast showed the generic
"Действие" button label instead of the registry's ``action_label`` (e.g.
``mlx.oom`` → "Выгрузить через Telegram"). Proven via a live IPC
``list_recent_errors`` call: ``code=mlx.oom, actionable=True`` came back with
``context`` keys ``['model', 'profile']`` — no ``action_label`` anywhere.

Root cause: ``ERROR_REGISTRY`` entries carry an ``action_label`` field, but no
production code path ever copied it into ``KrabError.context`` before the
event reached Swift, which reads ``payload.context["action_label"]``
(ErrorToastView.swift) and silently falls back to "Действие" when the key is
absent.

Why the existing tests missed it: ``test_error_codes_actions_invariant.py``
only checks that the registry dict itself has a non-empty ``action_label`` —
it never touches ``ErrorBus``/``KrabError`` at all. ``ErrorToastViewTests.swift``
only exercises the branch where ``context`` already contains
``action_label`` — a branch production never actually took. Both sides were
green while the contract between them was broken.

This file pins the fix at the ONE funnel every ``KrabError`` passes through
(``ErrorBus.push`` — ring buffer, ``krab_error`` event emit, and IPC
``list_recent_errors``/``list_recent_since`` all read the same enriched
object back out), plus a regex-based cross-language guard so a future rename
on either side goes red instead of silently drifting again (see
``scripts/audit_ipc_contract_drift.py`` for the established regex-guard
pattern in this repo).
"""
from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

# Allow imports from KrabEar/ (mirrors the sys.path setup used by sibling
# error_bus test files in this directory).
_HERE = Path(__file__).resolve().parent
_KRAB_EAR_ROOT = _HERE.parent
if str(_KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(_KRAB_EAR_ROOT))
_REPO_ROOT = _KRAB_EAR_ROOT.parent

from backend.error_bus import ErrorBus, KrabError  # noqa: E402
from backend.error_codes import ERROR_REGISTRY  # noqa: E402

# Real actionable code used as the live-prod proof in this bug report.
_ACTIONABLE_CODE = "mlx.oom"
_ACTIONABLE_ENTRY = ERROR_REGISTRY[_ACTIONABLE_CODE]


def _make_bus(registry: dict | None = None) -> tuple[ErrorBus, MagicMock]:
    event_bus = MagicMock()
    bus = ErrorBus(
        event_bus=event_bus,
        registry=registry if registry is not None else ERROR_REGISTRY,
        default_dedupe_window_sec=0.0,
    )
    return bus, event_bus


def _make_actionable_err(code: str, context: dict, action_id: str | None = None) -> KrabError:
    entry = ERROR_REGISTRY[code]
    return KrabError(
        severity=entry["severity"],
        component="mlx",
        code=code,
        message_user=entry["user_msg_ru"],
        message_debug="test debug",
        timestamp=datetime.now(timezone.utc),
        context=context,
        actionable=entry["actionable"],
        action_id=action_id if action_id is not None else entry["action_id"],
    )


class ActionLabelReachesContextTest(unittest.TestCase):
    """The registry's action_label must survive push() -> list_recent_since()."""

    def test_pushed_actionable_error_carries_registry_action_label(self):
        bus, _event_bus = _make_bus()
        err = _make_actionable_err(_ACTIONABLE_CODE, context={"model": "gemma", "profile": "balanced"})

        result = bus.push(err)
        self.assertTrue(result)

        items, _latest_seq = bus.list_recent_since(0)
        self.assertEqual(len(items), 1)
        pushed = items[0]
        self.assertIn(
            "action_label",
            pushed.context,
            "context must carry action_label after push() — this is exactly "
            "the field ErrorToastView.swift reads for the button label",
        )
        self.assertEqual(pushed.context["action_label"], _ACTIONABLE_ENTRY["action_label"])
        # Caller-supplied keys must survive alongside the enrichment.
        self.assertEqual(pushed.context["model"], "gemma")
        self.assertEqual(pushed.context["profile"], "balanced")

    def test_list_recent_also_carries_action_label(self):
        """list_recent() (used by list_recent_errors without since_seq) must
        see the same enrichment as list_recent_since()."""
        bus, _event_bus = _make_bus()
        err = _make_actionable_err(_ACTIONABLE_CODE, context={})
        bus.push(err)

        items = bus.list_recent()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].context.get("action_label"), _ACTIONABLE_ENTRY["action_label"])

    def test_emitted_event_payload_carries_action_label(self):
        """The krab_error event_bus.emit() payload (in-process SSE path) must
        also carry the enrichment, not just the ring-buffer copy."""
        bus, event_bus = _make_bus()
        err = _make_actionable_err(_ACTIONABLE_CODE, context={})
        bus.push(err)

        event_bus.emit.assert_called_once()
        _name, payload = event_bus.emit.call_args[0]
        self.assertEqual(payload["context"].get("action_label"), _ACTIONABLE_ENTRY["action_label"])


class UserProvidedActionLabelNotOverwrittenTest(unittest.TestCase):
    """A caller-supplied context['action_label'] must win over the registry."""

    def test_existing_action_label_in_context_is_preserved(self):
        bus, _event_bus = _make_bus()
        custom_label = "Custom Label From Caller"
        self.assertNotEqual(
            custom_label, _ACTIONABLE_ENTRY["action_label"],
            "test fixture bug: custom label must differ from the registry's "
            "to prove precedence, not just equality by coincidence",
        )
        err = _make_actionable_err(_ACTIONABLE_CODE, context={"action_label": custom_label})

        bus.push(err)

        items, _latest_seq = bus.list_recent_since(0)
        self.assertEqual(items[0].context["action_label"], custom_label)


class NonActionableErrorUnaffectedTest(unittest.TestCase):
    """Non-actionable codes (empty action_label in the registry) must not
    gain a stray action_label key — list_recent_errors must stay unchanged
    for them."""

    def test_non_actionable_code_gets_no_action_label_key(self):
        non_actionable = next(
            code for code, entry in ERROR_REGISTRY.items() if not entry["actionable"]
        )
        entry = ERROR_REGISTRY[non_actionable]
        self.assertEqual(entry["action_label"], "")

        bus, _event_bus = _make_bus()
        err = KrabError(
            severity=entry["severity"],
            component="stt",
            code=non_actionable,
            message_user=entry["user_msg_ru"],
            message_debug="test debug",
            timestamp=datetime.now(timezone.utc),
            context={"foo": "bar"},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        items, _latest_seq = bus.list_recent_since(0)
        self.assertNotIn("action_label", items[0].context)
        self.assertEqual(items[0].context, {"foo": "bar"})

    def test_code_absent_from_registry_gets_no_action_label_key(self):
        """A KrabError pushed for a code the registry doesn't know about
        (e.g. a legacy flat-dict registry in a test fixture) must not crash
        and must not gain a stray key."""
        bus, _event_bus = _make_bus(registry={})
        err = KrabError(
            severity="warn",
            component="stt",
            code="stt.unregistered_code",
            message_user="x",
            message_debug="x",
            timestamp=datetime.now(timezone.utc),
            context={},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        items, _latest_seq = bus.list_recent_since(0)
        self.assertNotIn("action_label", items[0].context)


class SwiftPythonActionLabelKeyContractTest(unittest.TestCase):
    """Cross-language guard: the context key Swift reads for the toast
    button label must match the key Python actually writes.

    Regex-parsed per the project's established convention for Swift↔Python
    contract guards (see scripts/audit_ipc_contract_drift.py) — there is no
    Swift AST tooling in this repo.
    """

    def test_swift_button_label_key_matches_python_enrichment_key(self):
        swift_path = (
            _REPO_ROOT / "native" / "KrabEarAgent" / "Sources" / "KrabEarAgent" / "ErrorToastView.swift"
        )
        if not swift_path.exists():
            self.skipTest(f"Swift source not present at {swift_path} (not checked out in this worktree)")

        swift_src = swift_path.read_text(encoding="utf-8")
        # Ignore comment-only lines so a stale comment can't mask a real
        # rename in the code below it.
        code_lines = "\n".join(
            line for line in swift_src.splitlines() if not line.strip().startswith("//")
        )
        match = re.search(r'\.context\[\s*"([A-Za-z0-9_]+)"\s*\]', code_lines)
        self.assertIsNotNone(
            match,
            "ErrorToastView.swift: no `.context[\"...\"]` read found — the "
            "action-label wiring may have moved; update this guard's regex "
            "alongside that change",
        )
        swift_key = match.group(1)

        # Determine the ACTUAL key Python's ErrorBus writes by running the
        # real enrichment path (not a re-typed string literal, which could
        # drift silently alongside a rename on the Python side too).
        bus, _event_bus = _make_bus()
        err = _make_actionable_err(_ACTIONABLE_CODE, context={})
        bus.push(err)
        items, _latest_seq = bus.list_recent_since(0)
        python_keys = set(items[0].context.keys())

        self.assertIn(
            swift_key,
            python_keys,
            f"Contract drift: ErrorToastView.swift reads context[{swift_key!r}] "
            f"for the toast button label, but ErrorBus.push() only wrote "
            f"{sorted(python_keys)} into context. Keep the Python enrichment "
            f"key and the Swift read key in lockstep.",
        )


if __name__ == "__main__":
    unittest.main()
