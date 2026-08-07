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
  заблокирован на ``lock_PyThread_acquire_lock`` (лок внутри
  ``sentry_sdk.flush()``) — классический дедлок реентерабельности;
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


if __name__ == "__main__":
    unittest.main()
