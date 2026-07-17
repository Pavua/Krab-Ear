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
from typing import Any, Callable


def _sanitize_md_body_text(text: str) -> str:
    """Sanitize a string before inserting it into a Markdown body.

    Prevents YAML-frontmatter-boundary injection: a bare ``---`` on its own
    line is a Markdown horizontal rule / YAML separator and can corrupt the
    .md file structure or confuse Obsidian's parser.

    Also strips NUL bytes and CR characters that have no place in a Markdown
    document.

    Does NOT strip newlines in general — multi-line transcripts are valid.

    Escaping rule: lines that are exactly ``---`` or ``...`` (the YAML end
    marker) become ``\\---`` / ``\\...`` so Markdown renderers show them as
    literal dashes rather than structural elements.
    """
    if not text:
        return text
    # Strip control chars that are meaningless in UTF-8 Markdown
    text = text.replace('\x00', '').replace('\r', '')
    # Escape bare YAML document boundary lines
    text = re.sub(r'(?m)^(---+|\.\.\.)$', r'\\\g<0>', text)
    return text


def _sanitize_speaker_name(name: str) -> str:
    """Sanitize a speaker label for safe inline Markdown use.

    Speaker names appear inside ``**[name (HH:MM:SS)]**`` inline formatting.
    Newlines would break the line structure; brackets confuse the Markdown
    parser.  Strip leading/trailing whitespace and replace newlines / brackets
    / backticks with space or underscore to keep the display clean.
    """
    if not name:
        return "Спикер"
    name = name.replace('\n', ' ').replace('\r', '')
    # Keep the name printable — replace chars that break inline MD formatting
    name = re.sub(r'[\[\]`]', '_', name)
    return name.strip() or "Спикер"


def _yaml_scalar(value: str) -> str:
    """Return a YAML-safe scalar string.

    Wraps in double-quotes and escapes internal double-quotes / backslashes
    if the value contains any character that would break unquoted YAML scalars
    (colon, newline, leading special chars, brackets, braces, etc.).
    Plain ASCII-safe values that don't start with a YAML indicator are left
    unquoted for readability.

    This avoids a PyYAML dependency while being correct for the subset of
    strings that appear in transcript frontmatter.
    """
    _NEEDS_QUOTE = re.compile(r'[:{}\[\]#&*!|>\'"\n\r,]|^\s|\s$|^[-?]')
    if _NEEDS_QUOTE.search(value) or not value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    return value


def _parse_ts(ts_str: str) -> datetime:
    """Parse an ISO-8601 timestamp string to a tz-aware datetime (UTC).

    Handles:
    - ``2026-05-26T12:00:00+00:00``  — already aware, kept as-is.
    - ``2026-05-26T12:00:00Z``       — Z suffix → +00:00.
    - ``2026-05-26T12:00:00``        — naive → assumed UTC.
    """
    ts_str = ts_str.strip()
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _ts_le(a: str, b: str) -> bool:
    """Return True if timestamp string *a* is less-than-or-equal to *b*.

    Falls back to lexicographic comparison if either value cannot be parsed,
    preserving the original behaviour while avoiding ValueError crashes.
    """
    try:
        return _parse_ts(a) <= _parse_ts(b)
    except (ValueError, TypeError):
        return a <= b


logger = logging.getLogger("KrabEar.Backend.ObsidianSync")

_SYNC_STATE_FILE = "obsidian_sync.json"
_DEFAULT_FOLDER = "Transcriptions"

# wave-32 MED DoS: cap the number of items synced per run to prevent disk-fill
# when the vault has never been synced and history contains tens of thousands of
# items. force=True still honours this cap; callers can paginate if needed.
MAX_SYNC_ITEMS = 10_000

# wave-32 MED (vault_path confuseddeputy): subdirectories under $HOME that must
# never be written to by the sync engine even if an IPC caller supplies them as
# vault_path. Checked AFTER expanduser().resolve() so symlinks are followed.
_FORBIDDEN_HOME_SUBDIRS: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    "Library/Keychains",
    "Library/Application Support/com.apple",
    "Library/Preferences",
    "Library/Saved Application State",
)

# W1768 (MED, path-traversal): разрешённый шаблон для имени папки внутри vault.
# Только буквы/цифры/подчёркивания (\w), дефис, пробел и слэш — чтобы поддержать
# вложенные подпапки (например "Krab Ear/Транскрипции"), но запретить любые
# спецсимволы, абсолютные пути и обход через "..".
_SAFE_FOLDER_PATTERN = re.compile(r"[\w\- /]+", re.UNICODE)


