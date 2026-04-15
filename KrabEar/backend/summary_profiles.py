"""SummaryProfileManager — профили стиля резюмирования для Krab Ear.

Встроенные профили: brief, detailed, bullet_points, meeting_notes, telegram.
Кастомные профили хранятся в {data_dir}/summary_profiles.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.SummaryProfiles")

# ---------------------------------------------------------------------------
# Модель профиля
# ---------------------------------------------------------------------------


@dataclass
class SummaryProfile:
    """Профиль стиля резюмирования."""

    name: str
    system_prompt: str
    max_tokens: int
    format_instructions: str
    builtin: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "system_prompt": self.system_prompt,
            "max_tokens": self.max_tokens,
            "format_instructions": self.format_instructions,
            "builtin": self.builtin,
        }


# ---------------------------------------------------------------------------
# Встроенные профили
# ---------------------------------------------------------------------------

_BUILTIN_PROFILES: list[SummaryProfile] = [
    SummaryProfile(
        name="brief",
        system_prompt=(
            "Ты — помощник-редактор. Сделай очень краткое резюме текста: "
            "не более 2–3 предложений. Отвечай строго на языке исходного текста."
        ),
        max_tokens=150,
        format_instructions="2-3 предложения, без заголовков, без списков.",
        builtin=True,
    ),
    SummaryProfile(
        name="detailed",
        system_prompt=(
            "Ты — аналитик. Напиши подробное резюме: один абзац с основными идеями, "
            "ключевыми деталями и выводами. Отвечай на языке исходного текста."
        ),
        max_tokens=400,
        format_instructions="Один связный абзац с ключевыми пунктами и выводами.",
        builtin=True,
    ),
    SummaryProfile(
        name="bullet_points",
        system_prompt=(
            "Ты — структурный редактор. Извлеки главные мысли из текста "
            "и представь их в виде маркированного списка. "
            "Каждый пункт — одно законченное утверждение. "
            "Отвечай на языке исходного текста."
        ),
        max_tokens=300,
        format_instructions="Маркированный список (- пункт), 4–8 пунктов.",
        builtin=True,
    ),
    SummaryProfile(
        name="meeting_notes",
        system_prompt=(
            "Ты — секретарь совещания. Структурируй текст переговоров в виде протокола:\n"
            "УЧАСТНИКИ: <список имён/ролей если упомянуты, иначе 'не указаны'>\n"
            "РЕШЕНИЯ:\n- <решение 1>\nДЕЙСТВИЯ:\n- <действие 1> (ответственный, срок)\n"
            "ИТОГ: <одно предложение — главный результат встречи>\n"
            "Отвечай на языке исходного текста."
        ),
        max_tokens=500,
        format_instructions="Структурированный протокол: УЧАСТНИКИ / РЕШЕНИЯ / ДЕЙСТВИЯ / ИТОГ.",
        builtin=True,
    ),
    SummaryProfile(
        name="telegram",
        system_prompt=(
            "Ты — автор Telegram-сообщений. Перепиши текст как короткое сообщение "
            "для пересылки в мессенджер: 1–3 коротких предложения, разговорный стиль, "
            "без служебных заголовков. Отвечай на языке исходного текста."
        ),
        max_tokens=200,
        format_instructions="1–3 коротких предложения, разговорный стиль, без заголовков.",
        builtin=True,
    ),
]

_BUILTIN_MAP: dict[str, SummaryProfile] = {p.name: p for p in _BUILTIN_PROFILES}


# ---------------------------------------------------------------------------
# Менеджер профилей
# ---------------------------------------------------------------------------

class SummaryProfileManager:
    """Управляет встроенными и кастомными профилями резюмирования."""

    _PROFILES_FILE = "summary_profiles.json"

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir else None
        self._custom: dict[str, SummaryProfile] = {}
        if self._data_dir:
            self._load()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get_profile(self, name: str) -> SummaryProfile:
        """Возвращает профиль по имени.

        Raises:
            KeyError: если профиль с таким именем не найден.
        """
        if name in _BUILTIN_MAP:
            return _BUILTIN_MAP[name]
        if name in self._custom:
            return self._custom[name]
        raise KeyError(f"Профиль резюмирования не найден: {name!r}")

    def list_profiles(self) -> list[dict[str, Any]]:
        """Возвращает список всех профилей (встроенных + кастомных)."""
        result = [p.to_dict() for p in _BUILTIN_PROFILES]
        result.extend(p.to_dict() for p in self._custom.values())
        return result

    def add_custom_profile(
        self,
        name: str,
        prompt: str,
        max_tokens: int,
        format_instructions: str = "",
    ) -> SummaryProfile:
        """Добавляет (или заменяет) кастомный профиль.

        Args:
            name: уникальное имя профиля (строка без пробелов, рекомендуется snake_case).
            prompt: системный промпт для LLM.
            max_tokens: максимальное количество токенов в ответе.
            format_instructions: краткое описание формата ответа (для UI).

        Returns:
            Созданный SummaryProfile.

        Raises:
            ValueError: если name — зарезервированное имя встроенного профиля,
                        либо если prompt/name пустые.
        """
        name = name.strip()
        if not name:
            raise ValueError("Имя профиля не может быть пустым")
        if name in _BUILTIN_MAP:
            raise ValueError(
                f"Имя {name!r} зарезервировано встроенным профилем — выберите другое имя"
            )
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Промпт профиля не может быть пустым")
        max_tokens = int(max_tokens)
        if max_tokens < 1:
            raise ValueError("max_tokens должен быть >= 1")

        profile = SummaryProfile(
            name=name,
            system_prompt=prompt,
            max_tokens=max_tokens,
            format_instructions=format_instructions.strip(),
            builtin=False,
        )
        self._custom[name] = profile
        self._save()
        logger.info("SummaryProfileManager: добавлен кастомный профиль %r", name)
        return profile

    def remove_custom_profile(self, name: str) -> bool:
        """Удаляет кастомный профиль. Возвращает True если удалён."""
        if name in _BUILTIN_MAP:
            raise ValueError(f"Нельзя удалить встроенный профиль {name!r}")
        removed = self._custom.pop(name, None)
        if removed:
            self._save()
            logger.info("SummaryProfileManager: удалён профиль %r", name)
            return True
        return False

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _profiles_path(self) -> Path | None:
        if self._data_dir is None:
            return None
        return self._data_dir / self._PROFILES_FILE

    def _load(self) -> None:
        path = self._profiles_path()
        if path is None or not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            for entry in data:
                p = SummaryProfile(
                    name=entry["name"],
                    system_prompt=entry["system_prompt"],
                    max_tokens=int(entry["max_tokens"]),
                    format_instructions=entry.get("format_instructions", ""),
                    builtin=False,
                )
                self._custom[p.name] = p
            logger.debug("SummaryProfileManager: загружено %d кастомных профилей", len(self._custom))
        except Exception as exc:
            logger.warning("SummaryProfileManager: не удалось загрузить профили: %s", exc)

    def _save(self) -> None:
        path = self._profiles_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                json.dump([p.to_dict() for p in self._custom.values()], fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning("SummaryProfileManager: не удалось сохранить профили: %s", exc)
