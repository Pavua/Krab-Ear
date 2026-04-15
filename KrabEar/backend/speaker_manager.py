"""SpeakerManager — управление псевдонимами спикеров диаризации Krab Ear.

Позволяет назначить человекочитаемые имена идентификаторам спикеров
(например, SPEAKER_00 → «Паша»). Псевдонимы персистируются в
{data_dir}/speaker_aliases.json.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger("KrabEar.Backend.SpeakerManager")

_SPEAKER_TAG_RE = re.compile(r"\[(SPEAKER_\d+)\]")


class SpeakerManager:
    """Хранит псевдонимы спикеров и применяет их к тексту транскрипции."""

    _FILENAME = "speaker_aliases.json"

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._aliases: dict[str, str] = {}
        if data_dir is not None:
            self._path: Path | None = Path(data_dir) / self._FILENAME
            self._load()
        else:
            self._path = None

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает псевдонимы из файла (не бросает исключений)."""
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._aliases = {str(k): str(v) for k, v in data.items()}
        except Exception as exc:
            _log.warning("Не удалось загрузить псевдонимы спикеров: %s", exc)

    def _save(self) -> None:
        """Сохраняет псевдонимы в файл (не бросает исключений)."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._aliases, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            _log.warning("Не удалось сохранить псевдонимы спикеров: %s", exc)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def set_alias(self, speaker_id: str, name: str) -> None:
        """Назначает псевдоним спикеру. Пустое имя — удаляет псевдоним."""
        speaker_id = speaker_id.strip()
        name = name.strip()
        with self._lock:
            if name:
                self._aliases[speaker_id] = name
            else:
                self._aliases.pop(speaker_id, None)
            self._save()

    def get_alias(self, speaker_id: str) -> str | None:
        """Возвращает псевдоним спикера или None если не задан."""
        with self._lock:
            return self._aliases.get(speaker_id.strip())

    def get_all_aliases(self) -> dict[str, str]:
        """Возвращает копию словаря {speaker_id: name}."""
        with self._lock:
            return dict(self._aliases)

    def remove_alias(self, speaker_id: str) -> bool:
        """Удаляет псевдоним. Возвращает True если запись существовала."""
        speaker_id = speaker_id.strip()
        with self._lock:
            existed = speaker_id in self._aliases
            if existed:
                del self._aliases[speaker_id]
                self._save()
            return existed

    # ------------------------------------------------------------------
    # Применение псевдонимов к тексту
    # ------------------------------------------------------------------

    def apply_aliases(self, text: str) -> str:
        """Заменяет [SPEAKER_XX] на [ИмяСпикера] в тексте транскрипции.

        Теги без псевдонима остаются без изменений.
        """
        with self._lock:
            aliases = dict(self._aliases)

        def _replace(m: re.Match) -> str:
            sid = m.group(1)
            name = aliases.get(sid)
            return f"[{name}]" if name else m.group(0)

        return _SPEAKER_TAG_RE.sub(_replace, text)

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_set_speaker_alias(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: set_speaker_alias — назначить псевдоним спикеру."""
        speaker_id = str(params.get("speaker_id", "")).strip()
        name = str(params.get("name", "")).strip()
        if not speaker_id:
            raise ValueError("Параметр speaker_id обязателен")
        if not name:
            raise ValueError("Параметр name обязателен и не должен быть пустым")
        self.set_alias(speaker_id, name)
        return {"speaker_id": speaker_id, "name": name}

    def handle_get_speaker_aliases(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_speaker_aliases — получить все псевдонимы."""
        return {"aliases": self.get_all_aliases()}

    def handle_remove_speaker_alias(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: remove_speaker_alias — удалить псевдоним спикера."""
        speaker_id = str(params.get("speaker_id", "")).strip()
        if not speaker_id:
            raise ValueError("Параметр speaker_id обязателен")
        removed = self.remove_alias(speaker_id)
        return {"speaker_id": speaker_id, "removed": removed}
