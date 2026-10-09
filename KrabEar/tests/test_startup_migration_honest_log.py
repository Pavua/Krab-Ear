"""Честный startup-лог миграции (SOURCE debt A5_2B_CALLER_SUCCESS_LOG_DEBT).

RED-волна по STARTUP_MIGRATION_CARD §3: caller-side регрессия —
``BackendService.__init__`` (W1034 block) обязан НЕ логировать
"migration complete" когда ``MigrationResult.reason`` set, а логировать
отказ (WARNING с reason, без слов success/complete).

Behavioral-тесты: реальный ``BackendService`` + stub-коллабораторы
(паттерн ``test_data_migrator.py:963-1039``) + временный OFF-профиль
(инверсия ``test_a52b2_snapshot_restore.py:82-83``) + детерминированный
stub ``DataMigrator`` (approved вариант карточки §3: реальный
``MigrationResult``, assert called once). Шифрование не включается,
Keychain не трогается, history-мутации не assert'ятся.

Лог-перехват: ТОЛЬКО caller-логгер ``KrabEar.Backend.Service`` на пороге
INFO + explicit ``record.name`` + explicit levels (WARNING-only capture и
unfiltered caplog запрещены карточкой).
"""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.data_migrator import DataMigrator, MigrationResult
from backend.service import BackendService
from backend.state_store import StateStore
from backend.translator import TranslationResult

CALLER_LOGGER = "KrabEar.Backend.Service"
REFUSAL_REASON = "history_encryption_operation_unavailable"
FORBIDDEN_REFUSAL_WORDS = ("success", "complete")


def _make_v1_item(item_id: str = "abc", text: str = "Привет мир") -> dict:
    """v1.0-запись: без полей tags/favorite/annotation (как test_data_migrator)."""
    return {
        "id": item_id,
        "ts": "2024-01-01T12:00:00",
        "text": text,
        "paste_status": "ok",
    }


def _write_ndjson(path: Path, items: list) -> None:
    lines = [json.dumps(item, ensure_ascii=False) for item in items]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _StubEngine:
    """Минимальный stub AudioEngine (паттерн test_data_migrator.py:963-975)."""

    quality_profile: str = "balanced"
    current_model: str = "stub-model"
    _llm_rewriter = None
    _settings_get = None

    def _resolve_diarization_device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        pass


class _StubTranscriber:
    """Минимальный stub Transcriber (паттерн test_data_migrator.py:977-988)."""

    def __init__(self) -> None:
        self.engine = _StubEngine()
        self._error_bus = None

    def transcribe(self, audio, **kw) -> str:
        return "test transcription"

    def transcribe_preview(self, audio, **kw) -> str:
        return "preview"


class _StubRecorder:
    """Минимальный stub AudioRecorder (паттерн test_data_migrator.py:991-999)."""

    is_recording = False

    def start(self) -> None:
        pass

    def stop(self) -> bytes:
        return b""


class _StubTranslator:
    """Минимальный stub Translator (паттерн test_data_migrator.py:1002-1014)."""

    def translate(self, text, **kw):
        return TranslationResult(
            text=text,
            status="ok",
            source_lang="ru",
            target_lang="ru",
            mode="off",
            engine="stub",
        )


class StartupMigrationHonestLogTestCase(unittest.TestCase):
    """Caller-side честность startup-лога миграции (STARTUP_MIGRATION_CARD §3)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _make_off_profile_with_v1(self) -> Path:
        """Временный OFF-профиль + v1 history.ndjson (1 запись)."""
        data_dir = Path(self._tmp.name) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "settings.json").write_text(
            json.dumps({"history_encryption_enabled": False}), encoding="utf-8"
        )
        _write_ndjson(data_dir / "history.ndjson", [_make_v1_item()])
        return data_dir

    def _run_startup_with_stubbed_migrator(self, data_dir: Path, reason):
        """Конструирует реальный BackendService со stubbed DataMigrator.

        Возвращает (service, caller_records, migrate_mock). Caller обязан
        закрыть service (см. вызывающие тесты: service.close() в finally).
        """
        plan = [
            "Текущая версия схемы: 1.0",
            "Целевая версия схемы: 2.0",
            "Записей в истории (активных): 1",
            "Записей, требующих обновления: 1",
        ]
        result = MigrationResult(
            from_version="1.0",
            to_version="2.0",
            items_migrated=0,
            items_skipped=0,
            backup_path="",
            reason=reason,
        )
        with (
            mock.patch.object(
                DataMigrator, "check_migration_needed", return_value=True
            ),
            mock.patch.object(DataMigrator, "get_migration_plan", return_value=plan),
            mock.patch.object(DataMigrator, "migrate", return_value=result) as mock_migrate,
            self.assertLogs(CALLER_LOGGER, level="INFO") as captured,
        ):
            service = BackendService(
                store=StateStore(data_dir),
                recorder=_StubRecorder(),
                transcriber=_StubTranscriber(),
                translator=_StubTranslator(),
            )
        caller_records = [r for r in captured.records if r.name == CALLER_LOGGER]
        return service, caller_records, mock_migrate

    def test_refusal_does_not_log_completion(self) -> None:
        """Отказ (reason set) — НЕТ completion INFO, ЕСТЬ refusal WARNING."""
        data_dir = self._make_off_profile_with_v1()
        service, caller_records, mock_migrate = self._run_startup_with_stubbed_migrator(
            data_dir, REFUSAL_REASON
        )
        try:
            mock_migrate.assert_called_once()
            self.assertEqual(
                mock_migrate.return_value.reason,
                REFUSAL_REASON,
                "stub migrate обязан вернуть reason отказа",
            )
            self.assertGreater(
                len(caller_records), 0, "ожидались записи caller-логгера"
            )
            completion_infos = [
                r
                for r in caller_records
                if r.levelno == logging.INFO and "migration complete" in r.getMessage()
            ]
            self.assertEqual(
                completion_infos,
                [],
                "отказ миграции не должен логировать 'migration complete' на INFO",
            )
            refusals = [
                r
                for r in caller_records
                if r.levelno == logging.WARNING and REFUSAL_REASON in r.getMessage()
            ]
            self.assertEqual(
                len(refusals), 1, "ожидался ровно один refusal WARNING с reason"
            )
            for record in refusals:
                lowered = record.getMessage().lower()
                for word in FORBIDDEN_REFUSAL_WORDS:
                    self.assertNotIn(
                        word, lowered, f"refusal не должен содержать слово {word!r}"
                    )
        finally:
            service.close()

    def test_success_logs_completion(self) -> None:
        """Успех (reason None) — ЕСТЬ completion INFO (guard от инверсии)."""
        data_dir = self._make_off_profile_with_v1()
        service, caller_records, mock_migrate = self._run_startup_with_stubbed_migrator(
            data_dir, None
        )
        try:
            mock_migrate.assert_called_once()
            self.assertIsNone(
                mock_migrate.return_value.reason,
                "stub migrate обязан вернуть reason=None для успеха",
            )
            completion_infos = [
                r
                for r in caller_records
                if r.levelno == logging.INFO
                and r.name == CALLER_LOGGER
                and "migration complete" in r.getMessage()
            ]
            self.assertGreaterEqual(
                len(completion_infos),
                1,
                "успешная миграция обязана логировать 'migration complete' на INFO",
            )
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
