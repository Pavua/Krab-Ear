"""Хранилище звонковых сессий (Phase 3 Call Automation).

Использует append-only NDJSON с tombstone-удалением, аналогично StateStore.
Файл: <data_dir>/call_sessions.ndjson

Wave 1772 — компактирование (исправление unbounded-growth + O(N²)):
  Каждый вызов add_transcript/update_status/mark_completed/mark_failed
  дописывал одну дельта-строку, но replay при get() перечитывал ВСЕ строки
  файла. Для K-реплик одного звонка это O(K²) парсов JSON. Исправление:
  - compact() переписывает файл атомарно (tmp + replace под flock), сохраняя
    только последнее свёрнутое состояние каждой активной сессии (1 строка/сессия).
  - maybe_compact() проверяет _COMPACT_LINE_THRESHOLD и запускает compact().
  - mark_completed/mark_failed вызывают maybe_compact() после финального append.
  - _apply_delta: cost_usd теперь присваивается (=), а не суммируется (+=).
    В дельте mark_completed/mark_failed всегда пишется итоговая стоимость звонка.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.parsing_utils import safe_json_loads
from backend.call_session import (
    CallSession,
    CallSessionStateMachine,
    CallStatus,
    TranscriptEntry,
    _now_iso,
)

logger = logging.getLogger("KrabEar.Backend.CallSessionStore")

# Порог числа строк в call_sessions.ndjson, при превышении которого
# maybe_compact() запускает компактирование.
# Одна активная сессия с ~200 репликами порождает ~202 строк; порог 500 даёт
# ~2 параллельных звонка с большим транскриптом до первого компактирования.
_COMPACT_LINE_THRESHOLD = 500


class CallSessionStore:
    """Персистентное хранилище CallSession в append-only NDJSON.

    Паттерн tombstone: удаление пишет ``{"id": "...", "_deleted": true}``.
    Обновления статуса пишутся как отдельные дельта-записи с ``_update: true``.
    При get/list последнее состояние собирается через replay всех дельт.

    Компактирование (Wave 1772):
      compact() перезаписывает файл атомарно: только по одной «закрытой» строке
      на каждую активную сессию.  Вызывается автоматически после завершения
      звонка (mark_completed/mark_failed) при превышении порога числа строк,
      а также явно через IPC-метод call_session_compact.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.sessions_path = data_dir / "call_sessions.ndjson"
        self.lock_path = data_dir / "call_sessions.lock"

        data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_path.touch(exist_ok=True)

    # ------------------------------------------------------------------
    # Lock
    # ------------------------------------------------------------------

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, phone_number: str, goal_text: str) -> CallSession:
        """Создаёт новую сессию в состоянии IDLE и сохраняет её."""
        session = CallSession.create(phone_number=phone_number, goal_text=goal_text)
        with self._lock():
            self._append(session.to_dict())
        return session

    def get(self, session_id: str) -> CallSession | None:
        """Возвращает актуальное состояние сессии или None."""
        sid = session_id.strip()
        with self._lock():
            return self._replay_session_unlocked(sid)

    def list_sessions(
        self,
        limit: int = 50,
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Возвращает список сессий (newest first), опционально фильтруя по статусу."""
        safe_limit = max(1, min(limit, 500))
        with self._lock():
            sessions = self._load_all_unlocked()

        if status_filter:
            sessions = [s for s in sessions if s.status == status_filter]

        # newest first
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return [s.to_dict() for s in sessions[:safe_limit]]

    def update_status(self, session_id: str, new_status: str) -> CallSession:
        """Применяет переход состояния и сохраняет дельту.

        Raises:
            KeyError: сессия не найдена.
            ValueError: недопустимый переход или неизвестный статус.
        """
        sid = session_id.strip()
        try:
            target = CallStatus(new_status)
        except ValueError:
            raise ValueError(f"Неизвестный статус: {new_status!r}")

        with self._lock():
            session = self._replay_session_unlocked(sid)
            if session is None:
                raise KeyError(f"Сессия не найдена: {sid!r}")

            sm = CallSessionStateMachine(CallStatus(session.status))
            sm.transition(target)
            session.status = target.value

            # Проставляем временные метки при переходах
            now = _now_iso()
            if target == CallStatus.DIALING and session.started_at is None:
                session.started_at = now

            delta: dict[str, Any] = {
                "id": sid,
                "_update": True,
                "status": target.value,
            }
            if session.started_at:
                delta["started_at"] = session.started_at
            self._append(delta)

        return session

    def add_transcript(
        self,
        session_id: str,
        speaker: str,
        text: str,
        ts: str | None = None,
    ) -> CallSession:
        """Добавляет реплику в транскрипт сессии.

        Raises:
            KeyError: сессия не найдена.
        """
        sid = session_id.strip()
        entry = TranscriptEntry(
            speaker=speaker,
            text=text,
            ts=ts or _now_iso(),
        )
        with self._lock():
            session = self._replay_session_unlocked(sid)
            if session is None:
                raise KeyError(f"Сессия не найдена: {sid!r}")
            session.transcript_history.append(entry)
            self._append({
                "id": sid,
                "_update": True,
                "_transcript_entry": entry.to_dict(),
            })
        return session

    def mark_completed(
        self,
        session_id: str,
        end_reason: str,
        cost_usd: float = 0.0,
    ) -> CallSession:
        """Переводит сессию в COMPLETED, вычисляет длительность и стоимость.

        После успешного завершения проверяет порог строк и запускает
        компактирование при необходимости (Wave 1772).

        Raises:
            KeyError: сессия не найдена.
            ValueError: переход недопустим.
        """
        sid = session_id.strip()
        with self._lock():
            session = self._replay_session_unlocked(sid)
            if session is None:
                raise KeyError(f"Сессия не найдена: {sid!r}")

            sm = CallSessionStateMachine(CallStatus(session.status))
            sm.transition(CallStatus.COMPLETED)
            session.status = CallStatus.COMPLETED.value
            session.end_reason = end_reason
            session.ended_at = _now_iso()
            session.cost_usd = float(cost_usd)
            session.duration_sec = self._compute_duration(
                session.started_at, session.ended_at
            )

            self._append({
                "id": sid,
                "_update": True,
                "status": CallStatus.COMPLETED.value,
                "ended_at": session.ended_at,
                "end_reason": end_reason,
                "cost_usd": session.cost_usd,
                "duration_sec": session.duration_sec,
            })
            # Компактирование под тем же flock: файл уже открыт монопольно.
            self._maybe_compact_unlocked()

        return session

    def mark_failed(
        self,
        session_id: str,
        end_reason: str,
        cost_usd: float = 0.0,
    ) -> CallSession:
        """Переводит сессию в FAILED.

        После записи дельты проверяет порог строк и запускает компактирование
        при необходимости (Wave 1772).

        Raises:
            KeyError: сессия не найдена.
            ValueError: переход недопустим.
        """
        sid = session_id.strip()
        with self._lock():
            session = self._replay_session_unlocked(sid)
            if session is None:
                raise KeyError(f"Сессия не найдена: {sid!r}")

            sm = CallSessionStateMachine(CallStatus(session.status))
            sm.transition(CallStatus.FAILED)
            session.status = CallStatus.FAILED.value
            session.end_reason = end_reason
            session.ended_at = _now_iso()
            session.cost_usd = float(cost_usd)
            session.duration_sec = self._compute_duration(
                session.started_at, session.ended_at
            )

            self._append({
                "id": sid,
                "_update": True,
                "status": CallStatus.FAILED.value,
                "ended_at": session.ended_at,
                "end_reason": end_reason,
                "cost_usd": session.cost_usd,
                "duration_sec": session.duration_sec,
            })
            # Компактирование под тем же flock: файл уже открыт монопольно.
            self._maybe_compact_unlocked()

        return session

    def delete_all(self) -> int:
        """Полностью очищает хранилище сессий (privacy-purge / wipe-all).

        Перезаписывает call_sessions.ndjson пустым файлом под flock.
        Звонки содержат номера телефонов и временны́е метки — обязательно
        удалять при full privacy-purge.

        Используется ТОЛЬКО из handle_purge_all_data.

        Returns:
            Количество активных сессий до очистки.
        """
        with self._lock():
            sessions_before = len(self._load_all_unlocked())
            tmp = self.sessions_path.with_suffix(".ndjson.tmp")
            try:
                tmp.write_text("", encoding="utf-8")
                tmp.replace(self.sessions_path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
        return sessions_before

    def delete(self, session_id: str) -> bool:
        """Мягкое удаление сессии через tombstone. Возвращает True если сессия была."""
        sid = session_id.strip()
        if not sid:
            return False
        with self._lock():
            session = self._replay_session_unlocked(sid)
            if session is None:
                return False
            self._append({"id": sid, "_deleted": True})
        return True

    # ------------------------------------------------------------------
    # Compaction (Wave 1772)
    # ------------------------------------------------------------------

    def compact(self) -> dict[str, int]:
        """Компактирует call_sessions.ndjson атомарно под flock.

        Сворачивает все дельты: оставляет по одной строке (полное состояние)
        на каждую активную (не удалённую) сессию.  Tombstone'ы и промежуточные
        дельта-записи удаляются.

        Returns:
            {"lines_before": int, "lines_after": int, "sessions_kept": int}
        """
        with self._lock():
            return self._compact_unlocked()

    def maybe_compact(self) -> bool:
        """Запускает compact() если число строк превышает порог.

        Returns:
            True если компактирование было выполнено.
        """
        with self._lock():
            return self._maybe_compact_unlocked()

    def _maybe_compact_unlocked(self) -> bool:
        """Проверяет порог и запускает _compact_unlocked() при необходимости.

        Вызывается внутри уже захваченного flock — повторный захват не нужен.
        """
        lines = self._count_lines_unlocked()
        if lines <= _COMPACT_LINE_THRESHOLD:
            return False
        self._compact_unlocked()
        return True

    def _compact_unlocked(self) -> dict[str, int]:
        """Компактирование под уже захваченным flock.

        Алгоритм:
          1. Считаем текущее число строк (для статистики).
          2. Загружаем все активные сессии (_load_all_unlocked): каждая уже
             содержит применённые дельты — это «закрытое» состояние.
          3. Пишем по одной полной строке на сессию в tmp-файл.
          4. Атомарно заменяем оригинальный файл через os.replace (POSIX-атомарно).

        Безопасность:
          - tmp-файл создаётся рядом с оригиналом (тот же каталог → тот же том).
          - При исключении tmp убирается, оригинал не тронут.
          - fsync перед replace гарантирует запись на диск.
        """
        lines_before = self._count_lines_unlocked()
        active = self._load_all_unlocked()

        tmp = self.sessions_path.with_suffix(".ndjson.tmp")
        replaced = False
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for session in active:
                    fh.write(json.dumps(session.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self.sessions_path)
            replaced = True
        finally:
            if not replaced and tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        lines_after = len(active)
        logger.info(
            "call_session_store compact",
            extra={
                "lines_before": lines_before,
                "lines_after": lines_after,
                "sessions_kept": len(active),
            },
        )
        return {
            "lines_before": lines_before,
            "lines_after": lines_after,
            "sessions_kept": len(active),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _replay_session_unlocked(self, session_id: str) -> CallSession | None:
        """Восстанавливает состояние одной сессии из NDJSON-дельт."""
        base: CallSession | None = None
        deleted = False

        for record in self._iter_records_unlocked():
            if record.get("id") != session_id:
                continue
            if record.get("_deleted"):
                deleted = True
                base = None
                continue
            if record.get("_update"):
                if base is None:
                    continue
                self._apply_delta(base, record)
                continue
            # Full record (base creation)
            try:
                base = CallSession.from_dict(record)
            except Exception:
                pass

        if deleted:
            return None
        return base

    def _load_all_unlocked(self) -> list[CallSession]:
        """Загружает все активные сессии с применением дельт."""
        sessions: dict[str, CallSession] = {}
        deleted: set[str] = set()

        for record in self._iter_records_unlocked():
            sid = str(record.get("id", "")).strip()
            if not sid:
                continue
            if record.get("_deleted"):
                deleted.add(sid)
                sessions.pop(sid, None)
                continue
            if record.get("_update"):
                if sid in sessions:
                    self._apply_delta(sessions[sid], record)
                continue
            try:
                sessions[sid] = CallSession.from_dict(record)
            except Exception:
                pass

        for sid in deleted:
            sessions.pop(sid, None)

        return list(sessions.values())

    @staticmethod
    def _apply_delta(session: CallSession, delta: dict[str, Any]) -> None:
        """Применяет дельта-запись к объекту сессии in-place.

        Wave 1772: cost_usd теперь присваивается (=), а не суммируется (+=).
        В дельтах mark_completed/mark_failed пишется итоговая стоимость звонка —
        накапливать её повторно при replay означало бы удвоение за каждую дельту.
        """
        if "status" in delta:
            session.status = str(delta["status"])
        if "started_at" in delta:
            session.started_at = delta["started_at"]
        if "ended_at" in delta:
            session.ended_at = delta["ended_at"]
        if "end_reason" in delta:
            session.end_reason = delta["end_reason"]
        if "cost_usd" in delta:
            try:
                # Итоговая стоимость — присваивание, не накопление.
                session.cost_usd = float(delta["cost_usd"])
            except (TypeError, ValueError):
                pass
        if "duration_sec" in delta:
            session.duration_sec = delta["duration_sec"]
        entry_data = delta.get("_transcript_entry")
        if isinstance(entry_data, dict):
            session.transcript_history.append(TranscriptEntry.from_dict(entry_data))

    def _iter_records_unlocked(self) -> Iterator[dict[str, Any]]:
        """Итерация по NDJSON-строкам файла."""
        if not self.sessions_path.exists():
            return
        with self.sessions_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                payload = safe_json_loads(raw)
                if isinstance(payload, dict):
                    yield payload

    def _count_lines_unlocked(self) -> int:
        """Подсчитывает непустые строки файла без парсинга JSON."""
        if not self.sessions_path.exists():
            return 0
        count = 0
        with self.sessions_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
        return count

    def _append(self, payload: dict[str, Any]) -> None:
        """Атомарный append JSON-строки с fsync."""
        with self.sessions_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _compute_duration(started_at: str | None, ended_at: str | None) -> float | None:
        """Вычисляет длительность в секундах или None."""
        if not started_at or not ended_at:
            return None
        try:
            fmt = "%Y-%m-%dT%H:%M:%S"
            start = datetime.strptime(started_at, fmt).replace(tzinfo=timezone.utc)
            end = datetime.strptime(ended_at, fmt).replace(tzinfo=timezone.utc)
            secs = (end - start).total_seconds()
            return max(0.0, secs)
        except ValueError:
            return None
