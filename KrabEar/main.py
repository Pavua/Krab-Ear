"""Точка входа backend-сервиса Krab Ear.

Новая архитектура использует нативный macOS агент (Swift) для UI/hotkey и этот
Python-процесс как локальный движок записи/транскрибации.
"""

from __future__ import annotations

from backend.service import main


if __name__ == "__main__":
    main()
