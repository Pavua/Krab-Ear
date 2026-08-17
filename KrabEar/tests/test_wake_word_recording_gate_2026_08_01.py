"""Гейт активной записи в `OpenWakeWordAdapter.handle_wake_word_start`.

Живой инцидент 2026-08-01: владелец терял диктовки одну за другой. Лог backend
в момент потери (машина здорова, load 9, backend стабилен 54 мин):

    02:35:21 OpenWakeWordAdapter: уже запущен, сначала stop()   <- пришёл START
    02:35:21 остановлен
    02:35:37 запущен                                            <- ПОСРЕДИ ЗАПИСИ
    02:35:40 AudioRecorder worker не завершился за 3.0 с
    02:35:40 stop_recording: audio worker завис -> recorder_timeout

`wake_word_start` во время активной диктовки открывает ВТОРОЙ входной поток на
то же устройство. Два конкурирующих CoreAudio-тапа вешают worker'а
AudioRecorder насмерть, диктовка теряется целиком.

Источник вызова — не человек: `WakeWordPoller` (Swift) шлёт `wake_word_start`
своим self-heal каждые ~10 секунд. Это видно по частоте отклонений гейта F5 в
логе. Значит гейт обязан стоять в backend: он владеет микрофоном и один знает
про активную запись — ровно тот принцип, что уже зафиксирован в комментарии к
F5 («устаревший или сломанный агент не должен уметь открыть тап вопреки
настройке»). Это sibling-gate asymmetry: F2 (privacy) и F5 (настройка) есть,
а сиблинг «идёт запись» отсутствовал.

Направление отказа — FAIL-CLOSED: при сбое `is_recording()` тап НЕ открываем.
Цена ошибки несимметрична: ложный отказ = wake word не сработал (заметно,
обратимо), ложное разрешение = потерянная диктовка (необратимо). Отличие от
`WakeWordWatchdog`, где тот же колбэк fail-OPEN: там ошибка в другую сторону
отключила бы сторожа, здесь — сломала бы основную функцию.

Уровень лога — DEBUG, не INFO: поллер стучится каждые 10 секунд, и INFO
залил бы err.log мусором на каждой диктовке (F5 именно так и спамит).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402

_RECORDING_REASON = "recording in progress"


def _make_adapter(
    tmp_dir: str | Path,
    settings: dict | None = None,
    is_recording=None,
    is_start_blocked=None,
) -> OpenWakeWordAdapter:
    settings = settings or {"wake_word_enabled": True}
    kwargs = {
        "data_dir": tmp_dir,
        "settings_get": lambda k, d: settings.get(k, d),
    }
    if is_recording is not None:
        kwargs["is_recording"] = is_recording
    if is_start_blocked is not None:
        kwargs["is_start_blocked"] = is_start_blocked
    adapter = OpenWakeWordAdapter(**kwargs)
    # Движок недоступен: гейт обязан отработать РАНЬШЕ любой попытки открыть
    # микрофон, поэтому отсутствие openwakeword тесту не мешает.
    adapter._oww_available = False
    return adapter


class TestRecordingGateBlocksStart(unittest.TestCase):
    """Активная запись обязана блокировать открытие второго тапа."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_active_recording_blocks_wake_word_start(self) -> None:
        adapter = _make_adapter(self._tmp, is_recording=lambda: True)
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("reason"), _RECORDING_REASON)

    def test_idle_recorder_proceeds_past_the_gate(self) -> None:
        """Без записи гейт молчит — отказ приходит уже от движка."""
        adapter = _make_adapter(self._tmp, is_recording=lambda: False)
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotEqual(result.get("reason"), _RECORDING_REASON)

    def test_missing_callback_keeps_legacy_behaviour(self) -> None:
        """Без колбэка (старый вызывающий код) гейт не срабатывает."""
        adapter = _make_adapter(self._tmp, is_recording=None)
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotEqual(result.get("reason"), _RECORDING_REASON)


