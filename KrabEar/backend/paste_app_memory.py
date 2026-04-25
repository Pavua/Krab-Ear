"""PasteAppMemory — память профилей вставки по приложениям.

Хранит ассоциацию bundle_id → paste_profile (markdown/plain/html/etc).
Persistence: JSON-файл paste_app_memory.json в data_dir.
Автоочистка записей, не использовавшихся >180 дней.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Профили вставки, поддерживаемые PasteFormatter / frontend
VALID_PROFILES: frozenset[str] = frozenset({
    "plain",
    "markdown",
    "html",
    "telegram",
    "email",
    "notes",
})

# Записи не обновлявшиеся дольше этого срока удаляются при cleanup
_STALE_DAYS = 180

_FILE_NAME = "paste_app_memory.json"


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class PasteAppMemory:
    """Запоминает предпочтительный профиль вставки для каждого приложения."""

    def __init__(
        self,
        data_dir: Path,
        enabled: bool = True,
        stale_days: int = _STALE_DAYS,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._enabled = enabled
        self._stale_days = stale_days
        self._path = self._data_dir / _FILE_NAME
        self._lock = threading.Lock()
        # {bundle_id: {profile, last_used_iso}}
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def record(self, bundle_id: str, profile: str) -> None:
        """Записывает/обновляет профиль для bundle_id."""
        if not self._enabled:
            return
        if not bundle_id or not bundle_id.strip():
            return
        if profile not in VALID_PROFILES:
            raise ValueError(f"Неизвестный профиль вставки: {profile!r}. Допустимые: {sorted(VALID_PROFILES)}")
        with self._lock:
            self._data[bundle_id] = {
                "profile": profile,
                "last_used": _utcnow_iso(),
            }
            self._save()

    def get_profile_for(self, bundle_id: str) -> str | None:
        """Возвращает профиль для bundle_id, или None если не найден / не включено."""
        if not self._enabled:
            return None
        if not bundle_id:
            return None
        with self._lock:
            entry = self._data.get(bundle_id)
        if entry is None:
            return None
        # Обновляем last_used при чтении (silent write)
        with self._lock:
            if bundle_id in self._data:
                self._data[bundle_id]["last_used"] = _utcnow_iso()
                self._save()
        return entry["profile"]

    def list_profiles(self) -> list[dict[str, str]]:
        """Возвращает все сохранённые ассоциации bundle→profile."""
        with self._lock:
            return [
                {"bundle_id": bid, "profile": v["profile"], "last_used": v["last_used"]}
                for bid, v in sorted(self._data.items())
            ]

    def delete(self, bundle_id: str) -> bool:
        """Удаляет запись для bundle_id. Возвращает True если запись существовала."""
        with self._lock:
            if bundle_id not in self._data:
                return False
            del self._data[bundle_id]
            self._save()
        return True

    def cleanup_stale(self) -> int:
        """Удаляет записи не использовавшиеся дольше stale_days. Возвращает кол-во удалённых."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self._stale_days)
        removed = 0
        with self._lock:
            stale = [
                bid for bid, v in self._data.items()
                if self._parse_dt(v.get("last_used", "")) < cutoff
            ]
            for bid in stale:
                del self._data[bid]
                removed += 1
            if removed:
                self._save()
        if removed:
            _log.info("PasteAppMemory: удалено %d устаревших записей (cutoff=%s)", removed, cutoff.date())
        return removed

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            loaded: dict[str, Any] = json.loads(raw)
            # Поддержка как плоского {bundle_id: profile_str}, так и нового формата
            for bid, val in loaded.items():
                if isinstance(val, str):
                    self._data[bid] = {"profile": val, "last_used": _utcnow_iso()}
                elif isinstance(val, dict) and "profile" in val:
                    self._data[bid] = {
                        "profile": val["profile"],
                        "last_used": val.get("last_used", _utcnow_iso()),
                    }
        except Exception as exc:
            _log.warning("PasteAppMemory: ошибка загрузки %s: %s", self._path, exc)

    def _save(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as exc:
            _log.error("PasteAppMemory: ошибка сохранения: %s", exc)

    @staticmethod
    def _parse_dt(iso: str) -> datetime:
        try:
            return datetime.fromisoformat(iso)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    # ------------------------------------------------------------------
    # IPC-обёртки (handle_*)
    # ------------------------------------------------------------------

    def handle_get_paste_profile_for_app(self, params: dict) -> dict:
        """IPC: get_paste_profile_for_app {bundle_id}."""
        bundle_id: str = params.get("bundle_id", "")
        profile = self.get_profile_for(bundle_id)
        return {"bundle_id": bundle_id, "profile": profile}

    def handle_record_paste_app_profile(self, params: dict) -> dict:
        """IPC: record_paste_app_profile {bundle_id, profile}."""
        bundle_id: str = params.get("bundle_id", "")
        profile: str = params.get("profile", "")
        if not bundle_id:
            return {"ok": False, "error": "bundle_id обязателен"}
        if not profile:
            return {"ok": False, "error": "profile обязателен"}
        try:
            self.record(bundle_id, profile)
            return {"ok": True, "bundle_id": bundle_id, "profile": profile}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    def handle_list_app_profiles(self, params: dict) -> dict:  # noqa: ARG002
        """IPC: list_app_profiles → список всех сохранённых ассоциаций."""
        return {"profiles": self.list_profiles()}

    def handle_delete_app_profile(self, params: dict) -> dict:
        """IPC: delete_app_profile {bundle_id}."""
        bundle_id: str = params.get("bundle_id", "")
        removed = self.delete(bundle_id)
        return {"ok": removed, "bundle_id": bundle_id}

    def handle_cleanup_stale_app_profiles(self, params: dict) -> dict:  # noqa: ARG002
        """IPC: cleanup_stale_app_profiles → удаляет устаревшие записи."""
        removed = self.cleanup_stale()
        return {"removed": removed}
