"""Аварийные сигналы (SIGSEGV/SIGABRT/…) не получают Python-обработчик.

🔴 Живой инцидент 2026-08-07. ``install_signal_handlers()`` вешал Python-колбэк
на SIGSEGV и SIGABRT, чтобы отправить событие в Sentry перед смертью. Это
документированная ловушка CPython: Python-колбэк НЕ исполняется в момент сбоя —
C-уровень (``signal_handler`` → ``trip_signal``) лишь ставит флаг и
ВОЗВРАЩАЕТСЯ, после чего ядро повторяет сбойную инструкцию. Для синхронного
сбоя это бесконечный цикл ре-сбоя.

Что было снято с живого прод-процесса (pid 1057, sample сохранён в
``.remember/forensics/backend-1057-sigsegv-storm-2026-08-07.sample.txt``):

* поток записи ``Thread-4 (_worker)`` — 1902 сэмпла из 1966 внутри
  ``_sigtramp`` на ``PaUtil_ReadRingBuffer`` (тот самый бесконечный ре-сбой),
  отсюда «AudioRecorder worker не завершился за 3.0 с» в логе;
* главный поток — 46 ВЛОЖЕННЫХ ``handle_signals``: каждый следующий сбой
  перевзводил Python-колбэк поверх незавершённого предыдущего, самый глубокий
  кадр стоит на ``lock_PyThread_acquire_lock``. 🔴 Чем именно кончалось
  вложение — дедлоком на локе внутри ``sentry_sdk.flush()`` или лайвлоком
  (ре-сбоящий поток взводит флаг быстрее, чем главный успевает дренировать) —
  sample НЕ различает: все простаивающие треды процесса стоят на том же
  ``_PyParkingLot_Park``, поэтому этот кадр сам по себе уликой дедлока не
  является (поправка адверсариального ревью). Лечение в обоих случаях одно;
* итог: процесс не принимает соединения (``ConnectionRefusedError`` на IPC),
  но и не умирает, и НЕ МОЖЕТ обработать SIGTERM — поэтому self-heal бессилен,
  лечит только SIGKILL. Честный крэш, после которого launchd поднял бы бэкенд
  за пару секунд, превращался в многочасовой ТИХИЙ простой.

Инвариант: аварийные сигналы обслуживает ``faulthandler`` (signal-safe C-уровень:
печатает трейсбек всех потоков и передаёт сигнал default-обработчику, процесс
честно умирает → launchd перезапускает). Никаких Python-колбэков.
"""

from __future__ import annotations

import ast
import faulthandler
import inspect
import json
import signal
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


# Синхронные («аварийные») сигналы: возврат из обработчика повторяет сбойную
# инструкцию, поэтому Python-колбэк для них запрещён в принципе.
FAULT_SIGNAL_NAMES = ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGFPE", "SIGILL")


def _fault_signals() -> list:
    return [getattr(signal, name) for name in FAULT_SIGNAL_NAMES
            if hasattr(signal, name)]


