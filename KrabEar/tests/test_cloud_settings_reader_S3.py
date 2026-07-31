"""S3/Задача 2 — ``cloud_stt``/``cloud_rewriter`` читают настройки через
инъецируемый аксессор владельца процесса, а не через собственный
module-level ``StateStore``.

До фикса оба модуля строили ``store = StateStore(settings.DATA_DIR)`` на
ИМПОРТЕ. После выравнивания каталога данных (S3/Задача 1) это те же файлы,
что у основного ``StateStore`` процесса — а per-thread depth-counter
реентерабельности ``_lock()`` (#1872) живёт в поле ЭКЗЕМПЛЯРА, поэтому между
двумя экземплярами он не защищает (см.
``test_state_store_cross_instance_lock_S3.py``). Фикс — вообще не строить
собственный ``StateStore``, когда владелец процесса подключил свой аксессор
через ``adopt_settings_reader()``.

RED-тест этой задачи: после ``adopt_settings_reader(...)`` ни один вызов
``_load_settings()`` не должен конструировать ``StateStore``. На коде ДО
фикса ``adopt_settings_reader`` не существует вовсе — тест падает
``AttributeError`` (фича отсутствует), что и есть правильная причина для RED.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import backend.cloud_stt as cloud_stt  # noqa: E402
import backend.cloud_rewriter as cloud_rewriter  # noqa: E402


class _ReaderResetMixin:
    """Сбрасывает module-level состояние обоих модулей до/после каждого теста,
    чтобы тесты не текли друг в друга (adopt_settings_reader — глобальный
    переключатель на весь процесс)."""

    def setUp(self):
        super().setUp()
        self._orig_cloud_stt_settings_fn = cloud_stt._settings_fn
        self._orig_cloud_stt_fallback = cloud_stt._fallback_store_instance
        self._orig_cr_settings_fn = cloud_rewriter._settings_fn
        self._orig_cr_fallback = cloud_rewriter._fallback_store_instance
        cloud_stt._settings_fn = None
        cloud_stt._fallback_store_instance = None
        cloud_rewriter._settings_fn = None
        cloud_rewriter._fallback_store_instance = None

    def tearDown(self):
        cloud_stt._settings_fn = self._orig_cloud_stt_settings_fn
        cloud_stt._fallback_store_instance = self._orig_cloud_stt_fallback
        cloud_rewriter._settings_fn = self._orig_cr_settings_fn
        cloud_rewriter._fallback_store_instance = self._orig_cr_fallback
        super().tearDown()


class TestAdoptSettingsReaderSkipsStateStoreConstruction(_ReaderResetMixin, unittest.TestCase):
    """RED-тест задачи: adopt_settings_reader() → ноль конструирований StateStore."""

    @patch("backend.cloud_stt.StateStore")
    def test_cloud_stt_zero_state_store_calls_after_adopt(self, mock_state_store_cls):
        fake_settings = {"openai_api_key": "k", "deepgram_api_key": "k2", "assemblyai_api_key": "k3"}
        cloud_stt.adopt_settings_reader(lambda: dict(fake_settings))

        # Несколько вызовов подряд — ни один не должен трогать StateStore.
        for _ in range(3):
            result = cloud_stt._load_settings()
            self.assertEqual(result, fake_settings)

        mock_state_store_cls.assert_not_called()

    @patch("backend.cloud_rewriter.StateStore")
    def test_cloud_rewriter_zero_state_store_calls_after_adopt(self, mock_state_store_cls):
        fake_settings = {"cloud_rewriter_provider": "openai", "openai_api_key": "k"}
        cloud_rewriter.adopt_settings_reader(lambda: dict(fake_settings))

        for _ in range(3):
            result = cloud_rewriter._load_settings()
            self.assertEqual(result, fake_settings)

        mock_state_store_cls.assert_not_called()

    @patch("backend.cloud_stt.StateStore")
    def test_cloud_stt_providers_read_via_adopted_reader_no_state_store(self, mock_state_store_cls):
        """Реальные точки входа (провайдеры), не только _load_settings() напрямую —
        восемь мест из плана (openai/deepgram/assemblyai * cloud_stt) должны идти
        через один и тот же аксессор."""
        cloud_stt.adopt_settings_reader(lambda: {})  # пустые настройки → stub-режим, без сети

        openai_provider = cloud_stt.get_cloud_stt_provider("openai")
        deepgram_provider = cloud_stt.get_cloud_stt_provider("deepgram")
        assemblyai_provider = cloud_stt.get_cloud_stt_provider("assemblyai")

        self.assertEqual(openai_provider.transcribe(b"x", 16000, "ru").get("error"), "no_api_key")
        self.assertEqual(deepgram_provider.transcribe(b"x", 16000, "ru").get("error"), "no_api_key")
        self.assertEqual(assemblyai_provider.transcribe(b"x", 16000, "ru").get("error"), "no_api_key")

        mock_state_store_cls.assert_not_called()


class TestFallbackLazySingletonWhenNoReaderAdopted(_ReaderResetMixin, unittest.TestCase):
    """Standalone-режим/тесты без владельца процесса: ленивый fallback-store
    создаётся не более одного раза (double-checked locking)."""

    @patch("backend.cloud_stt.StateStore")
    def test_cloud_stt_fallback_constructs_state_store_exactly_once(self, mock_state_store_cls):
        mock_instance = mock_state_store_cls.return_value
        mock_instance.load_settings.return_value = {"ok": True}

        for _ in range(5):
            result = cloud_stt._load_settings()
            self.assertEqual(result, {"ok": True})

        mock_state_store_cls.assert_called_once()

    @patch("backend.cloud_rewriter.StateStore")
    def test_cloud_rewriter_fallback_constructs_state_store_exactly_once(self, mock_state_store_cls):
        mock_instance = mock_state_store_cls.return_value
        mock_instance.load_settings.return_value = {"ok": True}

        for _ in range(5):
            result = cloud_rewriter._load_settings()
            self.assertEqual(result, {"ok": True})

        mock_state_store_cls.assert_called_once()

    @patch("backend.cloud_stt.StateStore")
    def test_cloud_stt_fallback_singleton_under_concurrent_access(self, mock_state_store_cls):
        """Наивный check-then-set дал бы больше одного экземпляра под гонкой —
        именно ту мину, от которой уходим (два StateStore на одних файлах)."""
        mock_state_store_cls.return_value.load_settings.return_value = {}

        barrier = threading.Barrier(8)

        def _worker():
            barrier.wait(timeout=5.0)
            cloud_stt._load_settings()

        threads = [threading.Thread(target=_worker, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())

        mock_state_store_cls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
