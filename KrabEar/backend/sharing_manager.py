"""SharingManager — подготовка пакетов для шаринга транскрипций Krab Ear.

Позволяет упаковывать одну или несколько записей истории в текстовый пакет
(markdown / text / json) и сохранять их в {data_dir}/shares/.

Wave 158: добавлены TTL (expires_at) и revoke API для устранения privacy gap
(токен шаринга без TTL = постоянная утечка данных).
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import secrets
import string
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("KrabEar.Backend.SharingManager")

_BASE62_CHARS = string.ascii_letters + string.digits  # 62 символа
_SHARE_ID_LEN = 8
_SHARES_DIR = "shares"
_SHARES_INDEX_FILE = "shares_index.json"

# Default TTL: 7 days in hours
DEFAULT_SHARE_TTL_HOURS: int = 168

# W1244: TTL upper bound (1 year) — prevents infinite/astronomically-large expiry timestamps
_MAX_TTL_HOURS: int = 24 * 365  # 1 year

# W1245: item_ids cap — prevents memory bomb from unbounded list
_MAX_SHARE_ITEMS: int = 1000

SUPPORTED_FORMATS = ("markdown", "text", "json")


@dataclass
class SharePackage:
    """Пакет для шаринга транскрипций."""

    share_id: str
    content: str
    filename: str
    size_bytes: int
    created_at: str  # ISO-8601
    expires_at: Optional[float] = None  # Unix timestamp; None = no expiry
    is_revoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SharingManager:
    """Управляет подготовкой и хранением пакетов для шаринга транскрипций.

    Пакеты хранятся в {data_dir}/shares/ как текстовые файлы.
    Индекс пакетов — {data_dir}/shares/shares_index.json.
    """

    def __init__(
        self,
        store: Any,
        default_share_ttl_hours: int = DEFAULT_SHARE_TTL_HOURS,
        share_no_default_ttl: bool = False,
        privacy_mode_fn: Optional[Any] = None,
    ) -> None:
        self._store = store
        self._data_dir = Path(getattr(store, "data_dir", "."))
        self._shares_dir = self._data_dir / _SHARES_DIR
        self._index_path = self._shares_dir / _SHARES_INDEX_FILE
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = {}
        self._default_share_ttl_hours = default_share_ttl_hours
        self._share_no_default_ttl = share_no_default_ttl
        # wave-25 A2: callable returning bool — checked at IPC boundary in handle_prepare_share.
        # None → guard disabled (privacy_mode treated as False).
        from typing import Callable as _Callable
        self._privacy_mode_fn: Optional[_Callable[[], bool]] = privacy_mode_fn
        # W1767 #16: директория shares/ должна быть 0o700 (только владелец).
        # parents=True создаёт промежуточные директории с дефолтными правами;
        # окончательный chmod применяется явно.
        self._shares_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._shares_dir, 0o700)
        except Exception as exc:
            logger.warning("Не удалось установить права 0o700 на %s: %s", self._shares_dir, exc)
        self._load_index()

    # ------------------------------------------------------------------
    # Персистентность индекса
    # ------------------------------------------------------------------

    def _load_index(self) -> None:
        """Загружает индекс пакетов из файла."""
        try:
            if self._index_path.exists():
                raw = self._index_path.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    self._index = loaded
        except Exception as exc:
            logger.warning("Не удалось загрузить индекс shares: %s", exc)

    def _save_index(self) -> None:
        """Сохраняет индекс пакетов атомарно с правами 0o600 (W1767 #16)."""
        try:
            tmp = self._index_path.with_suffix(".tmp")
            # Открываем через os.open с O_CREAT|O_WRONLY|O_TRUNC и mode=0o600,
            # чтобы файл с первой записи имел правильные права (не 0o644).
            fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(self._index, ensure_ascii=False, indent=2))
            except Exception:
                # fdopen берёт владение fd; при исключении файл уже закрыт
                raise
            tmp.replace(self._index_path)
        except Exception as exc:
            logger.error("Не удалось сохранить индекс shares: %s", exc)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def generate_share_id(self) -> str:
        """Генерирует короткий уникальный ID (8 символов, base62, криптографически стойкий).

        Использует secrets.choice вместо random.choices (Mersenne Twister) для
        предотвращения предсказуемости токенов шаринга (W931 F1 MEDIUM).
        """
        return "".join(secrets.choice(_BASE62_CHARS) for _ in range(_SHARE_ID_LEN))

    def prepare_share(
        self,
        item_ids: list[str],
        format: str = "markdown",
        include_translation: bool = True,
        ttl_hours: Optional[float] = None,
    ) -> SharePackage:
        """Упаковывает записи истории в SharePackage.

        Args:
            item_ids: список ID записей истории для включения в пакет.
            format: формат пакета — "markdown", "text" или "json".
            include_translation: включать ли поля перевода.
            ttl_hours: срок жизни пакета в часах. None = использовать дефолт
                (если share_no_default_ttl=False, иначе бессрочно).

        Returns:
            SharePackage с готовым контентом и метаданными.

        Raises:
            ValueError: если format не поддерживается или item_ids пустой.
        """
        if not item_ids:
            raise ValueError("item_ids не может быть пустым")
        # W1245: cap item_ids to avoid memory bomb
        if len(item_ids) > _MAX_SHARE_ITEMS:
            raise ValueError(
                f"too many item_ids: {len(item_ids)} > {_MAX_SHARE_ITEMS}"
            )
        fmt = format.strip().lower()
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Неподдерживаемый формат: {format!r}. Допустимые: {SUPPORTED_FORMATS}"
            )

        # W1244: validate and clamp ttl_hours before resolving
        if ttl_hours is not None:
            ttl_hours = float(ttl_hours)
            if not math.isfinite(ttl_hours):
                raise ValueError(
                    "ttl_hours должен быть конечным числом (не inf/nan)"
                )
            if ttl_hours < 0:
                ttl_hours = 0.0
            ttl_hours = min(ttl_hours, float(_MAX_TTL_HOURS))

        # Вычисляем expires_at
        effective_ttl = self._resolve_ttl(ttl_hours)
        expires_at: Optional[float] = None
        if effective_ttl is not None:
            expires_at = time.time() + effective_ttl * 3600.0

        items = self._fetch_items(item_ids)
        content = self._render(items, fmt, include_translation)

        ext = {"markdown": "md", "text": "txt", "json": "json"}[fmt]
        created_at = datetime.now(tz=timezone.utc).isoformat()
        size_bytes = len(content.encode("utf-8"))

        # W1767 #17 (TOCTOU): генерируем share_id и резервируем его в индексе
        # под одним локом, чтобы параллельные вызовы не могли выбрать один ID.
        with self._lock:
            share_id = self._unique_share_id_locked()
            filename = f"krabear_share_{share_id}.{ext}"
            package = SharePackage(
                share_id=share_id,
                content=content,
                filename=filename,
                size_bytes=size_bytes,
                created_at=created_at,
                expires_at=expires_at,
                is_revoked=False,
            )
            # Резервируем слот в индексе немедленно — _persist_package обновит его
            # повторно после записи файла, но слот уже «занят».
            self._index[share_id] = {"share_id": share_id, "_reserved": True}

        self._persist_package(package)
        return package

    def list_shared(self, include_expired: bool = False, include_revoked: bool = False) -> list[dict[str, Any]]:
        """Возвращает список сохранённых пакетов (без content).

        По умолчанию исключает истёкшие и отозванные пакеты.
        """
        now = time.time()
        with self._lock:
            result = []
            for entry in self._index.values():
                # W1767 #17: пропускаем временные «reserved» placeholders
                if entry.get("_reserved"):
                    continue
                if not include_revoked and entry.get("is_revoked", False):
                    continue
                expires_at = entry.get("expires_at")
                if not include_expired and expires_at is not None and now > expires_at:
                    continue
                result.append({k: v for k, v in entry.items() if k not in ("content", "_reserved")})
            result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return result

    def _find_share_by_token_constant_time(self, token: str) -> dict[str, Any] | None:
        """Ищет запись в индексе по токену за константное время.

        Итерирует ВСЕ записи без short-circuit, чтобы не давать timing-oracle
        атакующему информацию о существовании токена.

        Должна вызываться под self._lock.
        """
        found: dict[str, Any] | None = None
        for known_token, entry in self._index.items():
            if hmac.compare_digest(known_token, token):
                found = entry
        return found

    def get_shared(self, share_id: str) -> SharePackage | None:
        """Возвращает SharePackage по ID, или None если не найден / истёк / отозван."""
        now = time.time()
        with self._lock:
            entry = self._find_share_by_token_constant_time(share_id)
            if entry is None:
                return None
            # W1767 #17: пропускаем временный «reserved» placeholder (запись ещё записывается)
            if entry.get("_reserved"):
                return None
            # Проверяем отзыв
            if entry.get("is_revoked", False):
                return None
            # Проверяем TTL
            expires_at = entry.get("expires_at")
            if expires_at is not None and now > expires_at:
                return None
            # Фильтруем служебные поля, чтобы SharePackage(**entry) не упал
            pkg_fields = {k: v for k, v in entry.items() if k != "_reserved"}
            return SharePackage(**pkg_fields)

    def revoke_share(self, token: str) -> bool:
        """Отзывает пакет по share_id (токену) и УДАЛЯЕТ файлы с диска.

        После отзыва get_shared возвращает None для этого токена.
        Очистка файла производится ДО обновления индекса — если удаление
        не удалось, метод бросает исключение и НЕ помечает запись как отозванную
        (честный репорт ошибки, а не молчаливый privacy провал).

        Returns:
            True если пакет существовал и был успешно отозван, False если не найден.

        Raises:
            RuntimeError: если файл на диске не удалось удалить.
        """
        with self._lock:
            entry = self._find_share_by_token_constant_time(token)
            if entry is None:
                return False
            share_id = entry["share_id"]

            # W1762 HIGH FIX: удаляем файл(ы) пакета с диска перед обновлением индекса.
            # Если удаление не удалось — сообщаем громко, не помечаем как отозванный.
            filename = entry.get("filename", "")
            if filename:
                file_path = self._shares_dir / filename
                try:
                    file_path.unlink(missing_ok=True)
                except Exception as exc:
                    # Частичный сбой: файл существует, но удалить не удалось.
                    # НЕ помечаем как отозванный — открытый текст остаётся на диске.
                    logger.error(
                        "revoke_share: не удалось удалить файл пакета %s (share_id=%s): %s",
                        file_path,
                        share_id,
                        exc,
                    )
                    raise RuntimeError(
                        f"Не удалось удалить файл пакета '{file_path}' при отзыве "
                        f"share_id={share_id!r}: {exc}"
                    ) from exc

            self._index[share_id]["is_revoked"] = True
            # W1767 #15: удаляем чувствительные текстовые поля из записи индекса
            # после отзыва, чтобы транскрипция не оставалась в shares_index.json.
            # Метаданные (share_id, filename, created_at, expires_at) сохраняются
            # как tombstone для аудита.
            for _sensitive_field in ("content", "text", "translated_text"):
                self._index[share_id].pop(_sensitive_field, None)
            self._save_index()
            return True

    def purge_all(self) -> dict[str, int]:
        """Удаляет ВСЕ файлы пакетов с диска и очищает индекс.

        Предназначен для privacy-purge (например, «удалить всё»).
        Частичные ошибки удаления логируются как предупреждения, но операция
        продолжается — в итоге возвращается статистика.

        Returns:
            dict с ключами:
                "deleted"  — количество успешно удалённых файлов,
                "errors"   — количество файлов, которые не удалось удалить,
                "cleared"  — 1 если индекс очищен, 0 при ошибке сохранения индекса.
        """
        with self._lock:
            deleted = 0
            errors = 0
            for entry in list(self._index.values()):
                filename = entry.get("filename", "")
                if not filename:
                    continue
                file_path = self._shares_dir / filename
                try:
                    file_path.unlink(missing_ok=True)
                    deleted += 1
                except Exception as exc:
                    errors += 1
                    logger.warning(
                        "purge_all: не удалось удалить файл пакета %s: %s",
                        file_path,
                        exc,
                    )

            self._index.clear()
            cleared = 0
            try:
                self._save_index()
                cleared = 1
            except Exception as exc:
                logger.error("purge_all: не удалось сохранить пустой индекс: %s", exc)

            logger.info(
                "purge_all: удалено файлов=%d ошибок=%d индекс_очищен=%s",
                deleted,
                errors,
                bool(cleared),
            )
            return {"deleted": deleted, "errors": errors, "cleared": cleared}

    def get_share_package_by_token(self, token: str) -> SharePackage | None:
        """Alias для get_shared (более явное именование для токен-ориентированного API)."""
        return self.get_shared(token)

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_prepare_share(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: подготовить пакет для шаринга.

        Privacy gate (wave-25 A2): когда privacy_mode_enabled=True — шаринг
        отключён; транскрипция НЕ записывается на диск.
        """
        # wave-25 A2: privacy gate — sharing disabled in privacy mode
        if self._privacy_mode_fn is not None and self._privacy_mode_fn():
            return {
                "ok": False,
                "reason": "privacy_mode_active",
                "error": "Sharing disabled in privacy mode",
            }
        item_ids = params.get("item_ids")
        if not isinstance(item_ids, list) or not item_ids:
            raise RuntimeError("Параметр 'item_ids' должен быть непустым списком")
        # W1245: item_ids cap — guard at IPC boundary before calling prepare_share
        if len(item_ids) > _MAX_SHARE_ITEMS:
            raise RuntimeError(
                f"too many item_ids: {len(item_ids)} > {_MAX_SHARE_ITEMS}"
            )
        fmt = str(params.get("format", "markdown")).strip()
        include_translation = bool(params.get("include_translation", True))
        ttl_hours_raw = params.get("ttl_hours")
        # W1244: when ttl_hours absent from IPC params, apply 1h IPC default (not the 168h store default)
        if ttl_hours_raw is None:
            ttl_hours: Optional[float] = 1.0
        else:
            ttl_hours = float(ttl_hours_raw)
        # W1244: TTL validation at IPC boundary — raise RuntimeError (not ValueError) for IPC callers
        if ttl_hours is not None:
            if not math.isfinite(ttl_hours):
                raise RuntimeError(
                    "ttl_hours должен быть конечным числом (не inf/nan)"
                )
            if ttl_hours < 0:
                ttl_hours = 0.0
            ttl_hours = min(ttl_hours, float(_MAX_TTL_HOURS))
        # W1244 F5: pre-check if any items resolve — used to set warning after
        resolved_items = self._fetch_items(item_ids)
        try:
            package = self.prepare_share(
                item_ids,
                format=fmt,
                include_translation=include_translation,
                ttl_hours=ttl_hours,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        result = package.to_dict()
        # Add warning when none of the item_ids resolved to actual history items
        if not resolved_items:
            result["warning"] = "no_items_found"
        return result

    def handle_list_shared(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: список сохранённых пакетов."""
        include_expired = bool(params.get("include_expired", False))
        include_revoked = bool(params.get("include_revoked", False))
        return {"shares": self.list_shared(include_expired=include_expired, include_revoked=include_revoked)}

    def handle_get_shared(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: получить пакет по share_id."""
        share_id = str(params.get("share_id", "")).strip()
        if not share_id:
            raise RuntimeError("Параметр 'share_id' обязателен")
        package = self.get_shared(share_id)
        if package is None:
            raise RuntimeError(f"Пакет не найден: {share_id!r}")
        return package.to_dict()

    def handle_revoke_share_link(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: отозвать пакет шаринга по токену (share_id)."""
        token = str(params.get("token", params.get("share_id", ""))).strip()
        if not token:
            raise RuntimeError("Параметр 'token' (или 'share_id') обязателен")
        revoked = self.revoke_share(token)
        return {"revoked": revoked, "token": token}

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _resolve_ttl(self, ttl_hours: Optional[float]) -> Optional[float]:
        """Определяет эффективный TTL (в часах) для нового пакета.

        Приоритет:
        1. Явно переданный ttl_hours (включая 0 — мгновенный expiry).
        2. Если share_no_default_ttl=True — None (бессрочно).
        3. Иначе — self._default_share_ttl_hours.
        """
        if ttl_hours is not None:
            return ttl_hours
        if self._share_no_default_ttl:
            return None
        return float(self._default_share_ttl_hours)

    def _fetch_items(self, item_ids: list[str]) -> list[Any]:
        """Получает записи истории из store по списку ID.

        Записи, не найденные в store, пропускаются с предупреждением.
        """
        items = []
        for item_id in item_ids:
            item = None
            # StateStore предоставляет get_history_item_by_id (или аналог)
            if hasattr(self._store, "get_history_item_by_id"):
                item = self._store.get_history_item_by_id(item_id)
            if item is None:
                logger.warning("Запись не найдена при формировании пакета: %s", item_id)
            else:
                items.append(item)
        return items

    def _render(self, items: list[Any], fmt: str, include_translation: bool) -> str:
        """Рендерит список записей в строку нужного формата."""
        if fmt == "json":
            return self._render_json(items, include_translation)
        elif fmt == "markdown":
            return self._render_markdown(items, include_translation)
        else:
            return self._render_text(items, include_translation)

    def _render_json(self, items: list[Any], include_translation: bool) -> str:
        rows = []
        for item in items:
            d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            if not include_translation:
                d.pop("translated_text", None)
                d.pop("translation_mode", None)
                d.pop("source_lang", None)
                d.pop("target_lang", None)
                d.pop("translation_status", None)
                d.pop("translation_engine", None)
            rows.append(d)
        return json.dumps(rows, ensure_ascii=False, indent=2)

    def _render_markdown(self, items: list[Any], include_translation: bool) -> str:
        lines = ["# Krab Ear — экспорт транскрипций\n"]
        for idx, item in enumerate(items, 1):
            d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            ts = d.get("ts", "")
            text = d.get("text", "")
            lines.append(f"## {idx}. {ts}")
            lines.append(f"\n{text}\n")
            if include_translation:
                translated = d.get("translated_text", "")
                if translated:
                    src_lang = d.get("source_lang", "")
                    tgt_lang = d.get("target_lang", "")
                    lines.append(f"> **Перевод** ({src_lang}→{tgt_lang}): {translated}\n")
        return "\n".join(lines)

    def _render_text(self, items: list[Any], include_translation: bool) -> str:
        parts = []
        for idx, item in enumerate(items, 1):
            d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            ts = d.get("ts", "")
            text = d.get("text", "")
            parts.append(f"[{idx}] {ts}\n{text}")
            if include_translation:
                translated = d.get("translated_text", "")
                if translated:
                    parts.append(f"  Перевод: {translated}")
        return "\n\n".join(parts)

    def _persist_package(self, package: SharePackage) -> None:
        """Сохраняет пакет на диск и обновляет индекс.

        W1762 MED FIX: гарантирует консистентность «файл ↔ индекс».
        Алгоритм:
        1. Записываем файл на диск (вне лока — медленная IO).
        2. Если запись завершилась с ошибкой — удаляем частичный файл
           и перебрасываем исключение. В индекс ничего не попадает.
        3. Только после успешной записи — берём лок и добавляем запись в индекс.
        Таким образом никогда не возникает «orphan file без index entry».
        """
        file_path = self._shares_dir / package.filename
        # W1767 #16: записываем файл пакета с правами 0o600 через os.open,
        # чтобы другие процессы на том же хосте не могли прочитать транскрипцию.
        try:
            fd = os.open(str(file_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(package.content)
            except Exception:
                raise
        except Exception as exc:
            logger.error(
                "Не удалось сохранить файл пакета %s: %s",
                file_path,
                exc,
            )
            # Зачищаем частичный файл, чтобы не оставлять orphan на диске
            try:
                file_path.unlink(missing_ok=True)
            except Exception as cleanup_exc:
                logger.warning(
                    "Не удалось удалить частичный файл %s после ошибки записи: %s",
                    file_path,
                    cleanup_exc,
                )
            # W1767 #17: освобождаем зарезервированный слот в индексе, чтобы
            # после ошибки записи в индексе не оставался placeholder.
            with self._lock:
                self._index.pop(package.share_id, None)
                self._save_index()
            raise RuntimeError(
                f"Не удалось записать файл пакета '{file_path}': {exc}"
            ) from exc

        # W1767 #17 (TOCTOU): регистрируем в индексе под тем же локом, который
        # был захвачен при генерации share_id в prepare_share → check-then-write атомарен.
        # Запись прошла успешно — добавляем в индекс.
        with self._lock:
            self._index[package.share_id] = package.to_dict()
            self._save_index()

    def _unique_share_id_locked(self) -> str:
        """Генерирует share_id, отсутствующий в индексе. ДОЛЖЕН вызываться под self._lock.

        W1767 #17: вся проверка «id не занят» и последующая вставка в индекс выполняются
        в одной критической секции, что устраняет TOCTOU при параллельных prepare_share.
        """
        for _ in range(20):
            sid = self.generate_share_id()
            if sid not in self._index:
                return sid
        # Крайне маловероятно, но добавляем timestamp-суффикс для надёжности
        return self.generate_share_id() + str(int(datetime.now().timestamp()))[-4:]

    def _unique_share_id(self) -> str:
        """Генерирует share_id, гарантированно отсутствующий в индексе.

        Устаревший helper без лока — оставлен для обратной совместимости.
        Новый код должен использовать _unique_share_id_locked() под self._lock.
        """
        for _ in range(20):
            sid = self.generate_share_id()
            if sid not in self._index:
                return sid
        # Крайне маловероятно, но добавляем timestamp-суффикс для надёжности
        return self.generate_share_id() + str(int(datetime.now().timestamp()))[-4:]
