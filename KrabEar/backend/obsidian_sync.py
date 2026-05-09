"""ObsidianSyncManager — синхронизация транскрипций Krab Ear с Obsidian vault.

Создаёт/обновляет .md файлы в Obsidian-совместимом формате (YAML frontmatter)
в указанной папке внутри vault. Синхронизирует только записи новее последней
синхронизации (если не указан force=True). Состояние сохраняется в
{data_dir}/obsidian_sync.json.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.ObsidianSync")

_SYNC_STATE_FILE = "obsidian_sync.json"
_DEFAULT_FOLDER = "Transcriptions"


@dataclass
class SyncResult:
    """Результат операции синхронизации с Obsidian vault."""

    synced_count: int = 0
    skipped_count: int = 0
    errors: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    updated_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObsidianSyncManager:
    """Менеджер синхронизации транскрипций Krab Ear с Obsidian vault.

    Поддерживает:
    - configure(vault_path, folder) — настройка цели синхронизации
    - sync(items, force) — запуск синхронизации
    - get_sync_status() — статус последней синхронизации
    - Создание/обновление .md файлов с YAML frontmatter
    - Инкрементальная синхронизация (только новые записи)
    - Персистентность состояния в {data_dir}/obsidian_sync.json
    """

    def __init__(self, data_dir: Path | None = None, event_bus=None) -> None:
        self._data_dir: Path | None = Path(data_dir) if data_dir is not None else None
        self._vault_path: Path | None = None
        self._folder: str = _DEFAULT_FOLDER
        self._last_sync_ts: str | None = None
        self._lock = threading.Lock()
        self._event_bus = event_bus

        if self._data_dir is not None:
            self._state_path = self._data_dir / _SYNC_STATE_FILE
            self._load_state()
        else:
            self._state_path = None

    # ------------------------------------------------------------------
    # Конфигурация
    # ------------------------------------------------------------------

    def configure(self, vault_path: str, folder: str = _DEFAULT_FOLDER) -> dict[str, Any]:
        """Установить целевой Obsidian vault и папку для синхронизации.

        Проверяет, что vault_path существует (должна быть директорией).
        Создаёт папку folder внутри vault, если её нет.

        Возвращает dict с vault_path, folder, folder_full_path.
        Вызывает ValueError если vault_path не существует или не является директорией.
        """
        p = Path(vault_path).expanduser().resolve()
        if not p.exists():
            raise ValueError(f"Vault path не существует: {vault_path!r}")
        if not p.is_dir():
            raise ValueError(f"Vault path должен быть директорией: {vault_path!r}")

        folder = folder.strip() or _DEFAULT_FOLDER

        with self._lock:
            self._vault_path = p
            self._folder = folder

            target_dir = p / folder
            target_dir.mkdir(parents=True, exist_ok=True)

            self._save_state()

        return {
            "vault_path": str(p),
            "folder": folder,
            "folder_full_path": str(target_dir),
        }

    # ------------------------------------------------------------------
    # Синхронизация
    # ------------------------------------------------------------------

    def sync(self, items: list[Any], force: bool = False) -> SyncResult:
        """Синхронизировать записи истории с Obsidian vault.

        Создаёт или обновляет .md файлы в формате Obsidian.
        Если force=False — синхронизирует только записи новее last_sync_ts.
        Если force=True — синхронизирует все переданные записи.

        Параметры:
            items — список HistoryItem (или dict с полями id, ts, text …).
            force — принудительная полная синхронизация.

        Возвращает SyncResult.
        Вызывает RuntimeError если vault не настроен.
        """
        with self._lock:
            if self._vault_path is None:
                raise RuntimeError(
                    "Obsidian vault не настроен. Вызовите configure() сначала."
                )
            vault_path = self._vault_path
            folder = self._folder
            last_sync_ts = self._last_sync_ts

        import time as _time

        result = SyncResult()
        target_dir = vault_path / folder
        target_dir.mkdir(parents=True, exist_ok=True)

        if self._event_bus is not None:
            self._event_bus.emit("app.status", {
                "op": "obsidian_sync",
                "stage": "started",
                "total_files": len(items),
                "progress": 0.0,
                "ts": _time.time(),
            })

        _total = len(items)
        for i, item in enumerate(items):
            try:
                item_ts = self._get_item_ts(item)
                self._get_item_attr(item, "id", "")

                # Инкрементальная синхронизация: пропускаем старые записи
                if not force and last_sync_ts is not None:
                    if item_ts <= last_sync_ts:
                        result.skipped_count += 1
                        if self._event_bus is not None:
                            self._event_bus.emit("app.status", {
                                "op": "obsidian_sync",
                                "stage": "syncing",
                                "file_index": i + 1,
                                "total_files": _total,
                                "progress": (i + 1) / _total if _total else 1.0,
                                "ts": _time.time(),
                            })
                        continue

                md_filename = self._make_filename(item)
                md_path = target_dir / md_filename
                existed = md_path.exists()

                content = self._build_md_content(item)
                md_path.write_text(content, encoding="utf-8")

                if existed:
                    result.updated_files.append(str(md_path))
                else:
                    result.new_files.append(str(md_path))
                result.synced_count += 1

            except Exception as exc:
                item_repr = self._get_item_attr(item, "id", repr(item))
                logger.error("Ошибка синхронизации записи %s: %s", item_repr, exc)
                result.errors.append(f"{item_repr}: {exc}")

            if self._event_bus is not None:
                self._event_bus.emit("app.status", {
                    "op": "obsidian_sync",
                    "stage": "syncing",
                    "file_index": i + 1,
                    "total_files": _total,
                    "progress": (i + 1) / _total if _total else 1.0,
                    "ts": _time.time(),
                })

        # Обновляем timestamp последней синхронизации
        now_ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._last_sync_ts = now_ts
            self._save_state()

        if self._event_bus is not None:
            self._event_bus.emit("app.status", {
                "op": "idle",
                "stage": "",
                "progress": 1.0,
                "ts": _time.time(),
            })

        logger.info(
            "Obsidian sync завершён: synced=%d skipped=%d errors=%d",
            result.synced_count,
            result.skipped_count,
            len(result.errors),
        )
        return result

    # ------------------------------------------------------------------
    # Статус
    # ------------------------------------------------------------------

    def get_sync_status(self) -> dict[str, Any]:
        """Вернуть статус синхронизации: vault_path, folder, last_sync_ts, file_count.

        file_count — количество .md файлов в папке vault/folder (или 0 если не настроен).
        """
        with self._lock:
            vault_path = self._vault_path
            folder = self._folder
            last_sync_ts = self._last_sync_ts

        file_count = 0
        if vault_path is not None:
            target_dir = vault_path / folder
            if target_dir.exists():
                file_count = sum(1 for f in target_dir.iterdir() if f.suffix == ".md")

        return {
            "configured": vault_path is not None,
            "vault_path": str(vault_path) if vault_path else None,
            "folder": folder,
            "last_sync_ts": last_sync_ts,
            "file_count": file_count,
        }

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_configure(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик configure_obsidian_sync."""
        vault_path = params.get("vault_path")
        if not vault_path:
            raise ValueError("Параметр vault_path обязателен")
        folder = str(params.get("folder", _DEFAULT_FOLDER))
        return self.configure(str(vault_path), folder)

    def handle_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик run_obsidian_sync.

        Принимает items (список dict) и опциональный force (bool).
        """
        raw_items = params.get("items")
        if raw_items is None or not isinstance(raw_items, list):
            raise ValueError("Параметр items (список) обязателен")
        force = bool(params.get("force", False))
        result = self.sync(raw_items, force=force)
        return result.to_dict()

    def handle_get_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик get_obsidian_sync_status."""
        return self.get_sync_status()

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _get_item_attr(item: Any, attr: str, default: Any = "") -> Any:
        """Получить атрибут из HistoryItem или dict."""
        if isinstance(item, dict):
            return item.get(attr, default)
        return getattr(item, attr, default)

    def _get_item_ts(self, item: Any) -> str:
        """Получить строку timestamp записи для сравнения."""
        ts = self._get_item_attr(item, "ts", "")
        return str(ts) if ts else ""

    def _make_filename(self, item: Any) -> str:
        """Сформировать безопасное имя файла для записи."""
        ts = self._get_item_attr(item, "ts", "")
        item_id = self._get_item_attr(item, "id", "")

        # Форматируем timestamp в читаемый вид: YYYY-MM-DD_HH-MM-SS
        try:
            dt = datetime.fromisoformat(str(ts))
            ts_part = dt.strftime("%Y-%m-%d_%H-%M-%S")
        except (ValueError, TypeError):
            ts_part = re.sub(r"[^\w\-]", "_", str(ts))[:20] if ts else "unknown"

        # Используем первые 8 символов UUID как суффикс
        id_suffix = str(item_id)[:8] if item_id else "noid"
        safe_suffix = re.sub(r"[^\w\-]", "_", id_suffix)

        return f"transcript_{ts_part}_{safe_suffix}.md"

    def _build_md_content(self, item: Any) -> str:
        """Построить Obsidian-совместимый .md контент для записи."""
        ts = self._get_item_attr(item, "ts", "")
        text = self._get_item_attr(item, "text", "")
        translated_text = self._get_item_attr(item, "translated_text", "")
        translation_mode = self._get_item_attr(item, "translation_mode", "off")
        source_lang = self._get_item_attr(item, "source_lang", "")
        target_lang = self._get_item_attr(item, "target_lang", "")
        tags = self._get_item_attr(item, "tags", []) or []
        diarization = self._get_item_attr(item, "diarization", None)
        confidence = self._get_item_attr(item, "confidence", None)
        item_id = self._get_item_attr(item, "id", "")

        # Форматируем дату
        try:
            dt = datetime.fromisoformat(str(ts))
            date_str = dt.strftime("%Y-%m-%d")
            datetime_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            date_str = str(ts)[:10] if ts else ""
            datetime_str = str(ts) if ts else ""

        # Нормализуем теги для YAML
        yaml_tags = ["krab-ear", "transcript"]
        for t in tags:
            clean = re.sub(r"[#\s]+", "-", str(t)).strip("-")
            if clean:
                yaml_tags.append(clean)

        # Строим frontmatter
        lines: list[str] = ["---"]
        lines.append(f"title: Транскрипция {date_str}")
        lines.append(f"date: {datetime_str}")
        lines.append(f"id: {item_id}")
        lines.append("tags:")
        for tag in yaml_tags:
            lines.append(f"  - {tag}")
        if source_lang:
            lines.append(f"source_lang: {source_lang}")
        if target_lang:
            lines.append(f"target_lang: {target_lang}")
        if confidence is not None:
            lines.append(f"confidence: {confidence:.3f}")
        lines.append("source: krab-ear")
        lines.append("---")
        lines.append("")

        # Заголовок документа
        lines.append(f"# Транскрипция {datetime_str}")
        lines.append("")

        # Секция транскрипции
        lines.append("## Улучшенная транскрибация")
        lines.append("")

        if diarization and isinstance(diarization, dict) and diarization.get("enabled"):
            speaker_turns = diarization.get("speaker_turns", [])
            if speaker_turns:
                for turn in speaker_turns:
                    speaker = turn.get("speaker", "Спикер")
                    turn_text = turn.get("text", "")
                    start = turn.get("start", 0.0)
                    # Форматируем время как HH:MM:SS
                    h = int(start // 3600)
                    m = int((start % 3600) // 60)
                    s = int(start % 60)
                    timestamp = f"{h:02d}:{m:02d}:{s:02d}"
                    lines.append(f"**[{speaker} ({timestamp})]** {turn_text}")
                    lines.append("")
            else:
                lines.append(f"[Спикер (00:00:00)] {text}")
                lines.append("")
        else:
            # Без диаризации — стандартный формат
            lines.append(f"[Спикер (00:00:00)] {text}")
            lines.append("")

        # Секция перевода (если есть)
        if translated_text and translation_mode != "off":
            lines.append("## Перевод")
            lines.append("")
            lines.append(translated_text)
            lines.append("")

        # Краткое содержание — placeholder
        lines.append("## Краткое содержание (Summary)")
        lines.append("")
        lines.append("*Авто-резюме не сгенерировано.*")
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Персистентность состояния
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Загрузить состояние из JSON-файла."""
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            vault_path_str = raw.get("vault_path")
            if vault_path_str:
                p = Path(vault_path_str)
                if p.exists() and p.is_dir():
                    self._vault_path = p
            self._folder = str(raw.get("folder", _DEFAULT_FOLDER))
            self._last_sync_ts = raw.get("last_sync_ts")
        except Exception as exc:
            logger.warning("Не удалось загрузить состояние ObsidianSync: %s", exc)

    def _save_state(self) -> None:
        """Сохранить состояние в JSON-файл. Вызывать под self._lock."""
        if self._state_path is None:
            return
        if self._data_dir is not None:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {
            "vault_path": str(self._vault_path) if self._vault_path else None,
            "folder": self._folder,
            "last_sync_ts": self._last_sync_ts,
        }
        try:
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception as exc:
            logger.error("Не удалось сохранить состояние ObsidianSync: %s", exc)
