"""Хранилище состояния Krab Ear: настройки и история последних транскрибаций.

Модуль используется UI (`ui/window.py`) для устойчивой работы:
1) хранит пользовательские переключатели;
2) хранит только последние N транскрибаций (по умолчанию 5);
3) безопасно переживает повреждённый JSON, чтобы приложение не падало.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any


DEFAULT_SETTINGS: dict[str, Any] = {
    "always_on_top": False,
    "auto_paste": True,
    "toggle_mode": True,
    "play_start_sound": True,
}


@dataclass(slots=True)
class HistoryItem:
    """Одна запись истории транскрибации."""

    timestamp: str
    text: str


@dataclass(slots=True)
class AppState:
    """Полное состояние приложения для сохранения на диск."""

    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    history: list[HistoryItem] = field(default_factory=list)


class AppStorage:
    """Менеджер файла состояния Krab Ear."""

    def __init__(self, state_file: Path | None = None, max_history: int = 5) -> None:
        self.max_history = max_history
        root = Path(__file__).resolve().parents[1]
        data_dir = root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = state_file or (data_dir / "state.json")

    def load(self) -> AppState:
        """Загружает состояние; при ошибке возвращает дефолт без исключения."""
        if not self.state_file.exists():
            return AppState()

        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return AppState()

        settings = dict(DEFAULT_SETTINGS)
        loaded_settings = payload.get("settings", {})
        if isinstance(loaded_settings, dict):
            settings.update(loaded_settings)

        history_items: list[HistoryItem] = []
        raw_history = payload.get("history", [])
        if isinstance(raw_history, list):
            for row in raw_history:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text", "")).strip()
                if not text:
                    continue
                timestamp = str(row.get("timestamp", "")).strip() or self._now_iso()
                history_items.append(HistoryItem(timestamp=timestamp, text=text))

        history_items = history_items[-self.max_history :]
        return AppState(settings=settings, history=history_items)

    def save(self, state: AppState) -> None:
        """Сохраняет состояние атомарно через временный файл."""
        normalized_history = state.history[-self.max_history :]
        payload = {
            "settings": state.settings,
            "history": [
                {"timestamp": item.timestamp, "text": item.text}
                for item in normalized_history
            ],
        }

        temp_path = self.state_file.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.state_file)

    def push_history(self, state: AppState, text: str) -> AppState:
        """Добавляет запись и возвращает обновлённый объект состояния."""
        clean_text = text.strip()
        if not clean_text:
            return state

        next_history = list(state.history)
        next_history.append(HistoryItem(timestamp=self._now_iso(), text=clean_text))
        state.history = next_history[-self.max_history :]
        return state

    @staticmethod
    def _now_iso() -> str:
        """Единый формат времени для UI и будущей аналитики."""
        return datetime.now().isoformat(timespec="seconds")
