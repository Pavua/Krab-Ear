"""W10 — объём и датируемость access-лога REST.

`krab-ear-rest.err.log` дорос до 179 МБ. Причины две, и обе лечатся здесь:

1. На КАЖДЫЙ запрос пишутся ДВЕ строки — своя (`KrabEar.REST`) и access-строка
   werkzeug. При 34 000 STT-запросов это ровно вдвое больше, чем нужно.
2. Своя строка в text-режиме идёт БЕЗ времени, а дату несла только строка
   werkzeug. Из-за этого 2026-08-18 не удалось привязать перцентили латентности
   к датам: p95=38.9с посчитан по всей истории лога и мог относиться к периоду
   до P0-фиксов. Метрика, которую нельзя датировать, не даёт делать выводы.

Поэтому глушить werkzeug можно ТОЛЬКО одновременно с добавлением времени в свою
строку — иначе экономия места купила бы потерю единственного источника дат.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.rest_log_config import (  # noqa: E402
    REST_LOG_FORMAT,
    configure_rest_logging,
)


class RestLogConfigTest(unittest.TestCase):
    def setUp(self):
        self._wz = logging.getLogger("werkzeug")
        self._prev_level = self._wz.level
        root = logging.getLogger()
        self._prev_root_level = root.level
        self._prev_handlers = list(root.handlers)

    def tearDown(self):
        # Процесс-глобальные эффекты обязаны откатываться (правило проекта
        # про необратимое тестовое окружение).
        self._wz.setLevel(self._prev_level)
        root = logging.getLogger()
        root.setLevel(self._prev_root_level)
        root.handlers[:] = self._prev_handlers

    def test_werkzeug_access_log_is_silenced(self):
        """Дубль строки на каждый запрос уходит; ошибки werkzeug остаются."""
        configure_rest_logging()
        self.assertGreaterEqual(
            logging.getLogger("werkzeug").level, logging.WARNING,
            "access-строки werkzeug дублируют собственный лог",
        )

    def test_werkzeug_errors_still_visible(self):
        """🔴 Глушим access, а не диагностику: WARNING и выше обязаны проходить."""
        configure_rest_logging()
        wz = logging.getLogger("werkzeug")
        self.assertTrue(wz.isEnabledFor(logging.WARNING))
        self.assertTrue(wz.isEnabledFor(logging.ERROR))
        self.assertFalse(wz.isEnabledFor(logging.INFO))

    def test_own_line_carries_timestamp(self):
        """Своя строка обязана нести время — иначе лог нельзя датировать."""
        self.assertIn("%(asctime)s", REST_LOG_FORMAT)

    def test_configured_root_formatter_has_time(self):
        configure_rest_logging()
        root = logging.getLogger()
        self.assertTrue(root.handlers, "должен быть хотя бы один хендлер")
        fmt = root.handlers[0].formatter
        self.assertIsNotNone(fmt)
        self.assertIn("asctime", getattr(fmt, "_fmt", "") or "")

    def test_is_idempotent(self):
        """Повторный вызов не должен плодить хендлеры (дубли строк в логе)."""
        configure_rest_logging()
        n1 = len(logging.getLogger().handlers)
        configure_rest_logging()
        n2 = len(logging.getLogger().handlers)
        self.assertEqual(n1, n2)


if __name__ == "__main__":
    unittest.main()
