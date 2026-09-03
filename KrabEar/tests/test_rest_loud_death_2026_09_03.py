"""Самоубийство REST после зависшей транскрибации обязано быть громким.

Живой случай 03.09.2026: `ai.krab.ear.rest` перезапускался 14 раз с кодом 70
(EX_SOFTWARE), а в журнале за весь день — НИ ОДНОЙ строки о причине. Шлюз при
этом видел 142 отказа «Transcription timeout» и «Krab Ear недоступен».

Причина молчания не в отсутствии лога: `logger.error("Transcription timed out…")`
вызывается. Она в том, что `os._exit` НЕ сбрасывает буферы, а служба запущена
без `-u`, поэтому stderr буферизуется блоками и последние записи гибнут вместе
с процессом. Тот же класс, что известный `(python script > log &)`.

🔴 Инвариант: fail-fast может быть сколь угодно резким, но обязан оставить
след. Иначе операционно он неотличим от краша, и причину искать не по чему.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class LoudDeathSourceContractTest(unittest.TestCase):
    """Контракт читается по исходнику: поднимать реальный REST ради проверки
    самоубийства нельзя — тест убил бы собственный процесс."""

    def setUp(self) -> None:
        self.src = (PACKAGE_ROOT / "backend" / "rest_server.py").read_text(encoding="utf-8")
        start = self.src.index("def _exit_poisoned_rest_process")
        end = self.src.index("def _arm_timeout_exit")
        self.body = self.src[start:end]

    def test_flush_precedes_os_exit(self) -> None:
        """Сброс обязан идти ДО os._exit — после него кода не существует.

        🔴 Сравниваем позиции ВЫЗОВОВ, а не упоминаний: docstring функции сам
        объясняет, почему взят `os._exit`, и наивный поиск подстроки находил
        его в тексте раньше настоящего вызова. Тест, который читает комментарий
        как код, краснеет на правильной реализации.
        """
        self.assertIn("_flush_logs_before_exit()", self.body,
                      "перед os._exit нет сброса журнала — причина смерти теряется в буфере")
        flush_at = self.body.index("_flush_logs_before_exit()")
        exit_at = self.body.index("os._exit(exit_code)")
        self.assertLess(flush_at, exit_at, "сброс журнала стоит ПОСЛЕ os._exit — он недостижим")

    def test_reason_is_logged_at_error_level(self) -> None:
        self.assertIn("logger.error", self.body,
                      "выход должен называть причину на уровне error, а не молчать")

    def test_flush_helper_covers_handlers_and_streams(self) -> None:
        """Мало сбросить stderr: записи логгера сидят в его собственных
        обработчиках, а stdout несёт строки Flask."""
        start = self.src.index("def _flush_logs_before_exit")
        helper = self.src[start:start + 1200]
        for needed in ("handlers", "stderr", "stdout"):
            self.assertIn(needed, helper, f"сброс не покрывает {needed}")

    def test_flush_never_blocks_the_exit(self) -> None:
        """Сброс не смеет помешать выходу: процесс уже отравлен зависшим
        MLX-локом, и исключение в логировании не должно оставить его жить."""
        start = self.src.index("def _flush_logs_before_exit")
        helper = self.src[start:start + 1200]
        self.assertIn("except", helper, "сброс без защиты может бросить и сорвать fail-fast")


class FlushHelperBehaviourTest(unittest.TestCase):
    def test_helper_survives_broken_handler(self) -> None:
        import backend.rest_server as rs

        class _Broken:
            def flush(self):
                raise OSError("сломанный обработчик")

        with patch.object(rs.logger, "handlers", [_Broken()]):
            rs._flush_logs_before_exit()  # не должно бросить

    def test_helper_flushes_real_handler(self) -> None:
        import backend.rest_server as rs

        flushed = []

        class _Spy:
            def flush(self):
                flushed.append(True)

        with patch.object(rs.logger, "handlers", [_Spy()]):
            rs._flush_logs_before_exit()
        self.assertTrue(flushed, "обработчик логгера не сброшен")
