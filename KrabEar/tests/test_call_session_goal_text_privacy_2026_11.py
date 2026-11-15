"""goal_text звонковой сессии — PII и в privacy-режиме не отдаётся.

Цель звонка («уточнить результаты анализов в клинике») раскрывает суть
разговора не меньше номера и транскрипта, которые `_scrub_session` уже
затирал. Ключ остаётся в ответе (schema-parity), значение — REDACTED.
Проверка идёт на настоящем ``CallSessionStore``: мок ``to_dict`` не доказывает,
что реальная запись не просочится.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_session_store import CallSessionStore  # noqa: E402

SECRET_GOAL = "уточнить результаты анализов в клинике"


def _svc(tmp: Path, privacy: bool) -> tuple[CallSessionService, CallSessionStore]:
    store = CallSessionStore(tmp)
    svc = CallSessionService(
        store=store,
        settings_get=lambda k, d: privacy if k == "privacy_mode_enabled" else d,
    )
    return svc, store


class GoalTextPrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_get_privacy_mode_redacts_goal_text(self) -> None:
        svc, store = _svc(self.dir, privacy=True)
        session = store.create(phone_number="+34911234567", goal_text=SECRET_GOAL)

        result = svc.handle_call_session_get({"id": session.id})

        self.assertEqual(result["goal_text"], "REDACTED")
        self.assertNotIn(SECRET_GOAL, repr(result))

    def test_list_privacy_mode_redacts_goal_text(self) -> None:
        svc, store = _svc(self.dir, privacy=True)
        store.create(phone_number="+34911234567", goal_text=SECRET_GOAL)
        store.create(phone_number="+34911234568", goal_text=SECRET_GOAL + " 2")

        result = svc.handle_call_session_list({})

        self.assertEqual(result["total"], 2)
        for item in result["sessions"]:
            self.assertEqual(item["goal_text"], "REDACTED")
        self.assertNotIn("анализов", repr(result))

    def test_normal_mode_keeps_goal_text(self) -> None:
        svc, store = _svc(self.dir, privacy=False)
        session = store.create(phone_number="+34911234567", goal_text=SECRET_GOAL)

        got = svc.handle_call_session_get({"id": session.id})
        listed = svc.handle_call_session_list({})

        self.assertEqual(got["goal_text"], SECRET_GOAL)
        self.assertEqual(listed["sessions"][0]["goal_text"], SECRET_GOAL)

    def test_privacy_mode_keeps_schema_keys(self) -> None:
        svc_on, store = _svc(self.dir, privacy=True)
        session = store.create(phone_number="+34911234567", goal_text=SECRET_GOAL)
        svc_off, _ = _svc(self.dir, privacy=False)

        on_keys = set(svc_on.handle_call_session_get({"id": session.id}))
        off_keys = set(svc_off.handle_call_session_get({"id": session.id}))

        self.assertEqual(on_keys, off_keys)


if __name__ == "__main__":
    unittest.main()
