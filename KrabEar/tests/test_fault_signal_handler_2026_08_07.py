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
import signal
import textwrap
import unittest
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

    def _report(self, verdict):
        from backend.service import BackendService

        stub = type("Stub", (), {})()
        stub._error_bus = self._FakeBus()
        BackendService._report_unclean_restart(stub, verdict)
        return stub._error_bus.pushed

    def test_clean_verdicts_report_nothing(self):
        for verdict in ("first_run", "clean"):
            with self.subTest(verdict=verdict):
                self.assertEqual(self._report(verdict), [])

    def test_unclean_verdicts_push_error(self):
        for verdict in ("unclean_collected", "unclean_collect_failed"):
            with self.subTest(verdict=verdict):
                pushed = self._report(verdict)
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

    def test_never_raises_when_error_bus_is_broken(self):
        """Телеметрия не должна ронять тред startup-recovery.

        Следом за этим вызовом идёт rescue-скан незавершённых записей — если
        отчёт бросит, аудио пользователя не будет восстановлено.
        """
        from backend.service import BackendService

        class _ExplodingBus:
            def push(self, err):
                raise RuntimeError("bus down")

        stub = type("Stub", (), {})()
        stub._error_bus = _ExplodingBus()
        try:
            BackendService._report_unclean_restart(stub, "unclean_collected")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"_report_unclean_restart пробросил {exc!r}")


class StartupRecoveryUsesVerdictSourceContractTest(unittest.TestCase):
    """AST-контракт: вердикт ``check_and_collect()`` не выбрасывается.

    Именно потерянное возвращаемое значение сделало крэши невидимыми —
    проверяем сам факт использования, а не наличие подстроки.
    """

    def test_verdict_is_assigned_and_passed_on(self):
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

        used = {
            n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        self.assertTrue(
            assigned_names & used,
            f"вердикт {assigned_names} присвоен, но нигде не читается",
        )


if __name__ == "__main__":
    unittest.main()
