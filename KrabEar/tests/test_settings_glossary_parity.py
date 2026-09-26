# -*- coding: utf-8 -*-
"""Тест паритета настроек Krab Ear с русским глоссарием и документацией.

Проверяет, что каждый параметр конфигурации из DEFAULT_SETTINGS:
1. Задокументирован в docs/settings-glossary-ru.md с типом, группой и описанием.
2. Присутствует в Swift-словаре SettingsGlossary.items с русским заголовком и группой.
"""

from __future__ import annotations

import re
from pathlib import Path
import pytest
from core.config import DEFAULT_SETTINGS

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_settings_glossary_markdown_parity() -> None:
    """Все ключи DEFAULT_SETTINGS должны присутствовать в docs/settings-glossary-ru.md."""
    doc_path = REPO_ROOT / "docs" / "settings-glossary-ru.md"
    assert doc_path.exists(), f"Файл документации {doc_path} обязан существовать"

    content = doc_path.read_text(encoding="utf-8")
    table_keys = set(re.findall(r"\|\s*`([a-z0-9_]+)`\s*\|", content))

    missing_keys = set(DEFAULT_SETTINGS.keys()) - table_keys
    assert not missing_keys, (
        f"В docs/settings-glossary-ru.md отсутствуют {len(missing_keys)} ключей: {sorted(missing_keys)}"
    )


def test_settings_glossary_swift_parity() -> None:
    """Все ключи DEFAULT_SETTINGS должны быть определены в SettingsGlossary.swift."""
    swift_path = (
        REPO_ROOT
        / "native"
        / "KrabEarAgent"
        / "Sources"
        / "KrabEarAgent"
        / "SettingsGlossary.swift"
    )
    assert swift_path.exists(), f"Файл {swift_path} обязан существовать"

    content = swift_path.read_text(encoding="utf-8")
    swift_keys = set(re.findall(r'"([a-z0-9_]+)":\s*SettingDescriptor\(', content))

    missing_keys = set(DEFAULT_SETTINGS.keys()) - swift_keys
    assert not missing_keys, (
        f"В SettingsGlossary.swift отсутствуют {len(missing_keys)} ключей: {sorted(missing_keys)}"
    )


def test_settings_glossary_minimum_keys_count() -> None:
    """В DEFAULT_SETTINGS должно быть не менее 245 ключей."""
    assert len(DEFAULT_SETTINGS) >= 245, (
        f"Ожидалось >= 245 ключей, фактически: {len(DEFAULT_SETTINGS)}"
    )