class FaultSignalsHaveNoPythonHandlerTest(unittest.TestCase):
    """После install_signal_handlers() ни один аварийный сигнал не питонячий."""

    def setUp(self):
        import backend.observability as mod

        self.mod = mod
        self._reset_installed_flag()
        # Процесс-глобальное состояние: снимаем снимок и возвращаем в tearDown
        # (иначе тест ломает соседей по чанку — класс «irreversible test
        # scaffolding»).
        self._saved_handlers = {
            sig: signal.getsignal(sig) for sig in _fault_signals()
        }
        self._faulthandler_was_enabled = faulthandler.is_enabled()

    def tearDown(self):
        for sig, handler in self._saved_handlers.items():
            try:
                # getsignal() отдаёт None, когда обработчик поставлен НЕ из
                # Python (например, самим faulthandler'ом pytest'а) — вернуть
                # такой объект через signal.signal() нельзя, поэтому кладём
                # SIG_DFL и ниже восстанавливаем faulthandler, если он был.
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
            except (ValueError, OSError, TypeError):
                pass
        if self._faulthandler_was_enabled:
            faulthandler.enable(all_threads=True)
        else:
            faulthandler.disable()
        self._reset_installed_flag()

    def _reset_installed_flag(self):
        if hasattr(self.mod.install_signal_handlers, "_installed"):
            del self.mod.install_signal_handlers._installed

    def test_no_fault_signal_gets_a_python_callable(self):
        self.mod.install_signal_handlers()

        for sig in _fault_signals():
            handler = signal.getsignal(sig)
            self.assertFalse(
                callable(handler),
                f"{sig.name}: Python-колбэк {handler!r} зациклит сбойную "
                f"инструкцию — обработчик обязан быть C-уровня "
                f"(faulthandler) или SIG_DFL",
            )

    def test_sigterm_and_sigint_are_not_touched(self):
        """Владелец SIGTERM/SIGINT — main() в service.py, не этот модуль."""
        before = {
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
        }

        self.mod.install_signal_handlers()

        for sig, handler in before.items():
            self.assertIs(
                signal.getsignal(sig), handler,
                f"{sig.name} перехвачен install_signal_handlers()",
            )

    def test_enables_faulthandler_for_all_threads(self):
        """Диагностика сбоя не теряется: трейсбек всех потоков в stderr.

        Именно этого не хватило при разборе живого инцидента — стек пришлось
        реконструировать по C-символам из ``sample``.
        """
        with patch.object(faulthandler, "enable") as enable:
            self.mod.install_signal_handlers()

        enable.assert_called_once()
        self.assertTrue(
            enable.call_args.kwargs.get("all_threads", False),
            "нужен all_threads=True: сбой прилетает в рабочий поток "
            "(в инциденте — в поток записи), а не в главный",
        )

    def test_idempotent(self):
        with patch.object(faulthandler, "enable") as enable:
            self.mod.install_signal_handlers()
            self.mod.install_signal_handlers()

        self.assertEqual(enable.call_count, 1)

    def test_never_raises_when_faulthandler_unavailable(self):
        """Диагностика не должна ронять запуск бэкенда."""
        with patch.object(faulthandler, "enable", side_effect=RuntimeError("no stderr")):
            try:
                self.mod.install_signal_handlers()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"install_signal_handlers() пробросил {exc!r}")

    def test_failed_enable_does_not_latch_diagnostics_off(self):
        """Провал enable() не должен запирать диагностику на всю жизнь процесса.

        Находка адверсариального ревью: защёлка ``_installed`` взводилась ДО
        попытки, поэтому один неудачный вызов (закрытый stderr — реальный
        сценарий для bundled-рантайма внутри .app) навсегда выключал бы
        crash-диагностику, и повторить было бы некому.
        """
        with patch.object(faulthandler, "enable", side_effect=RuntimeError("no stderr")):
            self.mod.install_signal_handlers()

        self.assertFalse(
            getattr(self.mod.install_signal_handlers, "_installed", False),
            "защёлка взведена после ПРОВАЛА enable() — повторная попытка невозможна",
        )

        # Вторая попытка (stderr снова доступен) обязана реально сработать.
        with patch.object(faulthandler, "enable") as enable:
            self.mod.install_signal_handlers()
        enable.assert_called_once()

    def test_failure_is_logged_loudly_not_at_debug(self):
        """Молчаливое отключение crash-диагностики — тот же класс, что уже ловили."""
        with patch.object(faulthandler, "enable", side_effect=RuntimeError("no stderr")):
            with self.assertLogs(self.mod.logger, level="WARNING") as captured:
                self.mod.install_signal_handlers()

        self.assertTrue(
            any("faulthandler" in line for line in captured.output),
            f"нет WARNING про недоступный faulthandler: {captured.output}",
        )


class InstallSignalHandlersSourceContractTest(unittest.TestCase):
    """AST-контракт: модуль наблюдаемости не регистрирует сигналы из Python.

    Сверка по AST, а не по подстроке: честный комментарий про signal.signal()
    не должен ронять корректную реализацию.
    """

    def _function_ast(self):
        import backend.observability as mod

        source = inspect.getsource(mod.install_signal_handlers)
        return ast.parse(textwrap.dedent(source))

    def test_does_not_call_signal_signal(self):
        calls = [
            node for node in ast.walk(self._function_ast())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "signal"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "signal"
        ]

        self.assertEqual(
            calls, [],
            "install_signal_handlers() снова ставит Python-обработчик сигнала "
            "— это и был корень тихого дауна бэкенда 2026-08-07",
        )

    def test_does_not_call_raise_signal(self):
        """``signal.raise_signal()`` в обработчике — второй виток того же цикла."""
        calls = [
            node for node in ast.walk(self._function_ast())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "raise_signal"
        ]

        self.assertEqual(calls, [], "raise_signal() внутри обработчика сбоя")