class TestStartBlockedWhileRecorderWorkerAlive(unittest.TestCase):
    """W7: после stop() флаг is_recording уже False, а worker ещё жив.

    Живой инцидент 2026-08-17: resume после диктовки слал wake_word_start,
    пока AudioRecorder.join() ещё не вышел / после recorder_timeout —
    второй InputStream на том же устройстве, THREAD_HUNG, PaMacCore -50.
    Reason тот же, что F6: поллер не жжёт бюджет self-heal.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_worker_alive_after_stop_blocks_wake_word_start(self) -> None:
        opened = {"n": 0}

        def _start_side_effect(*_a, **_k):
            opened["n"] += 1

        adapter = _make_adapter(
            self._tmp,
            is_recording=lambda: False,
            is_start_blocked=lambda: True,
        )
        adapter.start = _start_side_effect  # type: ignore[method-assign]
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("reason"), _RECORDING_REASON)
        self.assertEqual(opened["n"], 0, "второй InputStream/start() не должен вызываться")

    def test_idle_worker_does_not_use_start_blocked_reason(self) -> None:
        adapter = _make_adapter(
            self._tmp,
            is_recording=lambda: False,
            is_start_blocked=lambda: False,
        )
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertNotEqual(result.get("reason"), _RECORDING_REASON)


class TestRecordingGateFailsClosed(unittest.TestCase):
    """Сбой колбэка не должен открывать микрофон под запись."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_callback_raising_blocks_start(self) -> None:
        def _boom() -> bool:
            raise RuntimeError("recorder недоступен")

        adapter = _make_adapter(self._tmp, is_recording=_boom)
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result.get("reason"), _RECORDING_REASON)

    def test_callback_failure_is_logged_loudly(self) -> None:
        """Сбой колбэка — WARNING: тихий fail-closed навсегда убил бы wake word."""

        def _boom() -> bool:
            raise RuntimeError("recorder недоступен")

        adapter = _make_adapter(self._tmp, is_recording=_boom)
        with self.assertLogs("KrabEar.Backend.OpenWakeWordAdapter", level="WARNING"):
            adapter.handle_wake_word_start({"model": "hey_jarvis"})


class TestRecordingGateIsQuiet(unittest.TestCase):
    """Отказ по записи не должен спамить лог: поллер стучится каждые 10 с."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_refusal_does_not_log_at_info(self) -> None:
        adapter = _make_adapter(self._tmp, is_recording=lambda: True)
        with self.assertNoLogs("KrabEar.Backend.OpenWakeWordAdapter", level="INFO"):
            adapter.handle_wake_word_start({"model": "hey_jarvis"})


class TestGateOrderPrivacyStillWins(unittest.TestCase):
    """Privacy-гейт остаётся первым: его причина важнее для пользователя."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def test_privacy_mode_reason_wins_over_recording(self) -> None:
        adapter = _make_adapter(
            self._tmp,
            settings={"privacy_mode_enabled": True, "wake_word_enabled": True},
            is_recording=lambda: True,
        )
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        self.assertEqual(
            result.get("reason"), "cannot activate wake-word in privacy mode"
        )


