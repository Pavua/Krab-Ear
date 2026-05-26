"""Профили нормализации текста для Krab Ear.

Каждый профиль задаёт набор правил обработки транскрипции под конкретный контекст:
- verbatim  — минимальная обработка (только галлюцинации)
- clean     — стандартная очистка (поведение «soft» из TextUtils)
- formal    — агрессивная очистка + пунктуация + капитализация
- telegram  — короткий формат, без точки в конце, emoji-friendly
- subtitles — SRT-стиль: ограничение длины строки
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.NormalizationProfiles")

# Precompiled regex — используются в apply() и _apply_rule() на каждой транскрипции
_RE_NORMALIZE_WS = re.compile(r"\s+")
_RE_CAPITALIZE_SENT = re.compile(r"(?:^|(?<=[.!?…])\s+)([а-яa-z])")
_RE_STRIP_TRAILING_PERIOD = re.compile(r"[.]+$")

# ── Встроенные профили ──────────────────────────────────────────────────────

# Frozenset of builtin profile names — used to prevent disk JSON from
# silently overriding them in _load_custom() (W1264 N1).
_BUILTIN_NAMES: frozenset[str] = frozenset({"verbatim", "clean", "formal", "telegram", "subtitles"})

_BUILTIN_PROFILES: list[dict[str, Any]] = [
    {
        "name": "verbatim",
        "description": "Минимальная обработка: только удаление галлюцинаций Whisper",
        "rules": ["strip_hallucinations"],
    },
    {
        "name": "clean",
        "description": "Стандартная очистка: повторы, галлюцинации, нормализация брендов/времени",
        "rules": ["strip_hallucinations", "cleanup_soft", "normalize_entities"],
    },
    {
        "name": "formal",
        "description": "Агрессивная очистка + исправление пунктуации + правильная капитализация",
        "rules": [
            "strip_hallucinations",
            "cleanup_soft",
            "cleanup_strict",
            "normalize_entities",
            "fix_punctuation",
            "capitalize_sentences",
        ],
    },
    {
        "name": "telegram",
        "description": "Короткий формат для Telegram: без точки в конце, лаконично",
        "rules": [
            "strip_hallucinations",
            "cleanup_soft",
            "normalize_entities",
            "strip_trailing_period",
        ],
    },
    {
        "name": "subtitles",
        "description": "SRT-стиль: перенос строк по 42 символа, поддержка тайм-меток",
        "rules": [
            "strip_hallucinations",
            "cleanup_soft",
            "normalize_entities",
            "wrap_lines_42",
        ],
    },
]

# ── Движок правил ───────────────────────────────────────────────────────────


def _apply_rule(text: str, rule: str) -> str:
    """Применяет одно именованное правило к тексту."""
    from core.utils import TextUtils  # lazy, избегаем циклических импортов

    if rule == "strip_hallucinations":
        return TextUtils._strip_hallucinations(text)

    if rule == "cleanup_soft":
        return TextUtils._cleanup_soft(text)

    if rule == "cleanup_strict":
        return TextUtils._cleanup_strict(text)

    if rule == "normalize_entities":
        return TextUtils.normalize_entities(text)

    if rule == "fix_punctuation":
        try:
            return TextUtils.fix_punctuation(text)
        except Exception:
            return text

    if rule == "capitalize_sentences":
        # Капитализируем первую букву каждого предложения
        def _cap(m: re.Match) -> str:
            return m.group(0).upper()
        result = _RE_CAPITALIZE_SENT.sub(_cap, text)
        if result and result[0].islower():
            result = result[0].upper() + result[1:]
        return result

    if rule == "strip_trailing_period":
        return _RE_STRIP_TRAILING_PERIOD.sub("", text.rstrip())

    if rule == "wrap_lines_42":
        # Разбиваем текст на строки по ~42 символа (не ломая слова)
        return "\n".join(textwrap.wrap(text, width=42)) if text else text

    logger.warning("Неизвестное правило нормализации: %s", rule)
    return text


# ── Реестр профилей ─────────────────────────────────────────────────────────

@dataclass
class NormalizationProfile:
    name: str
    description: str
    rules: list[str] = field(default_factory=list)
    builtin: bool = False

    def apply(self, text: str) -> str:
        """Применяет все правила профиля последовательно."""
        result = _RE_NORMALIZE_WS.sub(" ", text).strip()
        for rule in self.rules:
            result = _apply_rule(result, rule)
        return result.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "rules": list(self.rules),
            "builtin": self.builtin,
        }


class NormalizationProfileRegistry:
    """Реестр профилей нормализации с поддержкой пользовательских профилей."""

    _CUSTOM_FILE = "normalization_profiles.json"

    def __init__(self, data_dir: Path | None = None) -> None:
        self._profiles: dict[str, NormalizationProfile] = {}
        self._data_dir = data_dir

        # Загружаем встроенные профили
        for raw in _BUILTIN_PROFILES:
            p = NormalizationProfile(
                name=raw["name"],
                description=raw["description"],
                rules=list(raw["rules"]),
                builtin=True,
            )
            self._profiles[p.name] = p

        # Загружаем пользовательские профили из диска
        if data_dir:
            self._load_custom(data_dir)

    # ── Публичный API ───────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict[str, Any]]:
        """Возвращает список всех профилей (встроенных + пользовательских)."""
        return [p.to_dict() for p in self._profiles.values()]

    def apply_profile(self, text: str, profile_name: str) -> str:
        """Применяет профиль по имени и возвращает нормализованный текст."""
        profile = self._profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"Профиль не найден: {profile_name!r}")
        return profile.apply(text)

    def add_profile(
        self,
        name: str,
        rules: list[str],
        description: str = "",
        *,
        overwrite: bool = False,
    ) -> NormalizationProfile:
        """Добавляет (или перезаписывает) пользовательский профиль и сохраняет на диск."""
        if not name or not name.strip():
            raise ValueError("Имя профиля не может быть пустым")
        if name in self._profiles and self._profiles[name].builtin and not overwrite:
            raise ValueError(f"Нельзя перезаписать встроенный профиль: {name!r}")
        profile = NormalizationProfile(
            name=name.strip(),
            description=description,
            rules=list(dict.fromkeys(rules)),  # deduplicate, preserve order (W1264 N2)
            builtin=False,
        )
        self._profiles[profile.name] = profile
        self._save_custom()
        return profile

    def remove_profile(self, name: str) -> bool:
        """Удаляет пользовательский профиль. Возвращает True если профиль был удалён."""
        profile = self._profiles.get(name)
        if profile is None:
            return False
        if profile.builtin:
            raise ValueError(f"Нельзя удалить встроенный профиль: {name!r}")
        del self._profiles[name]
        self._save_custom()
        return True

    def get_profile(self, name: str) -> NormalizationProfile | None:
        return self._profiles.get(name)

    # ── Персистентность ─────────────────────────────────────────────────────

    def _custom_path(self) -> Path | None:
        if self._data_dir is None:
            return None
        return self._data_dir / self._CUSTOM_FILE

    def _load_custom(self, data_dir: Path) -> None:
        path = data_dir / self._CUSTOM_FILE
        if not path.exists():
            return
        try:
            raw_list: list[dict] = json.loads(path.read_text(encoding="utf-8"))
            loaded = 0
            for raw in raw_list:
                name = raw.get("name", "")
                if name in _BUILTIN_NAMES:
                    logger.warning(
                        "Пропуск кастомного профиля %r: имя зарезервировано для встроенного профиля",
                        name,
                    )
                    continue
                p = NormalizationProfile(
                    name=name,
                    description=raw.get("description", ""),
                    rules=list(raw.get("rules", [])),
                    builtin=False,
                )
                self._profiles[p.name] = p
                loaded += 1
            logger.debug("Загружено %d пользовательских профилей из %s", loaded, path)
        except Exception as exc:
            logger.warning("Не удалось загрузить кастомные профили: %s", exc)

    def _save_custom(self) -> None:
        path = self._custom_path()
        if path is None:
            return
        custom = [p.to_dict() for p in self._profiles.values() if not p.builtin]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Не удалось сохранить кастомные профили: %s", exc)


# ── Singleton-подобный глобальный реестр (без data_dir по умолчанию) ────────

_default_registry: NormalizationProfileRegistry | None = None


def get_registry(data_dir: Path | None = None) -> NormalizationProfileRegistry:
    """Возвращает глобальный реестр, при необходимости инициализируя его."""
    global _default_registry
    if _default_registry is None or data_dir is not None:
        _default_registry = NormalizationProfileRegistry(data_dir=data_dir)
    return _default_registry


# ── Удобные функции верхнего уровня ─────────────────────────────────────────

def apply_profile(text: str, profile_name: str) -> str:
    """Применяет именованный профиль нормализации."""
    return get_registry().apply_profile(text, profile_name)


def list_profiles() -> list[dict[str, Any]]:
    """Список всех доступных профилей нормализации."""
    return get_registry().list_profiles()


def add_profile(name: str, rules: list[str], description: str = "") -> dict[str, Any]:
    """Добавляет пользовательский профиль и возвращает его описание."""
    return get_registry().add_profile(name, rules, description).to_dict()
