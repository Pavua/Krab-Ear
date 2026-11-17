"""F3 (ревью 2026-11-16/17): завершение звонка из UI не доходит до Voice Gateway.

Воспроизведено на настоящих CallSessionService + CallSessionStore:
 * `call_start` звонит через провайдера и заводит журнальную сессию в состоянии `idle`;
 * дальше состояние никто не продвигает, а `mark_completed` разрешает только `ending → completed`,
   поэтому `call_session_end` ВСЕГДА отвечает `{"ok": False, "reason": "invalid_state_transition"}`;
 * `call_session_end` не вызывает `provider.hangup`, а журнал не хранит `gateway_session_id` —
   настоящий звонок в шлюзе продолжается (деньги), пока не сработает его собственный лимит.

Тесты — СТРОГИЙ xfail: фиксируют требуемый контракт и сами покраснеют («XPASS»), когда его
реализуют. Правка требует решения владельца и сессии Voice Gateway (контракт hangup).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_session_service import CallSessionService  # noqa: E402
from backend.call_session_store import CallSessionStore  # noqa: E402

_GATEWAY_SESSION = "vs_test_gateway_session"


class _RecordingProvider:
    """Двойник GatewayCallProvider: формы ответов взяты из его dial()/hangup()."""

    def __init__(self) -> None:
        self.hangups: list[str] = []

    def dial(self, phone, **kwargs):
        return {"ok": True, "call_control_id": _GATEWAY_SESSION, "call_id": "CA_test",
                "status": "dialing", "provider": "voice_gateway"}

    def hangup(self, call_control_id):
        self.hangups.append(call_control_id)
        return {"ok": True}


@pytest.fixture()
def started(tmp_path):
    provider = _RecordingProvider()
    service = CallSessionService(
        CallSessionStore(tmp_path), settings_get=lambda key, default: default,
        get_provider_fn=lambda _settings: provider,
    )
    result = service.handle_call_start({"phone": "+10000000000", "goal_text": "тест", "prompt": "тест"})
    assert result["ok"] is True
    return service, provider, result["session_id"]


@pytest.mark.xfail(strict=True, reason="F3: call_session_end не вызывает provider.hangup")
def test_call_session_end_hangs_up_the_gateway_call(started):
    service, provider, session_id = started
    service.handle_call_session_end({"id": session_id, "reason": "completed"})
    assert provider.hangups == [_GATEWAY_SESSION]


@pytest.mark.xfail(strict=True, reason="F3: журнал остаётся в idle, завершение всегда отвергается")
def test_call_session_end_completes_a_session_started_by_call_start(started):
    service, _provider, session_id = started
    result = service.handle_call_session_end({"id": session_id, "reason": "completed"})
    assert result.get("ok") is not False, result
    assert service.handle_call_session_get({"id": session_id})["status"] in {"completed", "failed"}
