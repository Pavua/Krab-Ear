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

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir: Path | None = Path(data_dir) if data_dir is not None else None
        self._vault_path: Path | None = None
        self._folder: str = _DEFAULT_FOLDER
        self._last_sync_ts: str | None = None
        self._lock = threading.Lock()

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

        result = SyncResult()
        target_dir = vault_path / folder
        target_dir.mkdir(parents=True, exist_ok=True)

        for item in items:
            try:
                item_ts = self._get_item_ts(item)
                self._get_item_attr(item, "id", "")

                # Инкрементальная синхронизация: пропускаем старые записи
                if not force and last_sync_ts is not None:
                    if item_ts <= last_sync_ts:
                        result.skipped_count += 1
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

        # Обновляем timestamp последней синхронизации
        now_ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._last_sync_ts = now_ts
            self._save_state()

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

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        """Форматирует длительность как `5 мин 23 сек` / `1 ч 5 мин`."""
        if seconds is None or seconds <= 0:
            return ""
        total = int(round(float(seconds)))
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h > 0:
            return f"{h} ч {m} мин"
        if m > 0:
            return f"{m} мин {s} сек"
        return f"{s} сек"

    @staticmethod
    def _format_hhmmss(seconds: float) -> str:
        """Форматирует секунды как `MM:SS` или `HH:MM:SS`."""
        total = int(seconds or 0)
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _build_md_content(self, item: Any) -> str:
        """Построить Obsidian-совместимый .md контент для записи.

        Расширенный формат: YAML frontmatter с audio_path/duration/language/
        speakers/confidence + body с ссылкой на запись, summary и
        диаризованным транскриптом. Edge cases — graceful: отсутствующие
        поля не рендерятся (без `None`/`Не указано`).
        """
        ts = self._get_item_attr(item, "ts", "")
        text = self._get_item_attr(item, "text", "")
        cleaned_text = self._get_item_attr(item, "cleaned_text", "")
        translated_text = self._get_item_attr(item, "translated_text", "")
        translation_mode = self._get_item_attr(item, "translation_mode", "off")
        source_lang = self._get_item_attr(item, "source_lang", "")
        target_lang = self._get_item_attr(item, "target_lang", "")
        tags = self._get_item_attr(item, "tags", []) or []
        diarization = self._get_item_attr(item, "diarization", None)
        speaker_turns_field = self._get_item_attr(item, "speaker_turns", None)
        confidence = self._get_item_attr(item, "confidence", None)
        audio_path = self._get_item_attr(item, "audio_path", "") or ""
        audio_duration_sec = self._get_item_attr(item, "audio_duration_sec", None)
        # `summary` — опциональный (может появиться позже в HistoryItem).
        # `reasoning` от Voxtral содержит summary/Q&A — используем как fallback.
        summary = self._get_item_attr(item, "summary", "") or self._get_item_attr(
            item, "reasoning", ""
        ) or ""
        item_id = self._get_item_attr(item, "id", "")

        # Форматируем дату
        try:
            dt = datetime.fromisoformat(str(ts))
            date_str = dt.strftime("%Y-%m-%d")
            datetime_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            iso_created = dt.isoformat()
        except (ValueError, TypeError):
            date_str = str(ts)[:10] if ts else ""
            datetime_str = str(ts) if ts else ""
            iso_created = str(ts) if ts else ""

        # Нормализуем теги для YAML — добавляем `call`
        yaml_tags = ["krab-ear", "transcript", "call"]
        for t in tags:
            clean = re.sub(r"[#\s]+", "-", str(t)).strip("-")
            if clean and clean not in yaml_tags:
                yaml_tags.append(clean)

        # Определяем язык: source_lang при переводе, иначе target_lang
        language = source_lang or target_lang or ""

        # Собираем имена спикеров (unique, в порядке появления)
        speaker_names: list[str] = []
        diar_turns: list[dict] = []
        if isinstance(diarization, dict) and diarization.get("speaker_turns"):
            diar_turns = list(diarization.get("speaker_turns") or [])
        elif isinstance(speaker_turns_field, list) and speaker_turns_field:
            diar_turns = list(speaker_turns_field)
        for turn in diar_turns:
            if not isinstance(turn, dict):
                continue
            sp = str(turn.get("speaker", "")).strip()
            if sp and sp not in speaker_names:
                speaker_names.append(sp)

        # ---- YAML frontmatter ----
        lines: list[str] = ["---"]
        title_str = f"Транскрипт {datetime_str}" if datetime_str else "Транскрипт"
        lines.append(f"title: {title_str}")
        if iso_created:
            lines.append(f"created: {iso_created}")
        if date_str:
            lines.append(f"date: {date_str}")
        if item_id:
            lines.append(f"id: {item_id}")
        lines.append("tags:")
        for tag in yaml_tags:
            lines.append(f"  - {tag}")
        if audio_path:
            lines.append(f"audio_path: {audio_path}")
        if audio_duration_sec is not None:
            try:
                lines.append(f"audio_duration_sec: {float(audio_duration_sec):.3f}")
            except (TypeError, ValueError):
                pass
        if language:
            lines.append(f"language: {language}")
        if source_lang and source_lang != language:
            lines.append(f"source_lang: {source_lang}")
        if target_lang and target_lang != language:
            lines.append(f"target_lang: {target_lang}")
        if speaker_names:
            lines.append("speakers:")
            for sp in speaker_names:
                # Sanitize пробелы/двоеточия для YAML scalar
                safe_sp = re.sub(r"[:\n]+", "_", sp).strip()
                lines.append(f"  - {safe_sp}")
        if confidence is not None:
            try:
                lines.append(f"confidence: {float(confidence):.3f}")
            except (TypeError, ValueError):
                pass
        lines.append("source: krab-ear")
        lines.append("---")
        lines.append("")

        # ---- Заголовок ----
        lines.append(f"# 🎙️ {title_str}")
        lines.append("")

        # ---- Ссылка на аудио ----
        if audio_path:
            # `file://` URI с абсолютным путём; пробелы Obsidian обрабатывает корректно
            uri = audio_path
            if not uri.startswith("file://"):
                uri = "file://" + uri if uri.startswith("/") else "file:///" + uri
            lines.append(f"[🔊 Открыть запись]({uri})  ")
            lines.append("")

        # ---- Метаданные одной строкой ----
        meta_parts: list[str] = []
        dur_str = self._format_duration(audio_duration_sec)
        if dur_str:
            meta_parts.append(f"**Длительность**: {dur_str}")
        if language:
            meta_parts.append(f"**Язык**: {language}")
        if confidence is not None:
            try:
                pct = int(round(float(confidence) * 100))
                meta_parts.append(f"**Уверенность**: {pct}%")
            except (TypeError, ValueError):
                pass
        if meta_parts:
            lines.append(" | ".join(meta_parts))
            lines.append("")

        # ---- Summary (только если есть) ----
        summary_text = str(summary).strip()
        if summary_text:
            lines.append("## 📝 Summary")
            lines.append("")
            lines.append(summary_text)
            lines.append("")

        # ---- Транскрипт ----
        body_text = (cleaned_text or text or "").strip()
        if diar_turns:
            lines.append("## 🎙️ Транскрипт")
            lines.append("")
            for turn in diar_turns:
                if not isinstance(turn, dict):
                    continue
                speaker = str(turn.get("speaker", "Speaker")).strip() or "Speaker"
                turn_text = str(turn.get("text", "")).strip()
                start_val = turn.get("start", 0.0) or 0.0
                try:
                    timestamp = self._format_hhmmss(float(start_val))
                except (TypeError, ValueError):
                    timestamp = "00:00"
                if turn_text:
                    lines.append(f"**{speaker} ({timestamp})**: {turn_text}")
                else:
                    lines.append(f"**{speaker} ({timestamp})**")
                lines.append("")
        elif body_text:
            lines.append("## 🎙️ Транскрипт")
            lines.append("")
            lines.append(body_text)
            lines.append("")

        # ---- Перевод (если есть) ----
        if translated_text and translation_mode != "off":
            lines.append("## 🌐 Перевод")
            lines.append("")
            lines.append(str(translated_text).strip())
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
