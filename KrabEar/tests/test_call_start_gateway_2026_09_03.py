"""`call_start` — путь инициации звонка, которого у Krab Ear не было.

Разведка 03.09.2026: собственная телефония (3049 строк, Telnyx/Twilio/local-SIP)
не просто «не звонила» — она НЕ ПОДКЛЮЧЕНА: `dial()` не вызывается ни одной
строкой прод-кода, `get_provider` не импортирует ни один модуль, а
`CallSessionService` принимает `get_provider_fn` и никогда его не зовёт
(в `service.py` он даже не передаётся). `call_session_create` — журнальная
запись о звонке, который никто не совершает.

Владелец выбрал вариант «тонкий клиент шлюза», поэтому путь строится: IPC
`call_start` → провайдер (Voice Gateway) → запись сессии в существующий журнал.

Инварианты, которые тест держит:
  * без провайдера — понятный отказ, а не молчание и не пустая сессия;
  * отказ шлюза НЕ создаёт запись в журнале (иначе история копит звонки,
    которых не было — ровно то, чем эта телефония и болела);
  * успех возвращает session_id шлюза как ручку управления;
  * privacy_mode запрещает исходящие звонки целиком.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_session_store import CallSessionStore  # noqa: E402


class _Provider:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[dict] = []

    def dial(self, to_number, call_control_id=None, webhook_url=None, prompt=None, **extra):
        self.calls.append({"to": to_number, "prompt": prompt, **extra})
        return dict(self.result)

    def is_configured(self) -> bool:
        return True


def _svc(tmp: str, provider=None, privacy: bool = False) -> CallSessionService:
    store = CallSessionStore(Path(tmp) / "calls.ndjson")
    return CallSessionService(
        store=store,
        settings_get=lambda k, d=None: True if (k == "privacy_mode_enabled" and privacy) else d,
        get_provider_fn=(lambda _s: provider) if provider else None,
    )


class CallStartTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_successful_call_returns_gateway_session(self) -> None:
        prov = _Provider({"ok": True, "call_control_id": "vs_9", "call_id": "CA9", "status": "dialing"})
        svc = _svc(self.tmp.name, prov)
        out = svc.handle_call_start({"phone": "+34911234567", "goal_text": "уточнить часы"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["gateway_session_id"], "vs_9")
        self.assertTrue(out["session_id"], "локальная запись журнала обязана быть создана")
        self.assertEqual(prov.calls[0]["to"], "+34911234567")

    def test_provider_failure_leaves_no_phantom_session(self) -> None:
        prov = _Provider({"ok": False, "error": "twilio_rejected"})
        svc = _svc(self.tmp.name, prov)
        out = svc.handle_call_start({"phone": "+34911234567", "goal_text": "цель"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "twilio_rejected")
        listed = svc.handle_call_session_list({})
        self.assertEqual(listed["total"], 0, "несостоявшийся звонок не должен оседать в журнале")

    def test_without_provider_reports_clearly(self) -> None:
        svc = _svc(self.tmp.name, provider=None)
        out = svc.handle_call_start({"phone": "+34911234567", "goal_text": "цель"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "call_provider_unavailable")

    def test_privacy_mode_blocks_outgoing_calls(self) -> None:
        prov = _Provider({"ok": True, "call_control_id": "vs_1"})
        svc = _svc(self.tmp.name, prov, privacy=True)
        out = svc.handle_call_start({"phone": "+34911234567", "goal_text": "цель"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "privacy_mode")
        self.assertEqual(prov.calls, [], "в приватном режиме номер не должен уходить наружу")

    def test_phone_and_goal_are_required(self) -> None:
        svc = _svc(self.tmp.name, _Provider({"ok": True}))
        with self.assertRaises(ValueError):
            svc.handle_call_start({"goal_text": "цель"})
        with self.assertRaises(ValueError):
            svc.handle_call_start({"phone": "+34911234567"})