class UncleanRestartIsReportedTest(unittest.TestCase):
    """Вердикт форензики доходит до ErrorBus, а не выбрасывается.

    Находка адверсариального ревью этой же волны: убрав обработчик, который
    ХОТЯ БЫ НАМЕРЕВАЛСЯ послать ``capture_message(level="fatal")``, волна
    сделала бы наблюдаемость крэшей строго хуже — ``check_and_collect()``
    возвращал вердикт, а вызывающая сторона его игнорировала, и единственным
    следом оставалась WARNING-строка в err.log (у ``sentry_sdk`` по умолчанию
    WARNING — breadcrumb, а не issue).

    Метод зовём как несвязанный, на лёгкой заглушке: конструировать настоящий
    ``BackendService`` тут не нужно и вредно (демон-треды + обязательный
    ``close()`` в tearDown — хронический источник CI-флейка чанков).
    """

    class _FakeBus:
        def __init__(self):
            self.pushed = []

        def push(self, err):
            self.pushed.append(err)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_stub(self, bus=None):
        """Заглушка «процесса»: только то, что метод реально трогает.

        Константы берём из самого ``BackendService`` — единственный источник
        правды; хардкод здесь разошёлся бы с продом молча.
        """
        from backend.service import BackendService

        stub = type("Stub", (), {
            "UNCLEAN_RESTART_STATE_FILE": BackendService.UNCLEAN_RESTART_STATE_FILE,
            "UNCLEAN_RESTART_REPORT_MIN_GAP_SEC": (
                BackendService.UNCLEAN_RESTART_REPORT_MIN_GAP_SEC
            ),
        })()
        stub._error_bus = bus if bus is not None else self._FakeBus()
        return stub

    def _report(self, verdict, stub=None, data_dir=None):
        from backend.service import BackendService

        stub = stub if stub is not None else self._make_stub()
        BackendService._report_unclean_restart(
            stub, verdict, data_dir if data_dir is not None else self.data_dir,
        )
        return stub._error_bus.pushed

    def test_clean_verdicts_report_nothing(self):
        for verdict in ("first_run", "clean"):
            with self.subTest(verdict=verdict):
                self.assertEqual(self._report(verdict), [])

    def test_unclean_verdicts_push_error(self):
        for verdict in ("unclean_collected", "unclean_collect_failed"):
            with self.subTest(verdict=verdict):
                # Своя data_dir на подтест: кросс-рестартовый лимит ниже
                # иначе подавил бы второй вердикт.
                with tempfile.TemporaryDirectory() as fresh:
                    pushed = self._report(verdict, data_dir=Path(fresh))
                self.assertEqual(len(pushed), 1, "нештатная смерть не отражена в ErrorBus")
                err = pushed[0]
                self.assertEqual(err.code, "system.unclean_restart")
                self.assertEqual(err.context.get("verdict"), verdict)

    def test_severity_is_error_so_sentry_gets_it_immediately(self):
        """warn-tier батчится и сбрасывается лишь при ШТАТНОМ завершении.

        Для события «прошлая жизнь умерла нештатно» это ровно неверная
        семантика: в крэш-лупе штатного завершения не будет и одиночное
        событие потерялось бы навсегда.
        """
        err = self._report("unclean_collected")[0]
        self.assertEqual(err.severity, "error")

    def test_crash_loop_is_rate_limited_across_restarts(self):
        """🔴 Главный риск фикса: каждый подъём — НОВЫЙ процесс.

        In-memory дедуп ErrorBus живёт в памяти умершего процесса и для этого
        кода не срабатывает никогда. launchd поднимает юнит с KeepAlive=true и
        ThrottleInterval=5 — до ~720 раз в час; без кросс-рестартового лимита
        это положило бы квоту Sentry (уже выгорала на PortAudioError-лупе).
        Каждый вызов ниже имитирует ОТДЕЛЬНЫЙ процесс (свежая заглушка), но
        одну и ту же data_dir — как в реальном крэш-лупе.
        """
        pushes = [len(self._report("unclean_collected")) for _ in range(20)]

        self.assertEqual(pushes[0], 1, "первый крэш обязан быть отчитан")
        self.assertEqual(
            sum(pushes), 1,
            f"крэш-луп прорвался в Sentry: {sum(pushes)} событий подряд",
        )

    def test_suppressed_count_conveys_loop_intensity(self):
        """Одно событие в 15 минут неотличимо от одиночного крэша без счётчика."""
        for _ in range(5):
            self._report("unclean_collected")

        # Сдвигаем окно в прошлое — следующий отчёт снова разрешён.
        from backend.service import BackendService

        state = self.data_dir / BackendService.UNCLEAN_RESTART_STATE_FILE
        prev = json.loads(state.read_text(encoding="utf-8"))
        prev["last_report_ts"] = 0.0
        state.write_text(json.dumps(prev), encoding="utf-8")

        err = self._report("unclean_collected")[0]
        self.assertEqual(
            err.context.get("suppressed_since_last"), 4,
            "счётчик подавленных не доехал — интенсивность лупа не видна",
        )

    def test_never_raises_when_error_bus_is_broken(self):
        """Телеметрия не должна ронять тред startup-recovery.

        Следом за этим вызовом идёт rescue-скан незавершённых записей — если
        отчёт бросит, аудио пользователя не будет восстановлено.
        """
        from backend.service import BackendService

        class _ExplodingBus:
            def push(self, err):
                raise RuntimeError("bus down")

        try:
            BackendService._report_unclean_restart(
                self._make_stub(bus=_ExplodingBus()), "unclean_collected", self.data_dir,
            )
        except Exception as exc:  # noqa: BLE001
            self.fail(f"_report_unclean_restart пробросил {exc!r}")

    def test_unwritable_state_dir_suppresses_instead_of_storming(self):
        """Не сохранили лимит — не отчитываемся (fail-closed), но и не падаем.

        Направление отказа выбрано осознанно и несимметрично: потерять один
        отчёт о крэше дешевле, чем выжечь квоту Sentry на ~720 подъёмах в час,
        когда лимит фактически не работает.
        """
        try:
            pushed = self._report(
                "unclean_collected", data_dir=self.data_dir / "does" / "not" / "exist",
            )
        except Exception as exc:  # noqa: BLE001
            self.fail(f"_report_unclean_restart пробросил {exc!r}")

        self.assertEqual(pushed, [], "лимит не сохранён, но отчёт всё равно ушёл")

    def test_write_is_atomic_so_a_failed_write_cannot_truncate_the_limit(self):
        """🔴 Прерванная запись НЕ должна оставлять усечённый файл.

        Голый ``write_text`` сначала усекает файл и только потом пишет. Если
        между этими шагами процесс умирает (крэш-луп) или диск полон (ENOSPC —
        сам по себе типовая причина ровно таких нештатных смертей), остаётся
        нулевой файл, который следующий старт прочитает как «отчётов ещё не
        было» — лимит превращается в no-op именно тогда, когда он нужен
        (адверсариальное ревью воспроизвело 10 отчётов из 10 «рестартов»).
        Класс «read-modify-write without a fail-safe» из CLAUDE.md.
        """
        import backend.service as service_mod
        from backend.service import BackendService

        state = self.data_dir / BackendService.UNCLEAN_RESTART_STATE_FILE
        self._report("unclean_collected")
        intact = state.read_text(encoding="utf-8")
        self.assertIn("last_report_ts", intact)

        # Следующая запись обрывается на последнем шаге (публикация tmp).
        with patch.object(service_mod.os, "replace", side_effect=OSError("ENOSPC")):
            pushed = self._report("unclean_collected")

        self.assertEqual(
            state.read_text(encoding="utf-8"), intact,
            "сорванная запись повредила состояние лимита — шлюз открыт",
        )
        self.assertEqual(pushed, [], "лимит не сохранён, а отчёт всё равно ушёл")
        self.assertFalse(
            list(self.data_dir.glob("*.tmp")),
            "временный файл не убран за собой",
        )

    def test_clock_skew_into_the_future_does_not_silence_forever(self):
        """Метка из будущего — испорченное состояние, а не «окно ещё идёт».

        Реальный триггер: севший RTC до синхронизации NTP или шаг часов назад.
        Без явной проверки elapsed >= 0 подавление становится вечным и
        молчаливым, причём лог рапортует штатную работу.
        """
        import time as _t

        from backend.service import BackendService

        state = self.data_dir / BackendService.UNCLEAN_RESTART_STATE_FILE
        state.write_text(
            json.dumps({"last_report_ts": _t.time() + 365 * 24 * 3600,
                        "suppressed_since_last": 0}),
            encoding="utf-8",
        )

        pushed = self._report("unclean_collected")
        self.assertEqual(
            len(pushed), 1,
            "метка из будущего заглушила отчёт — подавление стало бы вечным",
        )


