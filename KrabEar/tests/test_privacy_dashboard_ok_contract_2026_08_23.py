"""Шов Python↔Swift: get_privacy_dashboard обязан класть ``ok`` ВНУТРЬ result.

Живой инцидент 2026-08-23: секция «Приватность и данные» в проде ВСЕГДА
показывала «Нет данных — бэкенд недоступен», хотя IPC отвечал успешно.
Корень — контракт-дрейф: Swift-клиент
(``HistoryPanelController+PrivacyDashboard.swift``) гейтит разбор payload
на ``result["ok"] == true``, а хендлер клал ``ok`` только в КОНВЕРТ
(``{"id":…, "ok":true, "result":{…}}``), внутрь result — нет. Ветка
``guard let data else`` рисовала fallback, унося с собой и тумблер режима
приватности, и кнопку «Журнал аудита».

Почему не поймали раньше: соседний ``test_privacy_dashboard.py`` проверяет
``resp.get("ok", True)`` — дефолт ``True`` делает утверждение истинным даже
при полностью отсутствующем ключе (класс «тест валидирует дыру»). Оба конца
шва были зелёными по отдельности; ломался только стык.

Контракт задокументирован в CLAUDE.md (Launch Readiness 2026-06-27):
``get_privacy_dashboard {}`` → ``{ok, privacy_mode, encryption_enabled,
storage{…}, retention{…}, audit{…}, purge_available}``.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.privacy_audit import PrivacyAuditLogger  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.service import BackendService  # noqa: E402


class _FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.sample_rate = 16000

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.is_recording = False
        return None


class _FakeTranscriber:
    def __init__(self):
        self.vocabulary = []
        self.profile = "balanced"

    def transcribe(self, audio, sample_rate=16000, language=None, task="transcribe"):
        return {"text": "", "segments": [], "language": "ru", "confidence": 0.0}

    def transcribe_file(self, path, language=None):
        return {"text": "", "segments": [], "language": "ru", "confidence": 0.0}


class _FakeTranslator:
    def translate(self, text, mode="off"):
        return text


class PrivacyDashboardOkContractTest(unittest.TestCase):
    """``ok`` живёт и в конверте, и в payload — Swift читает второй."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        store = StateStore(data_dir=Path(self.tmpdir.name))
        # Изолированный audit-логгер обязателен: без него хендлер читает РЕАЛЬНЫЙ
        # privacy_audit.log владельца (десятки тысяч записей) — тест вешается
        # по таймауту и заодно трогает продовые данные.
        PrivacyAuditLogger.reset_instance()
        self.addCleanup(PrivacyAuditLogger.reset_instance)
        self._audit = PrivacyAuditLogger(
            log_path=Path(self.tmpdir.name) / "privacy_audit.log"
        )
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        # Обязательно: иначе daemon-треды BackendService → exit(1) в CI chunk.
        self.service.close()

    def _call(self) -> dict:
        with patch(
            "backend.service.get_privacy_audit_logger", return_value=self._audit
        ):
            return self.service.handle_request(
                {"id": "pd-ok", "method": "get_privacy_dashboard", "params": {}}
            )

    def test_result_payload_carries_ok_true(self):
        resp = self._call()
        result = resp.get("result")
        self.assertIsInstance(result, dict, f"Нет result в ответе: {resp}")
        # Строгая проверка БЕЗ дефолта: отсутствующий ключ обязан ронять тест
        # (сосед test_privacy_dashboard.py прятал именно это за .get('ok', True)).
        self.assertIn(
            "ok", result,
            "Swift гейтит разбор на result['ok'] — без ключа секция навсегда "
            "уходит в fallback «Нет данных — бэкенд недоступен»",
        )
        self.assertIs(result["ok"], True)

    def test_envelope_ok_still_true(self):
        # Конверт не должен пострадать от добавления payload-ключа.
        resp = self._call()
        self.assertIs(resp.get("ok"), True)

    def test_payload_keys_unchanged_besides_ok(self):
        # Добавление ok — аддитивное: остальная схема на месте.
        result = self._call().get("result") or {}
        required = {
            "privacy_mode", "encryption_enabled", "storage",
            "retention", "audit", "purge_available",
        }
        self.assertTrue(
            required.issubset(result.keys()),
            f"Потеряны ключи: {required - set(result.keys())}",
        )


if __name__ == "__main__":
    unittest.main()
