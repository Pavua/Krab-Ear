"""MemoryConductor — умеренная лестница выгрузки (спека v2.1, план T5).

ROOT CAUSE: 36 ГБ хронически насыщены (swap 92%, mlx.oom, P0 SEGV), а резиденты
памяти жили без координации: brain 19 ГБ выгружался только на старте записи,
rewriter — никогда, воркеры STT — никогда. Кондуктор применяет детерминированную
лестницу К СВОИМ резидентам IPC-процесса (gigaam, rewriter, brain); whisper-воркер
REST-процесса выселяет его собственный reaper (C-EXECUTOR-LOCALITY).

🔴 C-POLICY-SOURCE: политика читает ТОЛЬКО in-process состояние (инжектированные
колбэки, sysctl). Леджер — write-only витрина: publish_own/remove_own, НИКОГДА
чтение. Закреплено source-тестом: модуль не обращается к чтению леджера.

🔴 Синхронность точек входа: handle_oom_event / on_recording_start вызываются из
чужих потоков (шина событий = поток STT-пайплайна) — возвращаются немедленно,
работа в своих daemon-потоках. Сам тик живёт в собственном потоке и МОЖЕТ
блокироваться на verify (≤ verify_timeout_sec) — это его личное время.

🔴 Shadow: до enforce лестница логирует «would …» и считает счётчики, но НЕ зовёт
исполнителей. reload_brain_allowed() в shadow ВСЕГДА True + счётчик
would_skip_brain_reload (H2/H3 из адверсариального ревью спеки).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.MemoryConductor")

_OWN_LEASE_OWNER = "krab_ear"
_RESIDENTS = ("gigaam", "rewriter", "brain")
_SIZE_MB = {"gigaam": 2000, "rewriter": 5000, "brain": 20000}


def _default_pressure() -> Optional[int]:
    from core.mlx_memory_gate import vm_pressure_level
    return vm_pressure_level()


class MemoryConductor:
    def __init__(
        self,
        settings_service,
        ledger,
        *,
        is_recording: Callable[[], bool],
        is_meeting_active: Callable[[], bool],
        pressure_fn: Callable[[], Optional[int]] = _default_pressure,
        gigaam_close_if_idle: Callable[[float], bool] = None,
        gigaam_idle_sec_fn: Callable[[], float] = None,
        last_stt_activity_ts_fn: Callable[[], float] = None,
        tick_sec: float = 30.0,
        unload_model_fn=None,
        load_model_fn=None,
        model_loaded_fn=None,
        lease_holder_fn=None,
        verify_timeout_sec: float = 10.0,
        verify_poll_sec: float = 0.5,
    ) -> None:
        if unload_model_fn is None or load_model_fn is None or model_loaded_fn is None:
            from backend.lm_studio_lifecycle import (
                load_model_async, model_loaded, unload_model_async,
            )
            unload_model_fn = unload_model_fn or unload_model_async
            load_model_fn = load_model_fn or load_model_async
            model_loaded_fn = model_loaded_fn or model_loaded
        if lease_holder_fn is None:
            from backend.brain_lease import current_lease_holder
            lease_holder_fn = current_lease_holder

        self._settings_service = settings_service
        self._ledger = ledger
        self._is_recording = is_recording
        self._is_meeting_active = is_meeting_active
        self._pressure_fn = pressure_fn
        self.gigaam_close_if_idle = gigaam_close_if_idle
        self._gigaam_idle_sec_fn = gigaam_idle_sec_fn
        self._last_stt_activity_ts_fn = last_stt_activity_ts_fn
        self._tick_sec = float(tick_sec)
        self.unload_model_fn = unload_model_fn
        self.load_model_fn = load_model_fn
        self.model_loaded_fn = model_loaded_fn
        self._lease_holder_fn = lease_holder_fn
        self._verify_timeout = float(verify_timeout_sec)
        self._verify_poll = float(verify_poll_sec)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._pressure_streak = 0
        self._cooldown_until: dict[str, float] = {}
        self._counters = {r: {"attempted": 0, "succeeded": 0, "skipped_gate": 0,
                              "unknown": 0, "failed": 0, "would": 0}
                          for r in _RESIDENTS}
        self._would_skip_brain_reload = 0
        self._decisions: deque[str] = deque(maxlen=20)
        self._last_tick_ts: Optional[float] = None
        self._shadow_since: Optional[float] = None
        # LOW финального гейта: витрина брала "warm"+20000MB БЕЗУСЛОВНО, даже
        # сразу после подтверждённой выгрузки — Swift-строка показывала
        # несуществующие 19 ГБ. Кэш последней ДОСТОВЕРНОЙ проверки/выгрузки
        # (never пингуем LM Studio из _publish — тик 30с, отдельный HTTP на
        # каждый тик того не стоит); обновляется ТОЛЬКО там, где мы реально
        # получили verify-исход (_evict_model / _recording_sequence_worker).
        # Дефолт "unknown" — до первой попытки мы честно не знаем состояние
        # (не выдаём его за "warm", см. §3 C-EFFECT-CHECK три исхода).
        self._brain_state: str = "unknown"

    # -- настройки -----------------------------------------------------------

    def _settings(self) -> dict:
        try:
            return self._settings_service.cached_settings() or {}
        except Exception:
            return {}

    def _get(self, key: str, default):
        val = self._settings().get(key, default)
        try:
            return type(default)(val)
        except (TypeError, ValueError):
            return default

    def enforce_for(self, resident: str) -> bool:
        s = self._settings()
        return bool(s.get("memory_conductor_enforce", False)) or bool(
            s.get(f"memory_conductor_enforce_{resident}", False)
        )

    # -- жизненный цикл ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.enforce_for("brain") and self._shadow_since is None:
            self._shadow_since = time.time()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="memory-conductor", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        try:
            self._ledger.remove_own()
        except Exception:
            logger.debug("ledger remove_own failed", exc_info=True)

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_sec):
            try:
                self.tick_once()
            except Exception:
                logger.exception("conductor tick failed")

    def wait_workers(self, timeout: float = 5.0) -> None:
        """Дождаться фоновых воркеров (тесты и shutdown)."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                alive = [w for w in self._workers if w.is_alive()]
            if not alive:
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            alive[0].join(timeout=min(left, 0.2))

    # -- лестница ------------------------------------------------------------

    def tick_once(self) -> None:
        if not self._get("memory_conductor_enabled", True):
            # LOW финального гейта: раньше возвращались ДО обновления
            # _pressure_streak — счётчик залипал на последнем значении.
            # Комбо-баг: выключили кондуктора ПОСРЕДИ давления → streak
            # застрял на ≥ need → reload_brain_allowed() при enforce_brain
            # навсегда возвращает False, даже когда кондуктор снова включат
            # и реального давления давно нет. Выключенный кондуктор не смеет
            # хранить состояние, влияющее на будущие решения — сброс streak,
            # как при спокойном тике (level < 2).
            self._pressure_streak = 0
            return
        self._last_tick_ts = time.time()
        if self._shadow_since is None and not self.enforce_for("brain"):
            self._shadow_since = time.time()  # тень видима и без start()
        # давление: одиночное наблюдение НИКОГДА не решает (гистерезис 3 тика)
        level = self._pressure_fn()
        level = 0 if level is None else int(level)
        self._pressure_streak = self._pressure_streak + 1 if level >= 2 else 0

        recording = self._safe_bool(self._is_recording)
        self._gigaam_step(recording)
        self._rewriter_step(recording)
        self._brain_step(recording, trigger="pressure")
        self._publish(recording)

    def _safe_bool(self, fn) -> bool:
        try:
            return bool(fn())
        except Exception:
            return False

    def _gigaam_step(self, recording: bool) -> None:
        threshold = self._get("gigaam_idle_unload_sec", 600.0)
        try:
            idle = float(self._gigaam_idle_sec_fn())
        except Exception:
            return
        if idle < threshold or recording:
            return
        c = self._counters["gigaam"]
        if not self.enforce_for("gigaam"):
            c["would"] += 1
            self._note("would evict gigaam (idle %.0fs)" % idle)
            return
        c["attempted"] += 1
        try:
            if self.gigaam_close_if_idle(threshold):
                c["succeeded"] += 1
                self._note("evicted gigaam (idle %.0fs)" % idle)
        except Exception:
            c["failed"] += 1
            logger.exception("gigaam eviction failed")

    def _rewriter_step(self, recording: bool) -> None:
        threshold = self._get("rewriter_idle_unload_sec", 1800.0)
        try:
            idle = time.monotonic() - float(self._last_stt_activity_ts_fn())
        except Exception:
            return
        if idle < threshold or recording:
            return
        if time.monotonic() < self._cooldown_until.get("rewriter", 0.0):
            return
        c = self._counters["rewriter"]
        if not self.enforce_for("rewriter"):
            c["would"] += 1
            self._note("would evict rewriter (idle %.0fs)" % idle)
            return
        self._evict_model("rewriter", self._get("llm_model", ""))

    def _brain_step(self, recording: bool, trigger: str) -> None:
        need = int(self._get("memory_pressure_streak_ticks", 3))
        # Пейсинг: попытка на каждом need-кратном тике серии, не на каждом —
        # unknown-исход не жжёт cooldown, но и молотить LM Studio каждый тик нельзя.
        if self._pressure_streak < need or self._pressure_streak % need != 0:
            return
        c = self._counters["brain"]
        if recording or self._safe_bool(self._is_meeting_active):
            c["skipped_gate"] += 1
            return
        holder = self._safe_lease()
        if holder and holder.get("owner") != _OWN_LEASE_OWNER:
            c["skipped_gate"] += 1
            self._note("brain eviction blocked: lease held by %r" % holder.get("owner"))
            return
        if time.monotonic() < self._cooldown_until.get("brain", 0.0):
            return
        if not self.enforce_for("brain"):
            c["would"] += 1
            self._note("would evict brain (%s, streak=%d)" % (trigger, self._pressure_streak))
            return
        self._evict_model("brain", self._get("llm_brain_model", ""))

    def _safe_lease(self):
        try:
            return self._lease_holder_fn()
        except Exception:
            return None

    def _evict_model(self, resident: str, model_id: str) -> None:
        """Выгрузка + верификация эффекта (C-EFFECT-CHECK, три исхода).

        Cooldown жжётся ТОЛЬКО после подтверждённого успеха (reserve-before-send):
        неудача/неизвестность не должны блокировать повтор на 10 минут.
        """
        c = self._counters[resident]
        base = self._get("llm_base_url", "http://localhost:1234/v1")
        if not model_id:
            c["skipped_gate"] += 1
            return
        c["attempted"] += 1
        try:
            self.unload_model_fn(base, model_id)
        except Exception:
            c["failed"] += 1
            logger.exception("%s unload call failed", resident)
            return
        deadline = time.monotonic() + self._verify_timeout
        outcome: Optional[bool] = True
        while time.monotonic() < deadline:
            outcome = self.model_loaded_fn(base, model_id)
            if outcome is not True:
                break
            time.sleep(self._verify_poll)
        if outcome is False:
            c["succeeded"] += 1
            self._cooldown_until[resident] = (
                time.monotonic() + self._get("memory_evict_cooldown_sec", 600.0)
            )
            self._note("evicted %s (verified)" % resident)
        elif outcome is None:
            c["unknown"] += 1
            self._note("evict %s: effect UNKNOWN (no cooldown)" % resident)
        else:
            c["failed"] += 1
            self._note("evict %s: still loaded after %.0fs" % (resident, self._verify_timeout))
        if resident == "brain":
            self._update_brain_state(outcome)

    def _update_brain_state(self, loaded: Optional[bool]) -> None:
        """Обновляет кэш последнего ДОСТОВЕРНОГО состояния brain для _publish.

        loaded=False (verify подтвердил выгрузку) → "unloaded"; loaded=True
        (verify подтвердил, что модель всё ещё загружена) → "warm"; loaded=None
        (сеть/таймаут/битый ответ — C-EFFECT-CHECK "неизвестно") → "unknown",
        а НЕ предыдущее значение — устаревшая уверенность так же лжёт, как
        захардкоженное "warm" (см. §3, "None ≠ False", в этот раз симметрично
        и в сторону True)."""
        if loaded is True:
            self._brain_state = "warm"
        elif loaded is False:
            self._brain_state = "unloaded"
        else:
            self._brain_state = "unknown"

    # -- внешние точки входа (чужие потоки!) ---------------------------------

    def handle_oom_event(self, event_type: str, payload: dict) -> None:
        """Листенер шины: СИНХРОННЫЙ в потоке эмиттера — возвращаемся мгновенно."""
        try:
            if event_type != "krab_error" or (payload or {}).get("code") != "mlx.oom":
                return
            # 🔴 MED-1 финального гейта: sysctl = subprocess.run, а мы в потоке
            # STT-пайплайна; подтверждение давления делает ВОРКЕР, не эмиттер.
            self._spawn(self._brain_oom_worker)
        except Exception:
            logger.exception("handle_oom_event failed")

    def _brain_oom_worker(self) -> None:
        c = self._counters["brain"]
        level = self._pressure_fn()
        level = 0 if level is None else int(level)
        if level < 2:
            # mlx.oom имеет историю ложных срабатываний — подтверждаем sysctl
            c["skipped_gate"] += 1
            self._note("oom trigger unconfirmed by pressure (level=%d)" % level)
            return
        recording = self._safe_bool(self._is_recording)
        if recording or self._safe_bool(self._is_meeting_active):
            c["skipped_gate"] += 1
            return
        holder = self._safe_lease()
        if holder and holder.get("owner") != _OWN_LEASE_OWNER:
            c["skipped_gate"] += 1
            return
        if time.monotonic() < self._cooldown_until.get("brain", 0.0):
            return
        if not self.enforce_for("brain"):
            # 🔴 H1 финального гейта: прежний OOM-релиф был БОЕВЫМ по умолчанию
            # (mlx_oom_auto_unload_enabled=True) — shadow не смеет молча снять
            # взведённую страховочную сеть. Легаси-флаг сохраняет прод-поведение
            # до включения enforce_brain.
            if self._get("mlx_oom_auto_unload_enabled", True):
                self._note("oom relief via legacy flag (conductor still shadow)")
                self._evict_model("brain", self._get("llm_brain_model", ""))
            else:
                c["would"] += 1
            return
        self._evict_model("brain", self._get("llm_brain_model", ""))

    def on_recording_start(self) -> None:
        """Секвенс LM Studio на старте записи: unload brain → verify → load rewriter.

        🔴 Ровно ОДИН воркер-поток: два конкурентных fire-and-forget давали бы
        транзиентный пик (+5 ГБ поверх невыгруженных 19) в момент старта Whisper.
        """
        try:
            self._spawn(self._recording_sequence_worker)
        except Exception:
            logger.exception("on_recording_start failed")

    def _recording_sequence_worker(self) -> None:
        if not self.enforce_for("recording_sequence"):
            self._counters["brain"]["would"] += 1
            self._note("would run recording sequence (shadow)")
            return
        base = self._get("llm_base_url", "http://localhost:1234/v1")
        brain = self._get("llm_brain_model", "")
        rewriter = self._get("llm_model", "")
        if brain:
            try:
                self.unload_model_fn(base, brain)
            except Exception:
                logger.exception("sequence: brain unload failed")
                return
            deadline = time.monotonic() + self._verify_timeout
            outcome: Optional[bool] = True
            while time.monotonic() < deadline:
                outcome = self.model_loaded_fn(base, brain)
                if outcome is not True:
                    break
                time.sleep(self._verify_poll)
            # LOW финального гейта: та же витрина, тот же verify-исход — этот
            # секвенс тоже authoritative-проверяет brain, _publish не смеет
            # его игнорировать (иначе после enforce'd recording-sequence
            # состояние осталось бы "unknown" даже после реального verify).
            self._update_brain_state(outcome)
        if rewriter:
            try:
                self.load_model_fn(base, rewriter)
            except Exception:
                logger.exception("sequence: rewriter load failed")

    def reload_brain_allowed(self) -> bool:
        """C-NO-PINGPONG: под enforced pressure-streak reload brain на стопе записи
        пропускается. В shadow ВСЕГДА True + счётчик would_skip (H3)."""
        try:
            need = int(self._get("memory_pressure_streak_ticks", 3))
            active = self._pressure_streak >= need
            if not active:
                return True
            if not self.enforce_for("brain"):
                self._would_skip_brain_reload += 1
                self._note("would skip brain reload (shadow, streak=%d)" % self._pressure_streak)
                return True
            self._note("brain reload SKIPPED (pressure streak=%d)" % self._pressure_streak)
            return False
        except Exception:
            return True  # fail-open: не мешаем существующему поведению

    # -- сервис --------------------------------------------------------------

    def _spawn(self, target) -> None:
        w = threading.Thread(target=target, name="memory-conductor-worker", daemon=True)
        with self._lock:
            self._workers = [x for x in self._workers if x.is_alive()] + [w]
        w.start()

    def _note(self, msg: str) -> None:
        logger.info("conductor: %s", msg)
        self._decisions.append("%s %s" % (time.strftime("%H:%M:%S"), msg))

    def _publish(self, recording: bool) -> None:
        """Write-only витрина; provider-ошибки не роняют тик."""
        try:
            now = time.monotonic()
            idle_g = None
            try:
                idle_g = float(self._gigaam_idle_sec_fn())
            except Exception:
                pass
            entries = {}
            if idle_g is not None:
                entries["gigaam_worker"] = {
                    "size_mb": _SIZE_MB["gigaam"],
                    "state": "active" if recording else "idle",
                    "idle_since_ts": time.time() - idle_g,
                    "reload_cost": "cheap", "pid": None,
                }
            try:
                stt_idle = now - float(self._last_stt_activity_ts_fn())
            except Exception:
                stt_idle = None
            if stt_idle is not None:
                entries["rewriter"] = {
                    "size_mb": _SIZE_MB["rewriter"],
                    "state": "warm" if stt_idle < 300 else "idle",
                    "idle_since_ts": time.time() - stt_idle,
                    "reload_cost": "expensive", "pid": None,
                }
            # LOW финального гейта: раньше "brain" ВСЕГДА публиковался как
            # state="warm"/size_mb=20000 — даже сразу после подтверждённой
            # выгрузки, витрина врала про несуществующие 19 ГБ. size_mb
            # отражает то же состояние, что и "state": честные 0 при
            # unloaded, null (неизвестно — не 0 и не полный размер) при
            # unknown, реальный размер только при подтверждённом warm.
            brain_size_mb: Optional[int]
            if self._brain_state == "warm":
                brain_size_mb = _SIZE_MB["brain"]
            elif self._brain_state == "unloaded":
                brain_size_mb = 0
            else:
                brain_size_mb = None
            entries["brain"] = {
                "size_mb": brain_size_mb, "state": self._brain_state,
                "reload_cost": "expensive", "pid": None,
            }
            self._ledger.publish_own(entries)
        except Exception:
            logger.debug("ledger publish failed", exc_info=True)

    def get_diagnostics(self) -> dict[str, Any]:
        t = self._thread
        return {
            "enabled": self._get("memory_conductor_enabled", True),
            "thread_alive": bool(t is not None and t.is_alive()),
            "last_tick_ts": self._last_tick_ts,
            "shadow_since": self._shadow_since,
            "pressure_streak": self._pressure_streak,
            "would_skip_brain_reload": self._would_skip_brain_reload,
            "residents": {k: dict(v) for k, v in self._counters.items()},
            "decisions": list(self._decisions),
        }
