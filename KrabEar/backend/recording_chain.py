"""RecordingChainManager — связывание записей в цепочки (multi-part meetings).

Цепочки позволяют объединить несколько записей в одну логическую последовательность:
- многочасовые совещания с перерывами
- продолжение разговора из предыдущей сессии
- серия интервью по одной теме

Данные сохраняются в {data_dir}/recording_chains.json.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.RecordingChain")

_CHAINS_FILE = "recording_chains.json"

# DoS caps (A3 wave-34)
MAX_CHAINS = 500
MAX_ITEMS_PER_CHAIN = 1000
MAX_CHAIN_NAME_LEN = 200

# Basic UUID-like item_id format guard (A4 wave-34):
# must be non-empty, no path separators, no null bytes.
_ITEM_ID_UNSAFE_RE = re.compile(r'[/\\.\x00]')


def _is_valid_item_id(item_id: str) -> bool:
    """Returns True when item_id passes minimal format validation."""
    return bool(item_id) and not _ITEM_ID_UNSAFE_RE.search(item_id)


class RecordingChainManager:
    """Управление цепочками записей.

    Структура recording_chains.json:
    {
        "chains": {
            "<chain_id>": {
                "chain_id": str,
                "name": str,
                "created_at": ISO8601,
                "ended_at": ISO8601 | null,
                "item_ids": [str, ...]
            }
        }
    }
    """

    def __init__(
        self,
        store: Any,
        settings_fn: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self._store = store
        self._settings_fn = settings_fn
        self._data_dir = Path(getattr(store, "data_dir", "."))
        self._chains_path = self._data_dir / _CHAINS_FILE
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"chains": {}}
        self._load()

    def _is_privacy_mode(self) -> bool:
        """Returns True when privacy_mode_enabled is set in cached settings."""
        if self._settings_fn is None:
            return False
        try:
            return bool(self._settings_fn().get("privacy_mode_enabled", False))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._chains_path.exists():
            try:
                with open(self._chains_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and "chains" in loaded:
                    self._data = loaded
            except Exception:
                logger.warning("Не удалось загрузить %s, начинаем с чистого листа", self._chains_path)

    def _save(self) -> None:
        """Атомарно записывает цепочки на диск.

        ВАЖНО (RC-4 W1769): при ошибке записи исключение ПРОПАГИРУЕТСЯ наружу,
        а не глотается.  Раньше bare-`except` логировал и возвращался нормально,
        из-за чего:
          (a) мутирующие операции рапортовали успех, хотя на диск ничего не легло
              (ложный успех, теряется при перезапуске backend);
          (b) privacy-purge (delete_all_chains) сообщал об успешной очистке, в то
              время как файл recording_chains.json с открытыми именами цепочек
              (потенциально PII) оставался на диске.
        Теперь вызывающие (handle_* / delete_all_chains) могут отреагировать на
        сбой.  Путь успешной атомарной записи не изменён.
        """
        try:
            self._chains_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._chains_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self._chains_path)
        except Exception:
            logger.exception("Не удалось сохранить %s", self._chains_path)
            raise

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def start_chain(self, name: str) -> str:
        """Создаёт новую цепочку и возвращает chain_id."""
        name = name.strip()
        if not name:
            raise ValueError("Имя цепочки не может быть пустым")
        if len(name) > MAX_CHAIN_NAME_LEN:
            raise ValueError(
                f"Имя цепочки слишком длинное (максимум {MAX_CHAIN_NAME_LEN} символов)"
            )
        chain_id = str(uuid.uuid4())
        with self._lock:
            if len(self._data["chains"]) >= MAX_CHAINS:
                raise RuntimeError(
                    f"Достигнут лимит цепочек ({MAX_CHAINS})"
                )
            self._data["chains"][chain_id] = {
                "chain_id": chain_id,
                "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "ended_at": None,
                "item_ids": [],
            }
            self._save()
        return chain_id

    def add_to_chain(self, chain_id: str, item_id: str) -> None:
        """Добавляет запись истории в цепочку."""
        with self._lock:
            chain = self._data["chains"].get(chain_id)
            if chain is None:
                raise KeyError(f"Цепочка не найдена: {chain_id}")
            if chain.get("ended_at") is not None:
                raise RuntimeError(f"Цепочка уже завершена: {chain_id}")
            item_id = str(item_id).strip()
            if not _is_valid_item_id(item_id):
                raise ValueError(
                    f"item_id содержит недопустимые символы или пустой: {item_id!r}"
                )
            if len(chain["item_ids"]) >= MAX_ITEMS_PER_CHAIN:
                raise RuntimeError(
                    f"Цепочка достигла лимита элементов ({MAX_ITEMS_PER_CHAIN})"
                )
            if item_id not in chain["item_ids"]:
                chain["item_ids"].append(item_id)
                self._save()

    def unlink_recording_from_chain(self, chain_id: str, item_id: str) -> bool:
        """Удаляет запись из цепочки.

        Returns:
            True  — элемент был найден и удалён.
            False — элемент отсутствовал в цепочке (идемпотентно).

        Raises:
            KeyError: если цепочка не существует.
        """
        with self._lock:
            chain = self._data["chains"].get(chain_id)
            if chain is None:
                raise KeyError(f"Цепочка не найдена: {chain_id}")
            item_id = str(item_id).strip()
            if item_id in chain["item_ids"]:
                chain["item_ids"].remove(item_id)
                self._save()
                return True
            return False

    def end_chain(self, chain_id: str) -> None:
        """Завершает цепочку (помечает ended_at)."""
        with self._lock:
            chain = self._data["chains"].get(chain_id)
            if chain is None:
                raise KeyError(f"Цепочка не найдена: {chain_id}")
            if chain.get("ended_at") is None:
                chain["ended_at"] = datetime.now(timezone.utc).isoformat()
                self._save()

    def get_chain(self, chain_id: str) -> dict[str, Any]:
        """Возвращает цепочку: элементы по порядку, суммарные duration и word_count.

        Privacy gate (A1 wave-34): когда privacy_mode_enabled=True, возвращает
        минимальный конверт без transcript cleartext (items пустой список).
        """
        if self._is_privacy_mode():
            # Return structural metadata without transcript content.
            with self._lock:
                chain = self._data["chains"].get(chain_id)
                if chain is None:
                    raise KeyError(f"Цепочка не найдена: {chain_id}")
                return {
                    "chain_id": chain_id,
                    "name": chain["name"],
                    "created_at": chain["created_at"],
                    "ended_at": chain.get("ended_at"),
                    "item_ids": list(chain["item_ids"]),
                    "items": [],
                    "total_duration_sec": 0.0,
                    "total_word_count": 0,
                    "privacy_mode": True,
                }
        with self._lock:
            chain = self._data["chains"].get(chain_id)
            if chain is None:
                raise KeyError(f"Цепочка не найдена: {chain_id}")
            # Snapshot all chain fields while holding the lock to prevent
            # TOCTOU divergence (RC-1 W883): another thread could mutate or
            # delete the chain between releasing the lock and reading metadata.
            item_ids = list(chain["item_ids"])
            chain_name = chain["name"]
            chain_created_at = chain["created_at"]
            chain_ended_at = chain.get("ended_at")

        total_duration = 0.0
        total_words = 0
        items_detail: list[dict[str, Any]] = []
        for iid in item_ids:
            item = None
            try:
                if hasattr(self._store, "get_history_item_by_id"):
                    item = self._store.get_history_item_by_id(iid)
            except Exception:
                pass
            if item is not None:
                d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
                total_duration += float(d.get("duration_sec", 0) or 0)
                text = str(d.get("text", "") or "")
                total_words += len(text.split()) if text else 0
                items_detail.append(d)
            else:
                items_detail.append({"id": iid})

        return {
            "chain_id": chain_id,
            "name": chain_name,
            "created_at": chain_created_at,
            "ended_at": chain_ended_at,
            "item_ids": item_ids,
            "items": items_detail,
            "total_duration_sec": round(total_duration, 2),
            "total_word_count": total_words,
        }

    def list_chains(self, limit: int = 20) -> list[dict[str, Any]]:
        """Возвращает список цепочек (краткая форма), отсортированных по дате создания убыванием."""
        # F1 guard: отрицательные значения и превышение разумного лимита недопустимы
        limit = max(0, min(limit, 1000))
        # BUG3 fix (RC-2 W1726): build result snapshots INSIDE the lock so
        # concurrent add_to_chain / end_chain cannot mutate the shared dict
        # objects between list() and the subsequent field reads → torn
        # item_count / ended_at.
        with self._lock:
            summaries = [
                {
                    "chain_id": c["chain_id"],
                    "name": c["name"],
                    "created_at": c["created_at"],
                    "ended_at": c.get("ended_at"),
                    "item_count": len(c.get("item_ids", [])),
                }
                for c in self._data["chains"].values()
            ]

        summaries.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        return summaries[:limit]

    def delete_all_chains(self) -> int:
        """Удаляет все цепочки (используется при полной очистке данных / privacy-purge).

        ВАЖНО (RC-4 W1769): privacy-purge.  Если _save() не смог переписать файл
        на диск (disk full / read-only / permission), исключение ПРОПАГИРУЕТСЯ
        наружу — НЕ глотается.  Иначе recording_chains.json с открытыми именами
        цепочек (потенциально PII) пережил бы «успешную» очистку.  Вызывающий
        (HistoryService.handle_purge_all_data) ловит исключение и добавляет шаг
        "chains" в secondary_errors, так что пользователь видит частичный сбой
        вместо ложного «очищено».  Лог об успехе пишется ТОЛЬКО после того, как
        _save() реально завершился.

        Returns:
            int: количество удалённых цепочек.
        """
        with self._lock:
            count = len(self._data["chains"])
            self._data = {"chains": {}}
            self._save()  # пробрасывает OSError → privacy-purge surfaces failure
        logger.info("delete_all_chains: удалено %d цепочек", count)
        return count

    def merge_chain_text(self, chain_id: str) -> str:
        """Конкатенирует тексты всех записей цепочки в порядке добавления.

        Privacy gate (A1 wave-34): когда privacy_mode_enabled=True, возвращает
        пустую строку вместо transcript cleartext.
        """
        if self._is_privacy_mode():
            return ""
        chain_data = self.get_chain(chain_id)
        parts: list[str] = []
        for item in chain_data["items"]:
            text = str(item.get("text", "") or "").strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_start_chain(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("Параметр 'name' обязателен")
        # RC-4 W1769: persistence failure → error envelope вместо ложного успеха.
        # Имя цепочки (PII) НЕ логируем — только сам факт сбоя записи.
        try:
            chain_id = self.start_chain(name)
        except RuntimeError as exc:
            # DoS cap: limit_exceeded (A3 wave-34)
            return {"ok": False, "error": str(exc), "reason": "limit_exceeded"}
        except OSError as exc:
            logger.error("start_chain: не удалось сохранить цепочку: %s", exc)
            return {"ok": False, "error": f"persist_failed: {exc}"}
        return {"chain_id": chain_id}

    def handle_add_to_chain(self, params: dict[str, Any]) -> dict[str, Any]:
        chain_id = str(params.get("chain_id", "")).strip()
        item_id = str(params.get("item_id", "")).strip()
        if not chain_id:
            raise ValueError("Параметр 'chain_id' обязателен")
        if not item_id:
            raise ValueError("Параметр 'item_id' обязателен")
        # RC-4 W1769: persistence failure → error envelope вместо ложного успеха.
        try:
            self.add_to_chain(chain_id, item_id)
        except RuntimeError as exc:
            # DoS cap: limit_exceeded (A3 wave-34) or chain already ended
            return {"ok": False, "error": str(exc), "reason": "limit_exceeded"}
        except OSError as exc:
            logger.error("add_to_chain: не удалось сохранить цепочку: %s", exc)
            return {"ok": False, "error": f"persist_failed: {exc}"}
        return {"ok": True}

    def handle_end_chain(self, params: dict[str, Any]) -> dict[str, Any]:
        chain_id = str(params.get("chain_id", "")).strip()
        if not chain_id:
            raise ValueError("Параметр 'chain_id' обязателен")
        # RC-4 W1769: persistence failure → error envelope вместо ложного успеха.
        try:
            self.end_chain(chain_id)
        except OSError as exc:
            logger.error("end_chain: не удалось сохранить цепочку: %s", exc)
            return {"ok": False, "error": f"persist_failed: {exc}"}
        return {"ok": True}

    def handle_get_chain(self, params: dict[str, Any]) -> dict[str, Any]:
        chain_id = str(params.get("chain_id", "")).strip()
        if not chain_id:
            raise ValueError("Параметр 'chain_id' обязателен")
        return self.get_chain(chain_id)

    def handle_list_chains(self, params: dict[str, Any]) -> dict[str, Any]:
        # BUG4 fix (W1726): int() raises ValueError on non-numeric input
        # (e.g. {"limit": "all"}) → 500-level IPC error.  Gracefully fall
        # back to the default limit instead.
        try:
            limit = int(params.get("limit", 20))
        except (ValueError, TypeError):
            limit = 20
        return {"chains": self.list_chains(limit=limit)}

    def handle_merge_chain_text(self, params: dict[str, Any]) -> dict[str, Any]:
        chain_id = str(params.get("chain_id", "")).strip()
        if not chain_id:
            raise ValueError("Параметр 'chain_id' обязателен")
        text = self.merge_chain_text(chain_id)
        return {"text": text}

    def handle_unlink_recording_from_chain(self, params: dict[str, Any]) -> dict[str, Any]:
        chain_id = str(params.get("chain_id", "")).strip()
        item_id = str(params.get("item_id", "")).strip()
        if not chain_id:
            raise ValueError("Параметр 'chain_id' обязателен")
        if not item_id:
            raise ValueError("Параметр 'item_id' обязателен")
        # RC-4 W1769: persistence failure → error envelope вместо ложного успеха.
        try:
            removed = self.unlink_recording_from_chain(chain_id, item_id)
        except OSError as exc:
            logger.error(
                "unlink_recording_from_chain: не удалось сохранить цепочку: %s", exc
            )
            return {"ok": False, "error": f"persist_failed: {exc}"}
        return {"ok": True, "removed": removed}

    # ------------------------------------------------------------------
    # Merge helpers
    # ------------------------------------------------------------------

    def find_chains_containing(self, item_ids: list[str]) -> dict[str, list[str]]:
        """Возвращает словарь {chain_id: [matched_item_ids]} для всех цепочек,
        содержащих хотя бы один из переданных item_ids.

        Используется RecordingMerger для обнаружения «ghost refs» перед удалением
        оригиналов и последующей заменой их на merged item_id.
        """
        item_id_set = set(item_ids)
        result: dict[str, list[str]] = {}
        with self._lock:
            for chain_id, chain in self._data["chains"].items():
                matched = [iid for iid in chain.get("item_ids", []) if iid in item_id_set]
                if matched:
                    result[chain_id] = matched
        return result

    def replace_items_in_chain(self, chain_id: str, old_ids: list[str], new_id: str) -> bool:
        """Заменяет все вхождения old_ids в цепочке на new_id (однократно, в позиции первого).

        Возвращает True, если были произведены изменения.
        Не бросает исключение, если цепочка не найдена — идемпотентно.

        BUG1 fix (W1726): when new_id is ALREADY present in item_ids before
        any old_id is encountered, the previous implementation still inserted
        new_id again at the first old_id position → duplicate entry.
        Example: ['merged','a','orig1','b'] replacing orig1→merged previously
        yielded ['merged','a','merged','b'].
        Fix: only insert new_id if it is not already in new_list at the point
        of first match, so the final list contains new_id exactly once.
        """
        old_id_set = set(old_ids)
        with self._lock:
            chain = self._data["chains"].get(chain_id)
            if chain is None:
                return False
            item_ids: list[str] = list(chain.get("item_ids", []))
            new_list: list[str] = []
            inserted = False
            changed = False
            for iid in item_ids:
                if iid in old_id_set:
                    changed = True
                    if not inserted and new_id not in new_list:
                        new_list.append(new_id)
                        inserted = True
                    # остальные вхождения old_ids пропускаем (дедупликация)
                else:
                    new_list.append(iid)
            if changed:
                chain["item_ids"] = new_list
                self._save()
        return changed

    # ------------------------------------------------------------------
    # Каскадная очистка призрачных item_id (W1253 RC-3)
    # ------------------------------------------------------------------

    def remove_item_from_all_chains(self, item_id: str) -> int:
        """Удаляет item_id из ВСЕХ цепочек, где он встречается.

        Предотвращает накопление «призрачных» ссылок, когда запись удаляется
        из истории (delete / cleanup / archive), но её item_id остаётся
        в recording_chains.json.  Вызывается каскадно из трёх мест:
          - HistoryService.handle_delete_history_item
          - HistoryService.handle_cleanup_old_history
          - ArchiveManager.archive_items

        Args:
            item_id: ID удалённой/архивируемой записи истории.

        Returns:
            Количество цепочек, из которых был удалён item_id (0 = no-op).
        """
        item_id = str(item_id).strip()
        if not item_id:
            return 0
        removed_from = 0
        with self._lock:
            dirty = False
            for chain in self._data["chains"].values():
                if item_id in chain.get("item_ids", []):
                    chain["item_ids"].remove(item_id)
                    removed_from += 1
                    dirty = True
            if dirty:
                self._save()
        if removed_from:
            logger.debug(
                "remove_item_from_all_chains: item_id=%s удалён из %d цепочек",
                item_id,
                removed_from,
            )
        return removed_from
