"""Паритет IPC-документации с диспетчером (волна 0.4).

Каждый ключ _build_dispatch_table обязан быть задокументирован в
docs/IPC_API_REFERENCE.md. Исключение: clear_privacy_audit_log — намеренно
удалён из dispatch (W957 SECURITY, service.py:3167), в доке обязан нести
маркер удаления, а не молчать.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "KrabEar" / "backend" / "service.py"
DOC = REPO / "docs" / "IPC_API_REFERENCE.md"

# Документирован, но НЕ в dispatch — и это правильно (возврат запрещён).
KNOWN_REMOVED = {"clear_privacy_audit_log"}
REMOVED_MARKER = "намеренно удалён"


def dispatch_keys() -> set[str]:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    builders = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_build_dispatch_table"
    ]
    assert len(builders) == 1, "ожидался ровно один _build_dispatch_table"
    returns = [n for n in ast.walk(builders[0]) if isinstance(n, ast.Return)]
    tables = [
        n.value for n in returns
        if isinstance(n.value, ast.Dict)
        and sum(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in n.value.keys) > 50
    ]
    assert len(tables) == 1, "таблица dispatch не найдена"
    return {k.value for k in tables[0].keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def documented_names() -> set[str]:
    return set(re.findall(r"^### `([A-Za-z0-9_]+)`", DOC.read_text(encoding="utf-8"), re.M))


class IpcDocsParityTests(unittest.TestCase):
    def test_every_dispatched_method_is_documented(self) -> None:
        missing = sorted(dispatch_keys() - documented_names())
        self.assertEqual(missing, [])

    def test_every_documented_name_exists_or_is_marked_removed(self) -> None:
        extra = sorted(documented_names() - dispatch_keys())
        self.assertEqual(set(extra) - KNOWN_REMOVED, set())
        text = DOC.read_text(encoding="utf-8")
        for name in sorted(set(extra) & KNOWN_REMOVED):
            section = text.split(f"### `{name}`", 1)[1].split("\n## ", 1)[0]
            self.assertIn(REMOVED_MARKER, section.lower(),
                          f"{name}: секция обязана нести маркер удаления")

    def test_counts_are_not_pinned_but_sane(self) -> None:
        # Гарды от протухания методики, НЕ от дрейфа тоталов: тоталы не вшиваем.
        self.assertGreater(len(dispatch_keys()), 300)


if __name__ == "__main__":
    unittest.main()