class TestGateIsActuallyWired(unittest.TestCase):
    """Гейт без проводки декоративен — ровно та ошибка, что уже ловилась в проекте.

    Источник правды — конструкция `OpenWakeWordAdapter(...)` в service.py: без
    переданного `is_recording` колбэк остаётся None, гейт молча пропускает всё,
    а тесты выше остаются зелёными (они конструируют адаптер сами). Проверяем
    по AST именно вызов, а не подстрокой: честный комментарий про is_recording
    не должен красить тест.
    """

    def test_service_passes_is_recording_to_adapter(self) -> None:
        import ast

        src = (PROJECT_ROOT / "backend" / "service.py").read_text()
        tree = ast.parse(src)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "OpenWakeWordAdapter":
                continue
            found.append({kw.arg for kw in node.keywords})

        self.assertTrue(found, "конструкция OpenWakeWordAdapter не найдена в service.py")
        for kwargs in found:
            self.assertIn(
                "is_recording",
                kwargs,
                "OpenWakeWordAdapter сконструирован без is_recording — гейт F6 декоративен",
            )
            self.assertIn(
                "is_start_blocked",
                kwargs,
                "OpenWakeWordAdapter без is_start_blocked — гейт W7 "
                "(worker жив после stop) декоративен",
            )

    def test_service_wires_start_blocked_to_recording_gate(self) -> None:
        """W7: start-гейт — тот же OR (is_recording | worker_alive), что
        защищает Pa_Terminate. Голая лямбда только на is_recording слепа
        после stop()-таймаута."""
        import ast

        src = (PROJECT_ROOT / "backend" / "service.py").read_text()
        tree = ast.parse(src)
        call = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "OpenWakeWordAdapter":
                call = node
                break
        self.assertIsNotNone(call, "конструкция OpenWakeWordAdapter не найдена")
        kwarg = next((kw for kw in call.keywords if kw.arg == "is_start_blocked"), None)
        self.assertIsNotNone(kwarg, "is_start_blocked= не передан")
        value = kwarg.value
        self.assertTrue(
            isinstance(value, ast.Attribute)
            and value.attr == "_reinit_is_recording_gate"
            and isinstance(value.value, ast.Name)
            and value.value.id == "self",
            f"is_start_blocked= обязан быть self._reinit_is_recording_gate, "
            f"получено {ast.dump(value)}",
        )


