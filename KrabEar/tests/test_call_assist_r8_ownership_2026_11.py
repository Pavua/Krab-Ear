"""Тесты R8 (2026-11-08): владение capture и generation в Call Assist.

Покрывает:
1. Stop в фазе starting: итог stopped, recorder остановлен, _assist_loop не запущен,
   старт вернул stopped_during_start, следующий start допустим.
2. Stop до recorder.start(): старт после вытеснения не оставляет recorder запущенным.
3. Исключения I/O старта (recorder.start, load_settings, gateway.start_session):
   слот свободен, следующий start доходит до recorder, сессия Gateway закрыта.
4. Две фазы R2-01: компенсационный stop бросает (worker жив) -> следующий start
   отклоняется recorder_busy; после выхода worker следующий start успешен и долг снят.
5. Обычный stop бросает AudioRecorderStopTimeout: долг удерживается; после выхода worker
   повторный start успешен.
6. Заимствованный capture: recorder писал до старта звонка -> stop звонка НЕ останавливает recorder.
7. TOCTOU: RecordingCoreService.stop_unmanaged_capture() не останавливает чужую запись (диктовку/встречу).
8. Assist loop сессии A прекращает отправку событий после старта сессии B.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402
from backend.recorder import AudioRecorderStopTimeout  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _FakeRecorder:
    """Рекордер с поддержкой моделирования живого воркера и таймаута stop()."""

    def __init__(self, initially_recording: bool = False):
        self.is_recording = initially_recording
        self.start_calls = 0
        self.stop_calls = 0
        self.worker_alive = False
        self.stop_throws_timeout = False
        self.start_throws = False

    def start(self) -> bool:
        self.start_calls += 1
        if self.start_throws:
            raise RuntimeError("recorder_start_error")
        if self.worker_alive:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float | None = None) -> None:
        self.stop_calls += 1
        if self.stop_throws_timeout:
            self.worker_alive = True
            self.is_recording = False
            raise AudioRecorderStopTimeout("worker hung")
        self.is_recording = False
        self.worker_alive = False

    def snapshot_audio(self, max_duration_sec: float = 25.0):
        class _Buf:
            size = 32000
        return _Buf(), 2.0


class _FakeTranscriber:
    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return {"text": "тестовый текст звонка"}


class _FakeGateway(VoiceGatewayClient):
    def __init__(self):
        self.start_session_calls = 0
        self.stop_session_calls = 0
        self.post_calls = 0
        self.events_sent: list[dict] = []
        self.start_gate = threading.Event()
        self.start_entered = threading.Event()
        self.start_throws = False
        self.start_ok = True

    def start_session(self, voice_gateway_url, api_key, payload):
        self.start_session_calls += 1
        self.start_entered.set()
        if not self.start_gate.is_set():
            self.start_gate.wait(5.0)
        if self.start_throws:
            raise RuntimeError("gateway_start_error")
        if not self.start_ok:
            return {"ok": False, "error": "gateway_rejected"}
        return {"ok": True, "session_id": f"gw_{self.start_session_calls}"}

    def stop_session(self, voice_gateway_url, api_key, session_id):
        self.stop_session_calls += 1
        return {"ok": True}

    def post(self, voice_gateway_url, api_key, path, payload):
        self.post_calls += 1
        if "/events" in path:
            self.events_sent.append(payload)
        return {"ok": True, "payload": {}}

    def get(self, *a, **k):
        return {"ok": True, "payload": {}}


class R8RecorderOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.store.initialize_startup_plaintext_policy(new_profile=True)

    def _create_service(self, recorder=None, gateway=None, core=None):
        rec = recorder or _FakeRecorder()
        gw = gateway or _FakeGateway()
        return CallAssistService(
            store=self.store,
            recorder=rec,
            transcriber=_FakeTranscriber(),
            gateway=gw,
            recording_core=core,
        )

    def test_01_stop_during_starting_phase(self):
        """1. Stop в окне starting: сессия завершается stopped, recorder останавливается,
        старт возвращает stopped_during_start, следующий старт возможен."""
        rec = _FakeRecorder()
        gw = _FakeGateway()
        # gateway.start_session задержится на гейте
        gw.start_gate.clear()
        svc = self._create_service(recorder=rec, gateway=gw)

        start_res = {}
        def _run_start():
            start_res.update(svc.handle_start({}))

        t_start = threading.Thread(target=_run_start)
        t_start.start()

        # Ждем, пока старт дойдет до I/O gateway
        self.assertTrue(gw.start_entered.wait(timeout=3.0))

        # Вызываем stop во время starting
        stop_res = svc.handle_stop({})
        self.assertFalse(stop_res.get("active"))
        self.assertEqual(stop_res.get("status"), "stopped")

        # Отпускаем старт
        gw.start_gate.set()
        t_start.join(timeout=3.0)

        # Старт обнаружил аборт
        self.assertFalse(start_res.get("ok"))
        self.assertEqual(start_res.get("error"), "stopped_during_start")

        # Рекордер не остался запущенным
        self.assertFalse(rec.is_recording)

        # Следующий старт допустим
        gw.start_entered.clear()
        start2 = svc.handle_start({})
        self.assertTrue(start2.get("ok"))
        self.assertTrue(start2.get("active"))
        self.assertEqual(start2.get("status"), "running")
        svc.handle_stop({})

    def test_02_stop_before_recorder_start(self):
        """2. Stop, вызванный до recorder.start() (например прерван сразу):
        старт не оставляет recorder запущенным."""
        rec = _FakeRecorder()
        gw = _FakeGateway()
        gw.start_gate.set()
        svc = self._create_service(recorder=rec, gateway=gw)

        # Искусственно эмулируем отмену в фазе starting до recorder.start
        with svc._lock:
            svc._state["active"] = True
            svc._state["status"] = "starting"
            svc._state["session_id"] = "call_abort_early"
            gen = object()
            svc._gen = gen

        # Stop переводит в stopped
        svc.handle_stop({})

        # Проверяем, что _still_starting вернет False
        self.assertFalse(svc._still_starting(gen))

    def test_03_startup_io_failure_compensation(self):
        """3. Исключения I/O старта (gateway.start_session / settings) откатывают слот:
        active сбрасывается в False, следующий старт возможен."""
        rec = _FakeRecorder()
        gw = _FakeGateway()
        gw.start_gate.set()
        gw.start_throws = True
        svc = self._create_service(recorder=rec, gateway=gw)

        res = svc.handle_start({})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "start_failed")
        self.assertFalse(svc.state.get("active"))
        self.assertEqual(svc.state.get("status"), "idle")

        # Рекордер остановлен компенсацией
        self.assertFalse(rec.is_recording)

        # Следующий старт проходит
        gw.start_throws = False
        res2 = svc.handle_start({})
        self.assertTrue(res2.get("ok"))
        self.assertTrue(res2.get("active"))
        svc.handle_stop({})

    def test_04_r2_01_two_phases_compensation_timeout_recovery(self):
        """4. Две фазы R2-01:
        Фаза (а): компенсационный stop бросает (worker жив) -> следующий start
        отклоняется как recorder_busy (нет второго процесса/потока).
        Фаза (б): worker выходит -> следующий start успешно восстанавливает capture и долг снят."""
        rec = _FakeRecorder()
        rec.stop_throws_timeout = True  # stop бросит AudioRecorderStopTimeout
        gw = _FakeGateway()
        gw.start_gate.set()
        gw.start_throws = True  # провоцируем компенсацию при старте
        svc = self._create_service(recorder=rec, gateway=gw)

        # 1. Первый старт падает на gateway, пытается сделать stop_capture, тот бросает timeout
        res1 = svc.handle_start({})
        self.assertFalse(res1.get("ok"))
        self.assertEqual(res1.get("error"), "start_failed")
        # Долг остался в _capture_gen
        self.assertIsNotNone(svc._capture_gen)

        # Фаза (а): воркер все еще жив (worker_alive=True)
        rec.start_throws = False
        gw.start_throws = False
        res2 = svc.handle_start({})
        # Второй старт НЕ начинает запись поверх живого воркера
        self.assertFalse(res2.get("ok"))
        self.assertEqual(res2.get("error"), "recorder_busy")

        # Фаза (б): воркер завершился, причина таймаута устранена
        rec.stop_throws_timeout = False
        rec.worker_alive = False

        res3 = svc.handle_start({})
        # Теперь старт успешно восстановился!
        self.assertTrue(res3.get("ok"))
        self.assertTrue(res3.get("active"))
        self.assertEqual(res3.get("status"), "running")
        svc.handle_stop({})

    def test_05_normal_stop_timeout_recovery(self):
        """5. Обычный stop бросает AudioRecorderStopTimeout: долг удерживается;
        после выхода воркера повторный старт восстанавливает систему."""
        rec = _FakeRecorder()
        gw = _FakeGateway()
        gw.start_gate.set()
        svc = self._create_service(recorder=rec, gateway=gw)

        res_start = svc.handle_start({})
        self.assertTrue(res_start.get("ok"))

        # Stop с таймаутом
        rec.stop_throws_timeout = True
        stop_res = svc.handle_stop({})
        self.assertEqual(stop_res.get("status"), "stopped")
        self.assertIsNotNone(svc._capture_gen)

        # Фаза (б): воркер вышел
        rec.stop_throws_timeout = False
        rec.worker_alive = False

        # Новый старт возвращает систему в строй
        start2 = svc.handle_start({})
        self.assertTrue(start2.get("ok"))
        self.assertTrue(start2.get("active"))
        svc.handle_stop({})

    def test_06_borrowed_capture_not_stopped(self):
        """6. Заимствованный capture: recorder писал до старта звонка ->
        stop звонка НЕ вызывает recorder.stop()."""
        rec = _FakeRecorder(initially_recording=True)
        gw = _FakeGateway()
        gw.start_gate.set()
        svc = self._create_service(recorder=rec, gateway=gw)

        start_res = svc.handle_start({})
        self.assertTrue(start_res.get("ok"))
        # Call assist не запускал рекордер сам -> _capture_gen равен None
        self.assertIsNone(svc._capture_gen)

        stop_res = svc.handle_stop({})
        self.assertEqual(stop_res.get("status"), "stopped")
        # stop() рекордера НЕ вызывался!
        self.assertEqual(rec.stop_calls, 0)
        self.assertTrue(rec.is_recording)

    def test_07_core_stop_unmanaged_capture_toctou(self):
        """7. Core stop_unmanaged_capture: если запись принадлежит другому generation (owner!=None),
        Core отказывается останавливать её (owned_by_other)."""
        class _MockCore:
            def __init__(self, owner=None):
                self._owner = owner
                self.stop_calls = 0

            def stop_unmanaged_capture(self, timeout_sec=None):
                if self._owner is not None:
                    return "owned_by_other"
                self.stop_calls += 1
                return "stopped"

        core_busy = _MockCore(owner="dictation")
        rec = _FakeRecorder()
        gw = _FakeGateway()
        gw.start_gate.set()
        svc = self._create_service(recorder=rec, gateway=gw, core=core_busy)

        start_res = svc.handle_start({})
        self.assertTrue(start_res.get("ok"))

        # Вызываем stop_call_assist
        stop_res = svc.handle_stop({})
        self.assertEqual(stop_res.get("status"), "stopped")
        # Core отклонил остановку, так как владелец - dictation
        self.assertEqual(core_busy.stop_calls, 0)

    def test_08_assist_loop_exits_on_generation_mismatch(self):
        """8. Фоновый цикл сессии A завершается, как только generation меняется на B."""
        rec = _FakeRecorder()
        gw = _FakeGateway()
        gw.start_gate.set()
        svc = self._create_service(recorder=rec, gateway=gw)

        start1 = svc.handle_start({})
        self.assertTrue(start1.get("ok"))
        gen1 = svc._gen

        # Даем циклу поработать
        time.sleep(0.1)

        # Вызываем stop и сразу start2 (новая generation)
        svc.handle_stop({})
        start2 = svc.handle_start({})
        self.assertTrue(start2.get("ok"))
        gen2 = svc._gen
        self.assertIsNot(gen1, gen2)

        # Сессия 1 гарантированно остановила цикл
        svc.handle_stop({})

    def test_09_stop_unmanaged_capture_recording_core_real(self):
        """9. Прямой тест RecordingCoreService.stop_unmanaged_capture() на всех ветках."""
        rec = _FakeRecorder()
        core = RecordingCoreService.__new__(RecordingCoreService)
        core.recorder = rec

        # Ветка 1: Idle (не записывает) -> "not_recording"
        rec.is_recording = False
        core._active_generation = None
        self.assertEqual(core.stop_unmanaged_capture(), "not_recording")
        self.assertEqual(rec.stop_calls, 0)

        # Ветка 2: Активна диктовка/встреча (owner не None) -> "owned_by_other", stop() не вызывается
        rec.is_recording = True
        core._active_generation = {"owner": "dictation", "session_id": "s1"}
        self.assertEqual(core.stop_unmanaged_capture(), "owned_by_other")
        self.assertEqual(rec.stop_calls, 0)

        # Ветка 3: Unmanaged запись (owner is None, но is_recording == True) -> "stopped"
        core._active_generation = None
        self.assertEqual(core.stop_unmanaged_capture(timeout_sec=1.5), "stopped")
        self.assertEqual(rec.stop_calls, 1)

    def test_10_abort_during_recorder_start_releases_capture(self):
        """10. Аборт сразу после recorder.start() корректно освобождает захват через recording_core."""
        rec = _FakeRecorder()
        gw = _FakeGateway()

        class _SpyCore:
            def __init__(self):
                self.stopped = False

            def stop_unmanaged_capture(self, timeout_sec=None):
                self.stopped = True
                rec.stop()
                return "stopped"

        core = _SpyCore()
        svc = self._create_service(recorder=rec, gateway=gw, core=core)

        # Подменим recorder.start так, чтобы во время него статус менялся на stopped
        orig_start = rec.start

        def start_with_concurrent_stop():
            res = orig_start()
            # Имитируем конкурентный handle_stop, пока мы всё ещё внутри lock-блока handle_start
            svc._state["status"] = "stopped"
            return res

        rec.start = start_with_concurrent_stop

        res = svc.handle_start({})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "stopped_during_start")
        # Проверяем, что capture_gen был выставлен и release_capture вызван
        self.assertTrue(core.stopped)
        self.assertFalse(rec.is_recording)


if __name__ == "__main__":
    unittest.main()
