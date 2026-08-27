"""Heartbeat/generation/stop()->bool в OpenWakeWordAdapter (спека 2026-07-15).

_listen_loop вызывается СИНХРОННО с фейковым sounddevice-модулем,
вставленным в sys.modules ОБРАТИМО (setUp/tearDown).
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
BACKEND_DIR = _PROJECT_ROOT / "KrabEar"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


class _FakeStream:
    """Контекст-менеджер, эмулирующий sd.InputStream: отдаёт заготовленные
    чанки; когда чанки кончились — взводит stop_event и отдаёт нули."""

    def __init__(self, chunks, stop_event):
        self._chunks = list(chunks)
        self._stop_event = stop_event
        self.reads = 0

    @property
    def read_available(self) -> int:
        # Guarded read (спека 2026-08-23) не зовёт read(), пока кадров нет:
        # отсутствие атрибута трактуется как голодание (fail-open запрещён —
        # он вернул бы неубиваемый блокирующий Pa_ReadStream). Заготовленные
        # чанки «всегда доступны», поэтому отдаём заведомо достаточное число.
        return 1 << 20

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        self.reads += 1
        if not self._chunks:
            self._stop_event.set()
            return np.zeros((n, 1), dtype=np.int16), False
        return self._chunks.pop(0), False


class _FakeOWW:
    def predict(self, arr):
        return {}


def _nonzero_chunk(n=4):
    a = np.zeros((n, 1), dtype=np.int16)
    a[0][0] = 7
    return a


def _zero_chunk(n=4):
    return np.zeros((n, 1), dtype=np.int16)


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        self._sd_was_present = "sounddevice" in sys.modules
        self._sd_saved = sys.modules.get("sounddevice")
        self.fake_sd = types.ModuleType("sounddevice")
        sys.modules["sounddevice"] = self.fake_sd

    def tearDown(self):
        if self._sd_was_present:
            sys.modules["sounddevice"] = self._sd_saved
        else:
            sys.modules.pop("sounddevice", None)

    def _run_loop(self, chunks, generation=None, oww=None):
        """Синхронный прогон _listen_loop с фейковым стримом."""
        self.adapter._stop_event.clear()
        stream = _FakeStream(chunks, self.adapter._stop_event)
        self.fake_sd.InputStream = lambda **kw: stream
        self.adapter._oww = oww if oww is not None else _FakeOWW()
        gen = generation if generation is not None else self.adapter._generation
        self.adapter._listen_loop(
            threshold=0.5, chunk_size=4, sample_rate=16000, generation=gen,
        )
        return stream

    def _snapshotting_oww(self):
        """Heartbeat читается ВО ВРЕМЯ работы цикла (watchdog/status);
        после выхода цикл легитимно чистит сессию (chip Finding 3) —
        поэтому наблюдаем штампы снапшотами из predict()."""
        adapter = self.adapter

        class _SnapshottingOWW:
            snapshots: list = []

            def predict(self, arr):
                type(self).snapshots.append(adapter.heartbeat())
                return {}

        _SnapshottingOWW.snapshots = []
        return _SnapshottingOWW()

    def test_nonzero_chunk_stamps_heartbeat(self):
        oww = self._snapshotting_oww()
        self._run_loop([_nonzero_chunk()], oww=oww)
        during = oww.snapshots[0]
        self.assertIsNotNone(during["listen_started_ts"])
        self.assertIsNotNone(during["last_chunk_ts"])
        # После выхода цикла сессия зачищена (post-exit cleanup, Finding 3).
        hb = self.adapter.heartbeat()
        self.assertIsNone(hb["last_chunk_ts"])
        self.assertIsNone(hb["listen_started_ts"])

    def test_zero_chunks_do_not_stamp_heartbeat(self):
        oww = self._snapshotting_oww()
        self._run_loop([_zero_chunk(), _zero_chunk()], oww=oww)
        for during in oww.snapshots:
            self.assertIsNotNone(during["listen_started_ts"])
            self.assertIsNone(during["last_chunk_ts"])

    def test_stale_generation_exits_loop_early(self):
        # Поколение адаптера ушло вперёд — «зомби»-тред обязан выйти,
        # не дочитав все чанки (проверяем по счётчику reads: 1, не 3).
        self.adapter._generation = 5
        stream = self._run_loop(
            [_nonzero_chunk(), _nonzero_chunk(), _nonzero_chunk()],
            generation=4,
        )
        self.assertEqual(stream.reads, 1)

    def test_start_resets_heartbeat_and_wedged(self):
        self.adapter._last_chunk_ts = 123.0
        self.adapter._listen_started_ts = 120.0
        self.adapter.set_wedged(True)
        # Прямой вызов внутренностей start() невозможен без библиотеки —
        # проверяем контракт через _reset_session_state(), который start()
        # обязан вызывать под локом.
        self.adapter._reset_session_state()
        hb = self.adapter.heartbeat()
        self.assertIsNone(hb["last_chunk_ts"])
        self.assertIsNone(hb["listen_started_ts"])
        self.assertFalse(self.adapter.is_wedged())

    def test_real_start_wires_reset_session_state(self):
        # Интеграционная проводка: НАСТОЯЩИЙ start() обязан звать
        # _reset_session_state() (класс «test-validates-the-hole» —
        # хелпер-тест выше зелёный, даже если вызов из start() удалить).
        self.adapter._last_chunk_ts = 123.0
        self.adapter._listen_started_ts = 120.0
        self.adapter.set_wedged(True)
        self.adapter._oww_available = True  # обходим проверку установленности либы
        # Пустой фейковый стрим: первый read() взводит stop_event и отдаёт
        # нули — спавнутый тред выходит сам, не штампуя last_chunk_ts.
        self.fake_sd.InputStream = lambda **kw: _FakeStream(
            [], self.adapter._stop_event
        )
        with patch.object(self.adapter, "_load_model", return_value=_FakeOWW()):
            self.adapter.start("hey_jarvis", on_detected=lambda n, s: None)
        try:
            thread = self.adapter._thread
            if thread is not None:
                thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())
            hb = self.adapter.heartbeat()
            # Стейл 123.0 обязан быть сброшен; нулевой стрим не штампует.
            self.assertIsNone(hb["last_chunk_ts"])
            # Стейл 120.0 сброшен start(), затем перештампован свежим
            # monotonic самим циклом.
            self.assertNotEqual(hb["listen_started_ts"], 120.0)
            self.assertFalse(self.adapter.is_wedged())
        finally:
            self.adapter.stop()  # не утекаем тред в tearDown/соседние тесты


class _FakeThreadCleanExit:
    """Duck-type (НЕ наследник threading.Thread — atexit-hang правило).
    Жив до join, после join — вышел."""

    def __init__(self):
        self._alive = True
        self.join_timeout = None

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_timeout = timeout
        self._alive = False


class _FakeThreadHung(_FakeThreadCleanExit):
    def join(self, timeout=None):
        self.join_timeout = timeout  # остаётся _alive=True


class StopReturnsBoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_stop_returns_true_when_not_running(self):
        self.assertTrue(self.adapter.stop())

    def test_stop_returns_true_on_clean_exit(self):
        self.adapter._thread = _FakeThreadCleanExit()
        self.assertTrue(self.adapter.stop())

    def test_stop_returns_false_when_thread_hung(self):
        fake = _FakeThreadHung()
        self.adapter._thread = fake
        self.assertFalse(self.adapter.stop())
        self.assertEqual(fake.join_timeout, 3.0)

    def test_hung_thread_reference_is_preserved_and_restart_is_rejected(self):
        """Зависший CFFI-тред нельзя потерять и заменить новым listener'ом."""
        fake = _FakeThreadHung()
        self.adapter._thread = fake
        self.adapter._active_model = "hey_jarvis"
        self.adapter._oww_available = True
        self.adapter._load_model = MagicMock()

        self.assertFalse(self.adapter.stop(timeout=0.01))
        self.assertIs(self.adapter._thread, fake)
        self.assertTrue(self.adapter.is_running())
        self.assertTrue(self.adapter.is_wedged())

        with self.assertRaisesRegex(RuntimeError, "предыдущий поток"):
            self.adapter.start("hey_jarvis", lambda _name, _score: None)
        self.adapter._load_model.assert_not_called()
        self.assertIs(self.adapter._thread, fake)

    def test_late_exit_of_previously_hung_thread_can_be_reaped(self):
        """Повторный stop очищает ссылку, если PortAudio всё же отвис позже."""
        fake = _FakeThreadHung()
        self.adapter._thread = fake

        self.assertFalse(self.adapter.stop(timeout=0.01))
        fake._alive = False

        self.assertTrue(self.adapter.stop(timeout=0.01))
        self.assertIsNone(self.adapter._thread)

    def test_stop_bumps_epoch_even_when_not_running(self):
        # Chip Finding 5: во время танца слушатель остановлен координатором,
        # и toggle-off приходит именно no-op stop'ом — он ОБЯЗАН двигать epoch.
        e0 = self.adapter.stop_epoch()
        self.assertTrue(self.adapter.stop())
        self.assertEqual(self.adapter.stop_epoch(), e0 + 1)
        self.assertTrue(self.adapter.stop())
        self.assertEqual(self.adapter.stop_epoch(), e0 + 2)

    def test_stop_on_self_died_thread_clears_session_state(self):
        # Тред умер сам (exception-путь не чистит model) → штатный stop()
        # обязан снять сигнатуру «мёртвой сессии», иначе watchdog ложно
        # эскалирует dead_session на выключенном тумблере (re-review Task 4).
        dead = _FakeThreadCleanExit()
        dead._alive = False              # уже мёртв ДО stop()
        self.adapter._thread = dead
        self.adapter._active_model = "hey_jarvis"
        self.adapter._active_threshold = 0.5
        self.assertTrue(self.adapter.stop())
        self.assertIsNone(self.adapter.active_model())
        self.assertIsNone(self.adapter.active_threshold())
        self.assertFalse(self.adapter.is_running())


class StatusFieldsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_status_contains_additive_fields(self):
        result = self.adapter.handle_wake_word_status({})
        self.assertTrue(result["ok"])
        self.assertIn("last_chunk_ts", result)
        self.assertIn("listen_started_ts", result)
        self.assertIn("wedged", result)
        self.assertFalse(result["wedged"])

    def test_set_wedged_roundtrip(self):
        self.adapter.set_wedged(True)
        self.assertTrue(self.adapter.is_wedged())
        self.assertTrue(self.adapter.handle_wake_word_status({})["wedged"])
        self.adapter.set_wedged(False)
        self.assertFalse(self.adapter.is_wedged())


class MaintenanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_start_refused_during_maintenance(self):
        self.adapter._oww_available = True
        self.adapter.begin_maintenance()
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.start("hey_jarvis", lambda n, s: None)
        self.assertIn("обслуживанием", str(ctx.exception))

    def test_handle_start_returns_ok_false_during_maintenance(self):
        self.adapter._oww_available = True
        self.adapter.begin_maintenance()
        result = self.adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"])

    def test_end_maintenance_reopens_start(self):
        self.adapter.begin_maintenance()
        self.adapter.end_maintenance()
        # без библиотеки start() падает ПО ДРУГОЙ причине (oww не установлен)
        self.adapter._oww_available = False
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.start("hey_jarvis", lambda n, s: None)
        self.assertIn("openwakeword", str(ctx.exception).lower())


