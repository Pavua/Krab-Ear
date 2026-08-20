"""Реактивное освобождение памяти при `mlx.oom`.

ROOT CAUSE
----------
Выгрузка brain-модели LM Studio была ТОЛЬКО проактивной — на старте записи
(`llm_brain_unload_on_recording`). На факт реальной нехватки памяти система не
реагировала никак: рождался код `mlx.oom`, владельцу показывался тост «выгрузи
LM Studio» — и всё, дальше он должен был делать это руками. Механизм выгрузки
(`lm_studio_lifecycle.unload_model_async`) при этом существовал, работал и просто
никогда не звался из обработки ошибки.

🔴 ОГРАНИЧЕНИЕ СЛОЯ, определяющее весь дизайн этого модуля.
`EventBus.add_listener` вызывает колбэк СИНХРОННО внутри `emit()`, в потоке
эмиттера — то есть в STT-пайплайне. Документация шины прямо требует неблокирующий
колбэк. Здесь блокирующей работы хватает: `current_lease_holder()` читает файл под
`flock`, `unload_model_async` ходит по HTTP и при неудаче падает в CLI-фоллбэк.
Выполнить это в потоке эмиттера значило бы заморозить распознавание — тот самый
freeze-класс. Поэтому `handle_event` обязан вернуть управление немедленно, а всю
работу делать в отдельном daemon-потоке.

🔴 ГЕЙТ НА ЧУЖУЮ РАБОТУ. `brain_lease` — advisory-координация одного Metal GPU
между Krab Ear и Главным Крабом. Если лизу держит другой владелец, выгрузка
оборвала бы его inference. Направление отказа fail-safe: не уверены — не выгружаем.

🔴 COOLDOWN. OOM приходит штормом (`dedupe_seconds=5` у самого кода ошибки), а
выгрузка — дорогая и разрушительная операция. Без окна пять подряд ошибок дали бы
пять выгрузок подряд. Одна попытка на окно.
"""

from __future__ import annotations

import logging
import threading
import time

from backend.brain_lease import current_lease_holder
from backend.lm_studio_lifecycle import unload_model_async

logger = logging.getLogger("KrabEar.Backend.OomAutoRelief")

OOM_CODE = "mlx.oom"
_DEFAULT_COOLDOWN_SEC = 600.0
# Владелец brain-лизы, которым представляется этот процесс (см. recording_core_service).
_OWN_LEASE_OWNER = "krab_ear"


class OomAutoRelief:
    """Слушатель шины ошибок: на `mlx.oom` пытается освободить память.

    Живёт рядом с обработчиком кнопки того же тоста (`error_actions`) — руками и
    автоматически делается ОДНО И ТО ЖЕ, с одними и теми же гейтами.
    """

    def __init__(self, settings_service, cooldown_sec: float | None = None) -> None:
        self._settings_service = settings_service
        self._cooldown_override = cooldown_sec
        self._lock = threading.Lock()
        self._last_attempt_ts: float | None = None
        self._workers: list[threading.Thread] = []

    # -- слой шины ---------------------------------------------------------

    def handle_event(self, event_type: str, payload: dict) -> None:
        """Колбэк для `EventBus.add_listener`. ОБЯЗАН возвращаться немедленно.

        Никогда не бросает: исключение отсюда всплыло бы в поток STT-пайплайна.
        """
        try:
            if event_type != "krab_error":
                return
            if (payload or {}).get("code") != OOM_CODE:
                return
            if not self._cooldown_allows():
                return
            worker = threading.Thread(
                target=self._relieve, name="oom-auto-relief", daemon=True,
            )
            with self._lock:
                self._workers.append(worker)
            worker.start()
        except Exception:  # noqa: BLE001 — колбэк шины не смеет ронять эмиттер
            logger.exception("OomAutoRelief: сбой в обработчике события")

    def wait_for_idle(self, timeout: float = 10.0) -> None:
        """Дождаться завершения запущенной работы. Только для тестов и shutdown."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                pending = [w for w in self._workers if w.is_alive()]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            pending[0].join(timeout=min(remaining, 0.5))

    # -- решения -----------------------------------------------------------

    def _settings(self) -> dict:
        svc = self._settings_service
        if svc is None:
            return {}
        try:
            return svc.cached_settings() or {}
        except Exception:  # noqa: BLE001 — недоступные настройки не повод падать
            logger.exception("OomAutoRelief: не удалось прочитать настройки")
            return {}

    def _cooldown_allows(self) -> bool:
        """Одна попытка на окно. Проверка дешёвая — можно делать в потоке эмиттера."""
        settings = self._settings()
        if not settings.get("mlx_oom_auto_unload_enabled", True):
            return False
        if self._cooldown_override is not None:
            window = float(self._cooldown_override)
        else:
            try:
                window = float(
                    settings.get("mlx_oom_auto_unload_cooldown_sec", _DEFAULT_COOLDOWN_SEC)
                )
            except (TypeError, ValueError):
                window = _DEFAULT_COOLDOWN_SEC
        now = time.monotonic()
        with self._lock:
            last = self._last_attempt_ts
            if last is not None and (now - last) < window:
                return False
            self._last_attempt_ts = now
        return True

    # -- работа ------------------------------------------------------------

    def _relieve(self) -> None:
        """Тело выгрузки. Живёт в своём потоке — здесь блокироваться можно."""
        try:
            holder = current_lease_holder()
            if holder and holder.get("owner") != _OWN_LEASE_OWNER:
                logger.info(
                    "OomAutoRelief: выгрузка пропущена — brain-лизу держит другой владелец",
                    extra={"lease_owner": holder.get("owner")},
                )
                return
            settings = self._settings()
            model_id = settings.get("llm_brain_model") or ""
            base_url = settings.get("llm_base_url") or ""
            if not model_id:
                logger.info("OomAutoRelief: llm_brain_model не задан — выгружать нечего")
                return
            logger.warning(
                "OomAutoRelief: mlx.oom — запрошена выгрузка brain-модели",
                extra={"model_id": model_id},
            )
            unload_model_async(base_url, model_id)
        except Exception:  # noqa: BLE001 — фоновый поток не должен умирать молча
            logger.exception("OomAutoRelief: сбой при освобождении памяти")
