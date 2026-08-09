"""AudioRecorder.is_worker_thread_alive — гонка «is_recording лжёт после
таймаута stop()» (задача #12, разбор PortAudio-сегфолта от 2026-08-07).

## Разбор (не догадка — цепочка вызовов прочитана построчно)

`AudioReinitCoordinator._dance()` не должен звать `Pa_Terminate` (через
`sd._terminate()`), пока recorder держит живой PortAudio-стрим — сам код это
явно документирует как crash-класс (audio_reinit.py, комментарии про
«Pa_Terminate под живым стримом рекордера — тот же crash-класс, что и
THREAD_HUNG-инвариант»). Единственный гейт против этого — колбэк
`is_recording`, переданный в конструктор (`service.py`):

    is_recording=lambda: bool(getattr(self.recorder, "is_recording", False))

`AudioRecorder.is_recording` — чистый флаг `self._is_recording`
(`recorder.py::is_recording` property). А в `AudioRecorder.stop()`
`self._is_recording = False` выставляется на строке ~226, ДО попытки
`thread.join()`. Если join таймаутит (`stream.read()` завис внутри
PortAudio — ровно паттерн из sample'а живого крэша 2026-08-07,
`Thread-4 (_worker)` внутри `PaUtil_ReadRingBuffer`), метод кидает
`AudioRecorderStopTimeout`, но `self._thread` НЕ обнуляется в этой ветке —
воркер физически жив и заблокирован, а `is_recording` уже `False`.

Итог: если ЛЮБОЙ триггер reinit (wake-word watchdog staleness,
AudioSelfHealer на пустых диктовках) сработает в этом окне, единственный
гейт-проверка увидит `is_recording() == False` и пропустит `Pa_Terminate`
прямо на живой, физически блокированный стрим рекордера — ровно тот
crash-класс, от которого код сам себя пытается защитить.

## Фикс

Отдельное свойство `is_worker_thread_alive` — физическая живость потока,
НЕЗАВИСИМАЯ от пользовательской семантики `is_recording`. Другие
потребители `recorder.is_recording` (call_assist_service, meeting_session_service,
realtime_silence_filter, recording_duration_watchdog) НЕ меняются: им нужна
именно пользовательская семантика «идёт диктовка», не физическая живость
потока после таймаута. Комбинируется ТОЛЬКО в safety-гейте
`AudioReinitCoordinator` через `BackendService._reinit_is_recording_gate()`.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap
import threading
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.recorder import AudioRecorder, AudioRecorderStopTimeout  # noqa: E402


class IsRecordingLiesAfterStopTimeoutTest(unittest.TestCase):
    """Пин факта: is_recording падает в False, пока воркер ещё физически жив.

    Это НЕ баг recorder.stop() самого по себе (не хочет отдавать частичное
    аудио под has_pending) — это документирует ОПАСНОЕ побочное следствие
    для любого кода, использующего is_recording как safety-гейт против
    Pa_Terminate. Тест не проверяет фикс — фиксирует посылку, на которой
    фикс основан.
    """

    def test_is_recording_false_while_thread_still_alive(self) -> None:
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = stuck_thread
        stuck_thread.start()
        self.addCleanup(stuck_thread.join, 1.0)
        self.addCleanup(release.set)

        with self.assertRaises(AudioRecorderStopTimeout):
            rec.stop(timeout_sec=0.01)

        self.assertFalse(
            rec.is_recording,
            "is_recording обязан упасть после timeout — иначе следующий "
            "stop() не отличит его от активной записи",
        )
        with rec._lock:
            self.assertTrue(
                rec._thread is not None and rec._thread.is_alive(),
                "воркер обязан остаться физически живым сразу после "
                "timeout — иначе тест не воспроизводит гонку",
            )


class IsWorkerThreadAliveSurvivesStopTimeoutTest(unittest.TestCase):
    """Новое свойство: честный сигнал физической живости потока."""

    def test_true_while_worker_blocked_normally(self) -> None:
        rec = AudioRecorder()
        release = threading.Event()
        thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._thread = thread
        thread.start()
        self.addCleanup(thread.join, 1.0)
        self.addCleanup(release.set)

        self.assertTrue(rec.is_worker_thread_alive)

    def test_false_when_no_thread_was_ever_started(self) -> None:
        rec = AudioRecorder()
        self.assertFalse(rec.is_worker_thread_alive)

    def test_stays_true_after_stop_timeout_when_is_recording_already_false(self) -> None:
        """🔴 Ядро фикса: ровно окно, где старый гейт был слеп."""
        rec = AudioRecorder()
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._started_at = time.monotonic()
            rec._thread = stuck_thread
        stuck_thread.start()
        self.addCleanup(stuck_thread.join, 1.0)
        self.addCleanup(release.set)

        with self.assertRaises(AudioRecorderStopTimeout):
            rec.stop(timeout_sec=0.01)

        self.assertFalse(rec.is_recording, "предпосылка: is_recording уже упал")
        self.assertTrue(
            rec.is_worker_thread_alive,
            "is_worker_thread_alive обязан оставаться True — иначе "
            "AudioReinitCoordinator снова слеп к живому стриму рекордера",
        )

    def test_becomes_false_after_worker_actually_exits(self) -> None:
        rec = AudioRecorder()
        release = threading.Event()
        thread = threading.Thread(target=release.wait, daemon=True)
        with rec._lock:
            rec._is_recording = True
            rec._thread = thread
        thread.start()
        self.addCleanup(thread.join, 1.0)

        self.assertTrue(rec.is_worker_thread_alive)
        release.set()
        thread.join(timeout=1.0)

        self.assertFalse(
            rec.is_worker_thread_alive,
            "живой сигнал не должен застревать True после реального выхода потока",
        )


class _FakeRecorder:
    """Лёгкая заглушка вместо реального AudioRecorder — тестируем ТОЛЬКО
    комбинирование двух булевых сигналов, конструировать BackendService
    для этого не нужно и вредно (демон-треды + обязательный close() в
    tearDown — хронический источник CI-флейка чанков)."""

    def __init__(self, is_recording: bool, is_worker_thread_alive: bool):
        self.is_recording = is_recording
        self.is_worker_thread_alive = is_worker_thread_alive


class ReinitIsRecordingGateTest(unittest.TestCase):
    """BackendService._reinit_is_recording_gate — комбинирует оба сигнала."""

    def _gate(self, is_recording: bool, is_worker_thread_alive: bool) -> bool:
        from backend.service import BackendService

        stub = type("Stub", (), {})()
        stub.recorder = _FakeRecorder(is_recording, is_worker_thread_alive)
        return BackendService._reinit_is_recording_gate(stub)

    def test_true_when_recording_flag_set(self) -> None:
        self.assertTrue(self._gate(is_recording=True, is_worker_thread_alive=False))

    def test_true_when_worker_thread_alive_even_if_flag_already_false(self) -> None:
        """🔴 Ядро фикса: ровно окно после stop()-таймаута."""
        self.assertTrue(self._gate(is_recording=False, is_worker_thread_alive=True))

    def test_false_when_neither_signal_set(self) -> None:
        self.assertFalse(self._gate(is_recording=False, is_worker_thread_alive=False))

    def test_true_when_both_signals_set(self) -> None:
        """Ревью 2026-08-09 (F3): без этого случая XOR-мутант гейта
        (``is_recording != is_worker_thread_alive`` вместо ``or``) проходит
        все остальные тесты класса незамеченным — оба сигнала одновременно
        True вполне реальны (например, воркер жив И is_recording ещё не
        успел упасть до re-check внутри AudioReinitCoordinator)."""
        self.assertTrue(self._gate(is_recording=True, is_worker_thread_alive=True))

    def test_never_raises_when_recorder_attribute_missing(self) -> None:
        """fail-safe: отсутствие атрибута трактуем как False, не как исключение —
        исключение здесь оборвало бы конструктор BackendService целиком."""
        from backend.service import BackendService

        stub = type("Stub", (), {})()
        stub.recorder = object()  # ни is_recording, ни is_worker_thread_alive
        self.assertFalse(BackendService._reinit_is_recording_gate(stub))


class ReinitCoordinatorWiringSourceContractTest(unittest.TestCase):
    """AST-контракт: AudioReinitCoordinator получает именно gate-методы.

    Голая лямбда read-only recorder.is_recording — это и был баг; регрессия
    на неё должна ронять этот тест, а не молча вернуться при следующей правке
    рядом.
    """

    @staticmethod
    def _find_coordinator_call(tree: ast.AST) -> "ast.Call | None":
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AudioReinitCoordinator"
            ):
                return node
        return None

    def _assert_kwarg_is_self_attr(
        self, call: ast.Call, kwarg_name: str, expected_attr: str
    ) -> None:
        """Ревью 2026-08-09 (F4): «не Lambda» одна не закрывает дыру —
        ``functools.partial(...)`` (ast.Call, не ast.Lambda) или ссылка на
        любой ДРУГОЙ метод/атрибут прошли бы старую проверку молча.
        Позитивно пиновать ИМЕННО ``self.<expected_attr>`` — единственная
        форма, которую нельзя обойти переименованием или обёрткой без
        того, чтобы этот тест упал."""
        kwarg = next((kw for kw in call.keywords if kw.arg == kwarg_name), None)
        self.assertIsNotNone(kwarg, f"{kwarg_name}= не передан")
        value = kwarg.value
        is_expected_self_attr = (
            isinstance(value, ast.Attribute)
            and value.attr == expected_attr
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
        )
        self.assertTrue(
            is_expected_self_attr,
            f"{kwarg_name}= обязан быть ссылкой на self.{expected_attr} "
            f"(без вызова) — получено {ast.dump(value)}. Голая лямбда, "
            "functools.partial или ссылка на другой метод — все три "
            "молча обходили бы прежнюю ast.Lambda-only проверку.",
        )

    def test_audio_reinit_coordinator_uses_gate_method_not_bare_lambda(self) -> None:
        from backend.service import BackendService

        source = textwrap.dedent(inspect.getsource(BackendService.__init__))
        tree = ast.parse(source)

        call = self._find_coordinator_call(tree)
        self.assertIsNotNone(call, "не нашли конструктор AudioReinitCoordinator")
        self._assert_kwarg_is_self_attr(
            call, "is_recording", "_reinit_is_recording_gate"
        )

    def test_audio_reinit_coordinator_wires_is_worker_hung_to_recorder_signal(
        self,
    ) -> None:
        """Ревью 2026-08-09 (F1): без ``is_worker_hung=`` координатор не
        может отличить заклинивший worker-тред от настоящей диктовки —
        DEFERRED_WORKER_HUNG никогда не вернётся, эскалация недостижима.
        Проверяем именно ЧТО передан non-lambda callable (реальная семантика
        уже покрыта behaviour-тестами ReinitOutcome в test_audio_reinit_coordinator.py)."""
        from backend.service import BackendService

        source = textwrap.dedent(inspect.getsource(BackendService.__init__))
        tree = ast.parse(source)

        call = self._find_coordinator_call(tree)
        self.assertIsNotNone(call, "не нашли конструктор AudioReinitCoordinator")
        kwarg = next((kw for kw in call.keywords if kw.arg == "is_worker_hung"), None)
        self.assertIsNotNone(
            kwarg,
            "is_worker_hung= не передан — координатор не отличит "
            "заклинивший worker-тред от настоящей диктовки",
        )


if __name__ == "__main__":
    unittest.main()
