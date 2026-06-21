"""Regression tests — call_intervene + call_resume_bot IPC handlers.

Verifies:
1. Both methods are in the BackendService dispatch table.
2. Calling call_intervene sets bot_active=False for the session.
3. Calling call_resume_bot sets bot_active=True for the session.
4. Missing session_id raises ValueError.
5. Unknown session_id raises KeyError.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_session import CallSession, CallStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(session_id: str = "cs_test001", status: str = "talking") -> CallSession:
    session = CallSession.create(phone_number="+70001234567", goal_text="тест вмешательства")
    session.id = session_id
    session.status = status
    return session


def _make_store(session: CallSession | None = None) -> MagicMock:
    store = MagicMock()
    store.get.return_value = session
    return store


def _make_svc(session: CallSession | None = None) -> tuple[CallSessionService, MagicMock]:
    store = _make_store(session)
    svc = CallSessionService(store=store)
    return svc, store


# ---------------------------------------------------------------------------
# 1. Dispatch table presence
# ---------------------------------------------------------------------------

class TestDispatchTablePresence(unittest.TestCase):
    """Both methods must appear in the service.py dispatch table."""

    def _get_dispatch_block(self) -> str:
        service_path = PROJECT_ROOT / "backend" / "service.py"
        return service_path.read_text(encoding="utf-8")

    def test_call_intervene_in_dispatch(self) -> None:
        """'call_intervene' must be a key in the BackendService dispatch table."""
        text = self._get_dispatch_block()
        self.assertIn('"call_intervene"', text, "'call_intervene' not found in service.py")

    def test_call_resume_bot_in_dispatch(self) -> None:
        """'call_resume_bot' must be a key in the BackendService dispatch table."""
        text = self._get_dispatch_block()
        self.assertIn('"call_resume_bot"', text, "'call_resume_bot' not found in service.py")

    def test_call_intervene_delegates_to_call_session_service(self) -> None:
        """Dispatch entry for 'call_intervene' must point to _call_session_service."""
        text = self._get_dispatch_block()
        self.assertIn(
            '"call_intervene": self._call_session_service.handle_call_intervene',
            text,
        )

    def test_call_resume_bot_delegates_to_call_session_service(self) -> None:
        """Dispatch entry for 'call_resume_bot' must point to _call_session_service."""
        text = self._get_dispatch_block()
        self.assertIn(
            '"call_resume_bot": self._call_session_service.handle_call_resume_bot',
            text,
        )


# ---------------------------------------------------------------------------
# 2. Handler logic — bot_active state flips
# ---------------------------------------------------------------------------

class TestCallInterveneHandler(unittest.TestCase):
    """handle_call_intervene sets bot_active=False."""

    def test_intervene_returns_ok_false(self) -> None:
        session = _make_session()
        svc, store = _make_svc(session)
        result = svc.handle_call_intervene({"session_id": session.id})
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], session.id)
        self.assertFalse(result["bot_active"])

    def test_intervene_sets_internal_flag_false(self) -> None:
        session = _make_session()
        svc, store = _make_svc(session)
        svc.handle_call_intervene({"session_id": session.id})
        with svc._bot_active_lock:
            self.assertFalse(svc._bot_active.get(session.id, True))

    def test_intervene_then_resume_flips_flag_true(self) -> None:
        """Intervene followed by resume_bot should end in bot_active=True."""
        session = _make_session()
        svc, store = _make_svc(session)
        svc.handle_call_intervene({"session_id": session.id})
        result = svc.handle_call_resume_bot({"session_id": session.id})
        self.assertTrue(result["bot_active"])
        with svc._bot_active_lock:
            self.assertTrue(svc._bot_active.get(session.id, False))

    def test_intervene_missing_session_id_raises(self) -> None:
        svc, _ = _make_svc()
        with self.assertRaises(ValueError):
            svc.handle_call_intervene({})

    def test_intervene_empty_session_id_raises(self) -> None:
        svc, _ = _make_svc()
        with self.assertRaises(ValueError):
            svc.handle_call_intervene({"session_id": ""})

    def test_intervene_unknown_session_raises(self) -> None:
        svc, store = _make_svc(session=None)  # store.get returns None
        with self.assertRaises(KeyError):
            svc.handle_call_intervene({"session_id": "cs_nonexistent"})


class TestCallResumeBotHandler(unittest.TestCase):
    """handle_call_resume_bot sets bot_active=True."""

    def test_resume_returns_ok_true(self) -> None:
        session = _make_session()
        svc, store = _make_svc(session)
        result = svc.handle_call_resume_bot({"session_id": session.id})
        self.assertTrue(result["ok"])
        self.assertEqual(result["session_id"], session.id)
        self.assertTrue(result["bot_active"])

    def test_resume_sets_internal_flag_true(self) -> None:
        session = _make_session()
        svc, store = _make_svc(session)
        # First intervene to set flag False
        svc.handle_call_intervene({"session_id": session.id})
        svc.handle_call_resume_bot({"session_id": session.id})
        with svc._bot_active_lock:
            self.assertTrue(svc._bot_active.get(session.id, False))

    def test_resume_missing_session_id_raises(self) -> None:
        svc, _ = _make_svc()
        with self.assertRaises(ValueError):
            svc.handle_call_resume_bot({})

    def test_resume_unknown_session_raises(self) -> None:
        svc, store = _make_svc(session=None)
        with self.assertRaises(KeyError):
            svc.handle_call_resume_bot({"session_id": "cs_nonexistent"})

    def test_resume_idempotent(self) -> None:
        """Calling resume_bot twice in a row is safe and stays True."""
        session = _make_session()
        svc, store = _make_svc(session)
        svc.handle_call_resume_bot({"session_id": session.id})
        result = svc.handle_call_resume_bot({"session_id": session.id})
        self.assertTrue(result["bot_active"])


class TestDefaultBotActive(unittest.TestCase):
    """Without any call_intervene, bot is considered active (True by default)."""

    def test_fresh_session_has_no_flag(self) -> None:
        session = _make_session()
        svc, _ = _make_svc(session)
        with svc._bot_active_lock:
            # No entry yet — defaults to active (True) as in VG autopilot
            self.assertNotIn(session.id, svc._bot_active)

    def test_multiple_sessions_independent(self) -> None:
        """Intervening on one session does not affect another."""
        s1 = _make_session("cs_s1")
        s2 = _make_session("cs_s2")
        store = MagicMock()
        store.get.side_effect = lambda sid: s1 if sid == "cs_s1" else s2
        svc = CallSessionService(store=store)

        svc.handle_call_intervene({"session_id": "cs_s1"})
        with svc._bot_active_lock:
            self.assertFalse(svc._bot_active.get("cs_s1", True))
            # cs_s2 is unaffected — not yet in dict, defaults to active
            self.assertNotIn("cs_s2", svc._bot_active)


if __name__ == "__main__":
    unittest.main()