class UnsafeFolderError(ValueError):
    """Имя папки выходит за пределы Obsidian vault (path-traversal попытка).

    W1768 (MED): отдельный подкласс ValueError, чтобы вызывающая сторона при
    необходимости могла отличить traversal-отказ от прочих ошибок конфигурации,
    сохраняя при этом обратную совместимость (по-прежнему ловится как ValueError).
    """


def _validate_and_resolve_folder(vault_path: Path, folder: str) -> Path:
    """Проверить *folder* и вернуть безопасный resolved target_dir внутри *vault_path*.

    W1768 (MED, path-traversal): ``Path(vault) / folder`` сам по себе НЕ защищает
    от выхода за пределы vault — pathlib позволяет абсолютному RHS заменить базу
    (``Path('/vault') / '/etc'`` → ``/etc``), а ``..`` поднимается вверх по дереву.
    Незащищённый путь приводил к тому, что ``configure()``/``sync()`` создавали и
    писали .md в произвольную директорию (например ``/private/etc/cron.d``).

    Защита (raise ДО любого mkdir/write):
    1. Пустой folder → ValueError.
    2. Абсолютный folder (``/etc``, ``C:\\...``) → UnsafeFolderError.
    3. Любой компонент пути равен ``..`` → UnsafeFolderError.
    4. Folder не соответствует безопасному шаблону ``[\\w\\- /]+`` → UnsafeFolderError.
    5. ``(vault / folder).resolve()`` выходит за пределы ``vault.resolve()``
       (проверяется через ``Path.relative_to``) → UnsafeFolderError.

    Возвращает resolved ``target_dir`` (Path), гарантированно внутри vault.
    """
    if not folder or not folder.strip():
        raise ValueError("Параметр folder не может быть пустым")

    folder = folder.strip()

    # (2) Абсолютный путь недопустим: иначе он заменил бы базу vault целиком.
    if Path(folder).is_absolute():
        raise UnsafeFolderError(
            f"folder не должен быть абсолютным путём: {folder!r}"
        )

    # (3) Явный запрет на родительский компонент ".." в любой части пути.
    parts = Path(folder).parts
    if ".." in parts:
        raise UnsafeFolderError(
            f"folder не должен содержать '..' (path-traversal): {folder!r}"
        )

    # (4) Whitelist допустимых символов (буквы/цифры/_/-/пробел/слэш).
    if _SAFE_FOLDER_PATTERN.fullmatch(folder) is None:
        raise UnsafeFolderError(
            f"folder содержит недопустимые символы: {folder!r}"
        )

    # (5) Финальная проверка контейнмента по resolved-путям (ловит символьные
    # ссылки и любые остаточные способы выхода за пределы vault).
    vault_resolved = vault_path.resolve()
    target_dir = (vault_resolved / folder).resolve()
    try:
        target_dir.relative_to(vault_resolved)
    except ValueError:
        raise UnsafeFolderError(
            f"folder выходит за пределы vault: {folder!r} → {target_dir}"
        )

    return target_dir


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

    def __init__(
        self,
        data_dir: Path | None = None,
        event_bus=None,
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._data_dir: Path | None = Path(data_dir) if data_dir is not None else None
        self._vault_path: Path | None = None
        self._folder: str = _DEFAULT_FOLDER
        self._last_sync_ts: str | None = None
        self._lock = threading.Lock()
        self._event_bus = event_bus
        # Optional runtime settings provider (e.g. BackendService._get_runtime_setting).
        # Falls back to always returning the default when not provided (same
        # pattern as AppleIntegrationService).
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda key, default: default)

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
        Вызывает ValueError если vault_path не существует, не является
        директорией, или folder выходит за пределы vault (path-traversal,
        W1768 — UnsafeFolderError, подкласс ValueError).

        SECURITY NOTE (LOW, confused-deputy): vault_path should always be a
        user-confirmed location.  Krab Ear will write transcript_*.md files
        there on every sync.  Callers must ensure the path was explicitly
        chosen by the user (e.g. via a file-chooser dialog), not derived from
        untrusted IPC input.  An audit WARNING is emitted here so every
        vault-path change is visible in the log.
        """
        p = Path(vault_path).expanduser().resolve()
        if not p.exists():
            raise ValueError(f"Vault path не существует: {vault_path!r}")
        if not p.is_dir():
            raise ValueError(f"Vault path должен быть директорией: {vault_path!r}")

        # W19 (LOW, confused-deputy audit): log every vault_path change so the
        # write target is auditable.  This is intentionally a WARNING so it
        # surfaces in default log configs and can be cross-checked by the user.
        logger.warning(
            "ObsidianSync: vault_path настроен/изменён → %s (folder=%r). "
            "Убедитесь, что этот путь подтверждён пользователем.",
            p,
            folder.strip() or _DEFAULT_FOLDER,
        )

        folder = folder.strip() or _DEFAULT_FOLDER

        # W1768 (MED, path-traversal): валидируем folder ДО любого mkdir/write.
        # Бросает UnsafeFolderError (ValueError) если folder абсолютный, содержит
        # ".." или иным образом выходит за пределы vault — директория НЕ создаётся.
        target_dir = _validate_and_resolve_folder(p, folder)

        with self._lock:
            # Validate target dir BEFORE committing _vault_path (W603 fix).
            target_dir.mkdir(parents=True, exist_ok=True)

            self._vault_path = p
            self._folder = folder
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

        # wave-32 MED DoS: cap items to prevent disk-fill on large histories.
        # Truncate BEFORE the path-traversal check so the cap is applied even on
        # force-sync. Log a warning so operators notice the truncation.
        if len(items) > MAX_SYNC_ITEMS:
            logger.warning(
                "ObsidianSync.sync: items truncated %d → %d (MAX_SYNC_ITEMS cap)",
                len(items),
                MAX_SYNC_ITEMS,
            )
            items = items[:MAX_SYNC_ITEMS]

        result = SyncResult()
        # W1768 (MED, path-traversal): _folder перезагружается из obsidian_sync.json
        # при рестарте (_load_state), поэтому НЕ доверяем ему и повторно проверяем
        # ТОТ ЖЕ инвариант контейнмента перед mkdir/write. Если состояние было
        # подделано (folder='../../etc'), sync() откажет вместо записи вне vault.
        target_dir = _validate_and_resolve_folder(vault_path, folder)
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
                    if _ts_le(item_ts, last_sync_ts):
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
            # W24 (LOW, containment): re-validate folder containment before
            # reading the filesystem.  The persisted folder value may have been
            # tampered with (e.g. "../../etc") between configure() and
            # get_sync_status().  Return 0 rather than reading outside the vault.
            try:
                target_dir = _validate_and_resolve_folder(vault_path, folder)
            except ValueError as exc:
                logger.warning(
                    "get_sync_status: небезопасный folder %r, file_count=0: %s",
                    folder,
                    exc,
                )
                target_dir = None
            if target_dir is not None and target_dir.exists():
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
        """IPC-обработчик configure_obsidian_sync.

        wave-32 MED (vault_path confused-deputy): this is the IPC entrypoint;
        validate that vault_path is under $HOME and not inside a known sensitive
        system subdirectory BEFORE delegating to configure().  An untrusted IPC
        caller could supply vault_path=/Users/victim/.ssh/ to write transcript
        .md files into SSH key directories.  configure() itself is also called
        by internal code that may legitimately use paths outside $HOME (e.g.
        test fixtures), so the guard lives here at the IPC boundary only.

        Two-layer check:
          1. If path is under $HOME → also check it is not inside a forbidden
             sensitive subdir (e.g. ~/.ssh, ~/.gnupg, Library/Keychains).
          2. If path is outside $HOME → reject entirely (arbitrary write location).
        """
        vault_path = params.get("vault_path")
        if not vault_path:
            raise ValueError("Параметр vault_path обязателен")

        # Resolve the path once so symlinks are followed before comparison.
        p = Path(str(vault_path)).expanduser().resolve()
        home = Path.home().resolve()

        # wave-32 MED (vault_path confused-deputy): path guard.
        #
        # Reject paths inside well-known sensitive subdirs of $HOME (e.g.
        # ~/.ssh/ — writing there can corrupt known_hosts and break SSH auth;
        # ~/.gnupg/ — leaks GPG key material; Library/Keychains — macOS secrets).
        # Also reject paths outside $HOME entirely (e.g. /etc, /var) — arbitrary
        # system-owned directories are never valid Obsidian vault locations.
        #
        # We use Path.relative_to() for containment checks (not str.startswith)
        # because a sibling-prefix path (/home/user_evil) would bypass a naive
        # string comparison against /home/user.
        _under_home: bool
        try:
            p.relative_to(home)
            _under_home = True
        except ValueError:
            _under_home = False

        if not _under_home:
            raise ValueError(
                f"vault_path must be under the home directory: {p}"
            )

        for _forbidden in _FORBIDDEN_HOME_SUBDIRS:
            try:
                p.relative_to(home / _forbidden)
                raise ValueError(
                    f"vault_path is inside a restricted directory ({_forbidden}): {p}"
                )
            except ValueError as _ve:
                if "restricted directory" in str(_ve):
                    raise

        folder = str(params.get("folder", _DEFAULT_FOLDER))
        return self.configure(str(vault_path), folder)

    def handle_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик run_obsidian_sync.

        Принимает items (список dict) и опциональный force (bool).
        """
        # C3a wave (sibling-gate asymmetry): handle_create_apple_note уже гейтит
        # privacy_mode_enabled (apple_integration_service.py) — sync() пишет
        # transcript-текст в файлы vault'а тем же классом риска и обязана
        # гейтиться идентично, ДО любой записи на диск.
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "ok": False,
                "error": "privacy_mode_active",
                "user_msg": "Приватный режим включён — синхронизация с Obsidian запрещена.",
            }
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

        # Строим frontmatter (F1: все строковые значения экранируем через _yaml_scalar)
        lines: list[str] = ["---"]
        lines.append(f"title: {_yaml_scalar('Транскрипция ' + date_str)}")
        lines.append(f"date: {_yaml_scalar(datetime_str)}")
        lines.append(f"id: {_yaml_scalar(str(item_id))}")
        lines.append("tags:")
        for tag in yaml_tags:
            lines.append(f"  - {_yaml_scalar(tag)}")
        if source_lang:
            lines.append(f"source_lang: {_yaml_scalar(str(source_lang))}")
        if target_lang:
            lines.append(f"target_lang: {_yaml_scalar(str(target_lang))}")
        if confidence is not None:
            # W24 (LOW, silent-skip): coerce confidence defensively so a
            # non-numeric value (e.g. a string "high") does not raise ValueError
            # and cause the entire item to be silently skipped during sync.
            try:
                confidence_f = float(confidence)
            except (TypeError, ValueError):
                confidence_f = 0.0
            lines.append(f"confidence: {confidence_f:.3f}")
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
                    # W1769: sanitize speaker name + turn text to prevent
                    # YAML-frontmatter boundary injection in .md body.
                    speaker = _sanitize_speaker_name(turn.get("speaker", "Спикер"))
                    turn_text = _sanitize_md_body_text(str(turn.get("text", "")))
                    start = turn.get("start", 0.0)
                    # Форматируем время как HH:MM:SS
                    h = int(start // 3600)
                    m = int((start % 3600) // 60)
                    s = int(start % 60)
                    timestamp = f"{h:02d}:{m:02d}:{s:02d}"
                    lines.append(f"**[{speaker} ({timestamp})]** {turn_text}")
                    lines.append("")
            else:
                lines.append(f"[Спикер (00:00:00)] {_sanitize_md_body_text(text)}")
                lines.append("")
        else:
            # Без диаризации — стандартный формат
            lines.append(f"[Спикер (00:00:00)] {_sanitize_md_body_text(text)}")
            lines.append("")

        # Секция перевода (если есть)
        if translated_text and translation_mode != "off":
            lines.append("## Перевод")
            lines.append("")
            lines.append(_sanitize_md_body_text(translated_text))
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

    # ------------------------------------------------------------------
    # Privacy purge
    # ------------------------------------------------------------------

    def purge_all_synced_files(self) -> int:
        """Удалить все .md файлы, синхронизированные с Obsidian vault (privacy-wipe).

        #10 (MED W1766): файлы транскрипций в Obsidian vault содержат полный
        текст STT-записей и переживают стандартный privacy-purge истории.

        Алгоритм:
        1. Если vault не настроен (_vault_path is None) — no-op (возвращает 0).
        2. Удаляет все *.md файлы в vault_path/folder (target_dir).
        3. Сбрасывает _last_sync_ts и сохраняет состояние (obsidian_sync.json
           перезаписывается — last_sync_ts=null, vault_path и folder сохраняются
           как конфиг, не как PII).

        Возвращает:
            int — количество успешно удалённых файлов.

        W19 (LOW, privacy silent-failure): если хотя бы один .md не удалось
        удалить, после завершения цикла (все попытки выполнены) бросает
        OSError с перечнем путей. Вызывающая сторона (handle_purge_all_data в
        history_service.py) оборачивает вызов в try/except и записывает
        "obsidian" в secondary_errors — таким образом частичный сбой
        не остаётся незамеченным, а PII-файлы не теряются молча.
        Failure путь-traversal (ValueError из _validate_and_resolve_folder)
        по-прежнему не бросает — это безопасный no-op.
        """
        with self._lock:
            vault_path = self._vault_path
            folder = self._folder

        if vault_path is None:
            logger.debug("ObsidianSyncManager.purge_all_synced_files: vault не настроен, no-op")
            return 0

        # W1768 (MED, path-traversal): повторно проверяем тот же инвариант перед
        # удалением — folder перезагружается из state-файла и не является
        # доверенным. Если он выходит за пределы vault — безопасный no-op.
        try:
            target_dir = _validate_and_resolve_folder(vault_path, folder)
        except ValueError as exc:
            logger.warning(
                "ObsidianSyncManager.purge_all_synced_files: "
                "небезопасный folder %r, purge пропущен: %s",
                folder,
                exc,
            )
            return 0

        deleted = 0
        failed_paths: list[str] = []

        if target_dir.is_dir():
            for md_path in list(target_dir.glob("*.md")):
                try:
                    md_path.unlink(missing_ok=True)
                    deleted += 1
                except OSError as exc:
                    # W19 (LOW, privacy silent-failure): log each failure at
                    # WARNING so the path is visible, then collect for the
                    # raise below — we continue the loop to delete as many
                    # files as possible before surfacing the aggregate error.
                    logger.warning(
                        "ObsidianSyncManager.purge_all_synced_files: "
                        "не удалось удалить %s: %s",
                        md_path,
                        exc,
                    )
                    failed_paths.append(str(md_path))

        # wave-32 LOW (purge-gap): delete obsidian_sync.json so vault_path and
        # last_sync_ts do not survive a privacy-wipe across restarts. The file
        # persists sync metadata (vault location + last-sync timestamp); removing
        # it ensures no record of where the vault lives survives after a restart.
        # In-memory _vault_path/_folder are NOT cleared so sync can continue in
        # the current session without requiring re-configure; but the state will
        # not be loaded from disk on next startup (clean slate).
        with self._lock:
            self._last_sync_ts = None
            if self._state_path is not None:
                try:
                    self._state_path.unlink(missing_ok=True)
                    logger.info(
                        "ObsidianSyncManager.purge_all_synced_files: "
                        "obsidian_sync.json deleted (privacy-wipe)"
                    )
                except OSError as exc:
                    logger.warning(
                        "ObsidianSyncManager.purge_all_synced_files: "
                        "failed to delete obsidian_sync.json: %s",
                        exc,
                    )
                    failed_paths.append(str(self._state_path))

        logger.info(
            "ObsidianSyncManager.purge_all_synced_files: deleted %d .md files from %s"
            " (deletion failures: %d)",
            deleted,
            target_dir,
            len(failed_paths),
        )

        # W19 (LOW): raise AFTER the loop and after resetting state so the
        # caller (history_service.handle_purge_all_data) records "obsidian"
        # in secondary_errors and the partial failure is visible.
        if failed_paths:
            raise OSError(
                f"purge_all_synced_files: failed to delete {len(failed_paths)} file(s): "
                + ", ".join(failed_paths)
            )

        return deleted

    def _sanitize_loaded_folder(self, folder: str) -> str:
        """Вернуть безопасный folder из persisted state или _DEFAULT_FOLDER.

        W1768 (MED): folder, загруженный из obsidian_sync.json, мог быть подделан.
        Если он не проходит проверку контейнмента относительно текущего vault
        (или vault ещё не известен и folder сам по себе небезопасен) — логируем
        предупреждение и возвращаем _DEFAULT_FOLDER, чтобы sync() позже не записал
        файлы вне vault.
        """
        try:
            base = self._vault_path if self._vault_path is not None else Path(".")
            _validate_and_resolve_folder(base, folder)
            return folder.strip() or _DEFAULT_FOLDER
        except ValueError as exc:
            logger.warning(
                "ObsidianSync: небезопасный folder %r в state-файле, "
                "откат к %r: %s",
                folder,
                _DEFAULT_FOLDER,
                exc,
            )
            return _DEFAULT_FOLDER

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
            # W1768 (MED, path-traversal): folder из state-файла может быть подделан
            # (например внешним процессом). Валидируем при загрузке: если он выходит
            # за пределы vault (или vault ещё не настроен и folder сам по себе
            # небезопасен) — откатываемся к безопасному значению по умолчанию.
            loaded_folder = str(raw.get("folder", _DEFAULT_FOLDER))
            self._folder = self._sanitize_loaded_folder(loaded_folder)
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