class StartupRecoveryUsesVerdictSourceContractTest(unittest.TestCase):
    """AST-контракт: вердикт ``check_and_collect()`` не выбрасывается.

    Именно потерянное возвращаемое значение сделало крэши невидимыми —
    проверяем сам факт использования, а не наличие подстроки.
    """

    def test_verdict_is_assigned_and_passed_on(self):
        """Требуем ИМЕННО передачу вердикта в отчёт, а не «где-то прочитан».

        Слабая версия этого теста (вердикт присвоен + имя где-то читается)
        обходилась одной строкой ``logger.debug("verdict=%s", verdict)`` при
        полностью регрессировавшем баге — поймано пере-ревью правок.
        """
        import backend.service as service_mod

        source = textwrap.dedent(inspect.getsource(service_mod.BackendService.__init__))
        tree = ast.parse(source)

        assigned_names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "check_and_collect":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned_names.add(target.id)

        self.assertTrue(
            assigned_names,
            "результат check_and_collect() никуда не присваивается — вердикт "
            "нештатной смерти снова теряется",
        )

        reported_names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "_report_unclean_restart":
                continue
            for arg in node.args:
                if isinstance(arg, ast.Name):
                    reported_names.add(arg.id)

        self.assertTrue(
            reported_names,
            "_report_unclean_restart() не вызывается — вердикт нештатной "
            "смерти снова никуда не доходит",
        )
        self.assertTrue(
            assigned_names & reported_names,
            f"вердикт {assigned_names} присвоен, но в отчёт уходит что-то "
            f"другое ({reported_names})",
        )

    def test_report_call_is_an_unconditional_statement(self):
        """Вызов обязан быть ПРЯМЫМ оператором тела, а не спрятанным под ветвление.

        Даже усиленная версия предыдущего теста остаётся зелёной при полностью
        регрессировавшем баге — достаточно обернуть вызов в
        ``if verdict == "clean":`` (поймано третьим раундом ревью). Достижимость
        AST не доказывает в принципе; требуем хотя бы, чтобы вызов не был под
        условием внутри функции восстановления.

        🔴 Честное ограничение: внешний гейт ``_needs_background_recovery``
        (спавнить ли тред вообще) этим тестом НЕ покрыт — сделать его
        всегда-False по-прежнему тихо отключило бы отчёт.
        """
        import backend.service as service_mod

        source = textwrap.dedent(inspect.getsource(service_mod.BackendService.__init__))
        tree = ast.parse(source)

        def _calls_check_and_collect(fn):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if name == "check_and_collect":
                        return True
            return False

        bodies = [
            fn.body for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef) and _calls_check_and_collect(fn)
        ]
        self.assertTrue(bodies, "не нашли функцию восстановления с check_and_collect")

        def _is_report_stmt(stmt):
            return (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and getattr(stmt.value.func, "attr", None) == "_report_unclean_restart"
            )

        self.assertTrue(
            any(any(_is_report_stmt(st) for st in body) for body in bodies),
            "_report_unclean_restart не является безусловным оператором тела "
            "функции восстановления — отчёт можно тихо отключить ветвлением",
        )


if __name__ == "__main__":
    unittest.main()