class TestStopUnderRecordingDoesNotWedge(unittest.TestCase):
    """Пауза wake word под запись НЕ должна помечать адаптер зависшим.

    Живой инцидент 2026-08-03, замкнутый круг: владелец начинает диктовку →
    агент паузит wake word (`wake_word_stop` → `adapter.stop()`), чтобы
    слушатель не ловил его же голос → поток не успевает выйти за 3 с →
    адаптер ставил `_wedged = True` → агент видел флаг и ПРИНУДИТЕЛЬНО
    перезапускал backend ПОСРЕДИ записи:

        01:39:56 Старт записи
        01:40:40 Остановка записи запрошена            (запись 44 с)
        01:40:40 [WakeWord] backend сообщил wedged — принудительный рестарт
        01:40:49 stop_recording не подтверждён

    То есть сама легитимная пауза порождала ложный сигнал зависания, и
    диктовка не доезжала ни до вставки, ни до истории.

    Направление отказа здесь ОБРАТНОЕ гейту F6 — fail-OPEN в сторону лечения:
    не знаем состояние → считаем, что записи нет, и оставляем watchdog'у право
    вылечить НАСТОЯЩИЙ клин. Обратное решение молча отключило бы самолечение
    wake word навсегда. У F6 цена ошибки другая (потерянная диктовка), потому
    там fail-closed — асимметрия намеренная.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()

    def _adapter_with_stuck_thread(self, is_recording) -> OpenWakeWordAdapter:
        import threading

        adapter = _make_adapter(self._tmp, is_recording=is_recording)
        release = threading.Event()
        self.addCleanup(release.set)

        stuck = threading.Thread(target=lambda: release.wait(timeout=30), daemon=True)
        stuck.start()
        self.addCleanup(stuck.join, 1.0)
        adapter._thread = stuck
        return adapter

    def test_no_wedge_when_recording_active(self) -> None:
        adapter = self._adapter_with_stuck_thread(is_recording=lambda: True)
        exited = adapter.stop(timeout=0.2)
        self.assertFalse(exited, "поток намеренно не выходит — предпосылка теста")
        self.assertFalse(
            adapter._wedged,
            "под активной записью wedged выставлять нельзя: агент убьёт backend "
            "посреди диктовки",
        )

    def test_wedge_still_set_when_idle(self) -> None:
        """Без записи поведение прежнее — настоящий клин обязан лечиться."""
        adapter = self._adapter_with_stuck_thread(is_recording=lambda: False)
        adapter.stop(timeout=0.2)
        self.assertTrue(adapter._wedged, "вне записи wedged обязан выставляться")

    def test_callback_failure_fails_open_towards_healing(self) -> None:
        def _boom() -> bool:
            raise RuntimeError("recorder недоступен")

        adapter = self._adapter_with_stuck_thread(is_recording=_boom)
        adapter.stop(timeout=0.2)
        self.assertTrue(
            adapter._wedged,
            "сбой колбэка не должен отключать самолечение настоящего клина",
        )


class TestSwiftPollerReasonContract(unittest.TestCase):
    """Строка reason гейта F6 совпадает буква-в-букву в Python и Swift.

    Находка 2 Fable-ревью: WakeWordPoller (Swift) жёг бюджет self-heal
    (maxFailedStartAttempts=3) на КАЖДОМ отказе. Отказ «recording in progress»
    — транзиентный (запись кончится сама), и три таких отказа за встречу
    (встреча НЕ ставит паузу поллеру — амендмент 2026-07-16) оставляли wake
    word тихо мёртвым до ручного перетыкания тумблера. Фикс: Swift сравнивает
    reason с константой `recordingInProgressReason` и НЕ инкрементирует бюджет.

    Сравнение строк — межпроцессный контракт: разъедутся буквы (перевод,
    опечатка, точка) — фикс молча превратится обратно в дыру. Python-сторону
    берём ЖИВЫМ вызовом гейта (не grep'ом), Swift-сторону — объявлением
    константы (для Swift AST-парсера здесь нет; объявление `static let` —
    конструкция, а не комментарий, честный комментарий тест не покраснит).
    """

    _SWIFT = (
        PROJECT_ROOT.parent
        / "native" / "KrabEarAgent" / "Sources" / "KrabEarAgent"
        / "WakeWordPoller.swift"
    )

    def _live_python_reason(self) -> str:
        adapter = _make_adapter(tempfile.mkdtemp(), is_recording=lambda: True)
        result = adapter.handle_wake_word_start({"model": "hey_jarvis"})
        self.assertFalse(result["ok"], result)
        return result["reason"]

    def _swift_constant(self) -> str:
        import re

        src = self._SWIFT.read_text()
        m = re.search(
            r'static\s+let\s+recordingInProgressReason\s*=\s*"([^"]+)"', src
        )
        self.assertIsNotNone(
            m, "константа recordingInProgressReason не найдена в WakeWordPoller.swift"
        )
        return m.group(1)

    def test_reason_strings_match_verbatim(self) -> None:
        self.assertEqual(self._live_python_reason(), self._swift_constant())

    def test_swift_failure_branch_checks_reason_before_budget(self) -> None:
        """Ветка сравнения стоит в sendStart ДО инкремента бюджета."""
        src = self._SWIFT.read_text()
        cmp_pos = src.find("why == Self.recordingInProgressReason")
        self.assertGreater(
            cmp_pos, 0,
            "sendStart не сравнивает reason с recordingInProgressReason — "
            "транзиентный отказ снова жжёт бюджет self-heal",
        )
        inc_pos = src.find("failedStartAttempts += 1")
        self.assertGreater(inc_pos, cmp_pos,
                           "инкремент бюджета стоит ДО проверки транзиентной причины")

    def test_poller_does_not_rearm_cap_on_single_last_chunk_ts(self) -> None:
        """W7: короткий heartbeat после kickstart не должен звать noteHealthy()."""
        src = self._SWIFT.read_text()
        self.assertNotIn(
            "self.wedgedTracker.noteHealthy()",
            src,
            "поллер снова обнуляет give-up кап на одном last_chunk_ts",
        )
        self.assertIn("notePoll(running:", src)


if __name__ == "__main__":
    unittest.main()
