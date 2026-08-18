"""Конфигурация логирования REST-сервера: один раз, с временем, без дублей.

Волна W10 (2026-08-18). `krab-ear-rest.err.log` дорос до 179 МБ по двум
причинам сразу:

1. На каждый HTTP-запрос писались ДВЕ строки — собственная (`KrabEar.REST`)
   и access-строка werkzeug. При 34 000 STT-запросов это ровно вдвое больше
   строк, чем несут информации.
2. Собственная строка в text-режиме шла БЕЗ времени, а дату несла только
   строка werkzeug. Поэтому 2026-08-18 перцентили латентности не удалось
   привязать к датам: p95=38.9с был посчитан по всей истории файла и мог
   относиться к периоду до P0-фиксов. Метрика, которую нельзя датировать,
   не позволяет делать выводы — это дефект наблюдаемости, а не косметика.

🔴 Поэтому глушение werkzeug и добавление времени — одно неделимое изменение:
по отдельности первое купило бы экономию места ценой потери единственного
источника дат.
"""

from __future__ import annotations

import logging

# Время обязательно: без него строку нельзя соотнести с инцидентом.
REST_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_MARK = "_krab_rest_configured"


def configure_rest_logging(level: int = logging.INFO) -> None:
    """Настроить корневое логирование REST-процесса. Идемпотентна.

    Повторный вызов не добавляет хендлеров: лишний хендлер означал бы
    удвоение каждой строки — ровно та беда, которую волна и лечит.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(REST_LOG_FORMAT))
        setattr(handler, _MARK, True)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            if not getattr(handler, _MARK, False):
                handler.setFormatter(logging.Formatter(REST_LOG_FORMAT))
                setattr(handler, _MARK, True)
    root.setLevel(level)

    # Access-лог werkzeug дублирует собственную строку. Глушим ИМЕННО access
    # (INFO), диагностика уровня WARNING и выше обязана доходить: 4xx/5xx,
    # ошибки разбора запроса и падения воркера остаются видимыми.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