class _RaisingStream:
    """Контекст-менеджер, падающий на входе — синхронная ошибка открытия
    микрофона (класс circuit breaker'а, KRAB-EAR-BACKEND-1J)."""

    @property
    def read_available(self) -> int:
        # см. _FakeStream.read_available
        return 1 << 20

    def __enter__(self):
        raise RuntimeError("mic busy")

    def __exit__(self, *exc):
        return False


class LoopExitCleanupTests(unittest.TestCase):
    """Chip Finding 3 (Fable-гейт волны watchdog): смерть цикла не должна
    оставлять сигнатуру «мёртвой сессии» (running=False, model!=None) —
    иначе класс мгновенных падений старта, который до волны тихо гасился
    circuit-breaker'ом, получает kickstart вместо cooldown."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        self._sd_was_present = "sounddevice" in sys.modules
        self._sd_saved = sys.modules.get("sounddevice")
        self.fake_sd = types.ModuleType("sounddevice")
        sys.modules["sounddevice"] = self.fake_sd

    def tearDown(self):
        if self._sd_was_present:
            sys.modules["sounddevice"] = self._sd_saved
        else:
            sys.modules.pop("sounddevice", None)

    def _seed_session(self, generation=1, model="hey_jarvis"):
        """Состояние, которое start() оставляет перед спавном треда."""
        self.adapter._generation = generation
        self.adapter._active_model = model
        self.adapter._active_threshold = 0.5
        self.adapter._oww = _FakeOWW()
        self.adapter._stop_event.clear()

    def _assert_session_cleared(self):
        self.assertIsNone(self.adapter.active_model())
        self.assertIsNone(self.adapter.active_threshold())
        self.assertIsNone(self.adapter._oww)
        hb = self.adapter.heartbeat()
        self.assertIsNone(hb["last_chunk_ts"])
        self.assertIsNone(hb["listen_started_ts"])

    def test_exception_death_clears_session_state(self):
        self._seed_session()
        self.fake_sd.InputStream = lambda **kw: _RaisingStream()
        self.adapter._listen_loop(
            threshold=0.5, chunk_size=4, sample_rate=16000, generation=1,
        )
        self._assert_session_cleared()
        # Существующая circuit-breaker семантика не потеряна.
        self.assertEqual(self.adapter._consecutive_stream_failures, 1)
        # wedged — домен watchdog'а, cleanup его не трогает.
        self.assertFalse(self.adapter.is_wedged())

    def test_zombie_exception_death_does_not_clobber_new_session(self):
        # Новая сессия (generation=5) владеет полями; зомби старого
        # поколения умирает с исключением — поля новой сессии целы.
        self._seed_session(generation=5, model="krab_ru")
        self.adapter._last_chunk_ts = 111.0
        self.adapter._listen_started_ts = 110.0
        self.fake_sd.InputStream = lambda **kw: _RaisingStream()
        self.adapter._listen_loop(
            threshold=0.5, chunk_size=4, sample_rate=16000, generation=4,
        )
        self.assertEqual(self.adapter.active_model(), "krab_ru")
        self.assertEqual(self.adapter.active_threshold(), 0.5)
        self.assertIsNotNone(self.adapter._oww)
        # listen_started_ts перештампован зомби на входе в цикл — известная
        # косметика (стартовый штамп идёт до generation-проверки); несущие
        # поля сессии зомби не тронул.
        self.assertEqual(self.adapter._last_chunk_ts, 111.0)

    def test_privacy_break_clears_session_state(self):
        # Backend-side privacy-флип (без Swift-stop): цикл выходит по
        # _privacy_blocked — сигнатуры «мёртвой сессии» остаться не должно,
        # иначе watchdog эскалировал бы kickstart при ВКЛЮЧЁННОМ privacy.
        adapter = OpenWakeWordAdapter(
            data_dir=self.tmp,
            settings_get=lambda k, d: True if k == "privacy_mode_enabled" else d,
        )
        adapter._generation = 1
        adapter._active_model = "hey_jarvis"
        adapter._active_threshold = 0.5
        adapter._oww = _FakeOWW()
        stream = _FakeStream([_nonzero_chunk()], adapter._stop_event)
        self.fake_sd.InputStream = lambda **kw: stream
        adapter._listen_loop(
            threshold=0.5, chunk_size=4, sample_rate=16000, generation=1,
        )
        self.assertIsNone(adapter.active_model())
        self.assertIsNone(adapter._oww)
        self.assertEqual(stream.reads, 0)  # privacy-гард сработал до чтения

    def test_import_error_path_clears_session_state(self):
        self._seed_session()
        sys.modules["sounddevice"] = None  # import поднимет ImportError
        self.adapter._listen_loop(
            threshold=0.5, chunk_size=4, sample_rate=16000, generation=1,
        )
        self._assert_session_cleared()


if __name__ == "__main__":
    unittest.main()
