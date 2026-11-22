"""G3-MEM-11: AutoDeduplicator без settings_provider читает историю даже в privacy-режиме.

Документированный «тестовый путь» (provider=None → privacy OFF) безопасен, пока ВСЕ
боевые конструкторы передают provider. Тест закрепляет это разбором AST: любое
создание AutoDeduplicator в backend/ обязано передавать settings_provider.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"


def _autodedup_calls():
    for path in sorted(BACKEND.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name == "AutoDeduplicator":
                    yield path.name, node


def test_every_production_autodeduplicator_gets_a_settings_provider():
    calls = list(_autodedup_calls())
    assert calls, "боевой конструктор AutoDeduplicator не найден — тест устарел"
    for filename, node in calls:
        keywords = {kw.arg for kw in node.keywords}
        assert "settings_provider" in keywords, (
            f"{filename}:{node.lineno} создаёт AutoDeduplicator без settings_provider — "
            "в privacy-режиме сравнение транскриптов не будет заблокировано"
        )
