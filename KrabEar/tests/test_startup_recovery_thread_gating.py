"""Гейт-логика запуска фонового треда startup-recovery (R1, CI-амендмент 2026-07-24).

Ubuntu CI нашёл красный: голый daemon-тред, стартующий РЕАЛЬНОЕ дисковое I/O
(write_alive_marker создаёт data_dir/маркер немедленно) при КАЖДОМ
конструировании BackendService, гонялся с tearDown-очисткой tempfile в тестах
(``OSError: Directory not empty``) — таких конструирований в тест-сьюте сотни,
почти все на свежих ``TemporaryDirectory()``. Корневая причина: тред спавнился
безусловно, хотя в подавляющем большинстве случаев (чистый старт: маркера нет,
rescue/ пуст) ему нечего делать. Эти тесты проверяют, что:

- на чистом старте (нет dirty-маркера, нет .part-файлов) тред НЕ спавнится —
  маркер текущей жизни пишется синхронно, до возврата из __init__;
- когда есть реальная работа (dirty-маркер ИЛИ .part-файлы) — тред спавнится,
  как и раньше (спека §4.2/§4.3 не нарушена: медленная форензика/транскрипция
  остаётся в фоне, IPC не ждёт).
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.shutdown_forensics import _MARKER, write_alive_marker  # noqa: E402
from backend.recording_spill import RecordingSpillWriter  # noqa: E402


class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self.channels = 1

    def start(self, spill=None):
        return False

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        return None

    def snapshot_rms(self):
        return 0.0

    def snapshot_audio(self):
        return None


class FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "", "confidence": 0.0, "engine": "fake"}


class FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text="", status="not_requested", source_lang="",
            target_lang="", mode="off", engine="fake",
        )


def _startup_recovery_thread_names() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == "startup-recovery"]


class CleanStartNoThreadTest(unittest.TestCase):
    """Чистый старт (нет маркера, нет rescue/) — тред НЕ спавнится."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"

    def test_no_startup_recovery_thread_on_clean_start(self) -> None:
        """Перехватывает КОНСТРУКТОР threading.Thread — ловит сам факт спавна,
        а не гонку с его (возможно, мгновенным на пустом data_dir) завершением.
        Обычный poll-по-имени после sleep НЕ ловит регрессию: без фикса тред
        успевает стартовать и завершиться быстрее любого разумного окна
        ожидания на пустом каталоге (проверено — RED без патча не отличим
        от GREEN на чистом data_dir, именно поэтому нужен перехват вызова)."""
        real_thread_cls = threading.Thread
        spawned_names: list[str] = []

        class _SpyThread(real_thread_cls):
            def __init__(self, *args, **kwargs):
                if kwargs.get("name") == "startup-recovery":
                    spawned_names.append("startup-recovery")
                super().__init__(*args, **kwargs)

        store = StateStore(self.data_dir)
        with patch("backend.service.threading.Thread", _SpyThread):
            service = BackendService(
                store=store, recorder=FakeRecorder(),
                transcriber=FakeTranscriber(), translator=FakeTranslator(),
            )
        try:
            self.assertEqual(spawned_names, [], "startup-recovery не должен спавниться на чистом старте")
        finally:
            service.close()

    def test_marker_written_synchronously_before_init_returns(self) -> None:
        store = StateStore(self.data_dir)
        service = BackendService(
            store=store, recorder=FakeRecorder(),
            transcriber=FakeTranscriber(), translator=FakeTranslator(),
        )
        try:
            # Маркер обязан существовать СРАЗУ после возврата из __init__ —
            # без sleep/poll (иначе фикс не решает исходную гонку).
            marker_path = self.data_dir / _MARKER
            self.assertTrue(marker_path.exists())
        finally:
            service.close()


class DirtyMarkerSpawnsThreadTest(unittest.TestCase):
    """Есть dirty-маркер прошлой жизни — тред ОБЯЗАН спавниться (форензика)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Симулируем прошлую жизнь, умершую без graceful shutdown.
        write_alive_marker(self.data_dir)

    def test_thread_spawns_when_dirty_marker_present(self) -> None:
        store = StateStore(self.data_dir)
        service = BackendService(
            store=store, recorder=FakeRecorder(),
            transcriber=FakeTranscriber(), translator=FakeTranslator(),
        )
        try:
            deadline = time.monotonic() + 5.0
            spawned = False
            while time.monotonic() < deadline:
                if _startup_recovery_thread_names():
                    spawned = True
                    break
                time.sleep(0.05)
            self.assertTrue(spawned, "startup-recovery обязан спавниться при UNCLEAN-маркере")
            # Дождаться завершения (best-effort collect на пустом data_dir быстрый).
            deadline2 = time.monotonic() + 10.0
            while _startup_recovery_thread_names() and time.monotonic() < deadline2:
                time.sleep(0.05)
        finally:
            service.close()


class PendingRescuePartsSpawnThreadTest(unittest.TestCase):
    """Есть замороженные .part-кандидаты — тред ОБЯЗАН спавниться (rescue-скан)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        rescue_dir = self.data_dir / "rescue"
        w = RecordingSpillWriter(rescue_dir=rescue_dir, sample_rate=16000, channels=1, source="dictation")
        self.assertTrue(w.open())
        import numpy as np
        w.append(np.ones(16000, dtype=np.float32) * 0.1)
        w.close()

    def test_thread_spawns_when_pending_part_exists(self) -> None:
        # Poll-по-имени ловит гонку: без dirty-маркера check_and_collect быстрый,
        # тред успевает завершиться до первого sleep (тот же класс, что
        # CleanStartNoThreadTest — шпион на конструктор Thread).
        real_thread_cls = threading.Thread
        spawned_names: list[str] = []

        class _SpyThread(real_thread_cls):
            def __init__(self, *args, **kwargs):
                if kwargs.get("name") == "startup-recovery":
                    spawned_names.append("startup-recovery")
                super().__init__(*args, **kwargs)

        store = StateStore(self.data_dir)
        with patch("backend.service.threading.Thread", _SpyThread):
            service = BackendService(
                store=store, recorder=FakeRecorder(),
                transcriber=FakeTranscriber(), translator=FakeTranslator(),
            )
        try:
            self.assertIn(
                "startup-recovery", spawned_names,
                "startup-recovery обязан спавниться при незавершённой записи",
            )
            deadline2 = time.monotonic() + 20.0
            while _startup_recovery_thread_names() and time.monotonic() < deadline2:
                time.sleep(0.05)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
