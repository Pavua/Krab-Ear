# Memory Conductor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Spec (authoritative): `docs/superpowers/specs/2026-08-19-memory-conductor-design.md` (v2.1).
> Base SHA: `ed854a3f` (origin/codex/krab-ear-v2). Every worker: isolated worktree,
> first action `git checkout -b <task-branch>`. Never push to shared branches.

**Goal:** one shared memory picture (ledger) + symmetric moderate eviction policy;
week-one SHADOW (log decisions, execute nothing).

**Architecture:** write-only JSON ledger (sidecar flock) + per-process executors:
IPC conductor (gigaam/rewriter/brain), REST idle-reaper (whisper worker). Policy
reads ONLY in-process state (C-POLICY-SOURCE).

**Tech:** Python 3.14 dev / 3.12 CI (no mlx on CI), unittest/pytest, Swift 6 menu bar.

## Global Constraints (from spec — every task inherits)

- C-POLICY-SOURCE: policy code MUST NOT import the ledger reader (AST-tested).
- C-INFLIGHT: evictions atomic with in-flight work via the resident's OWN lock.
- C-ONE-PATH: ledger path = one formula, NO env override; log full path on first touch.
- Shadow default: `memory_conductor_enforce=False`; enforcement per-resident later.
- Fail direction: any ledger failure → dictation untouched; sysctl missing → "no pressure".
- Gates for EVERY task: pytest (file), flake8 CI-cmd (max-line-length=150 --extend-ignore=E501),
  `bash scripts/pre_merge_py312_check.sh <test files>`, `make audit-all` for new modules.
- 🔴 Tests creating BackendService MUST call service.close() in tearDown (#1782).
- 🔴 No daemon threads started at module import (rest_server is imported by test chunks).

---

### Task 1: backend/memory_ledger.py

**Files:** Create `KrabEar/backend/memory_ledger.py`, Test `KrabEar/tests/test_memory_ledger_2026_08_19.py`
**Produces:** `resolve_ledger_path() -> Path`; `class LedgerClient(owner: str, path: Path | None = None, lock_timeout_sec: float = 1.0)` with `publish_own(entries: dict[str, dict]) -> bool` (True=published; False=lock contention, counted in `self.skipped_publishes`), `read_all(nowait: bool = False) -> dict` (full ledger doc; `{"v":1,"entries":{}}` when unreadable/locked-nowait).

- [ ] **Step 1: failing tests** — cover: (a) resolve path is `~/.openclaw/memory_ledger.json`, absolute, no env influence (set fake env var, assert unchanged); (b) publish_own writes ONLY `<owner>/`-prefixed keys and preserves other owners' fresh entries (RMW-delta); (c) entry with `updated_ts` older than 120s OR missing → dropped by next publish (fail-closed GC); (d) corrupt JSON → backed up as `.corrupt-<ts>`, retention max 5, publish succeeds on fresh file; (e) two LedgerClients (different owners) publishing concurrently (threads×50) lose nothing of each other; (f) sidecar lock: data file itself is NEVER flocked (assert lock file separate); (g) read_all(nowait=True) under held lock → empty doc, not a hang.
- [ ] **Step 2: run — expect ModuleNotFoundError**
- [ ] **Step 3: implement**

```python
"""Write-only memory ledger shared between Krab Ear and Krab (spec §4).

🔴 Sidecar lock, NOT the brain_lease pattern: brain_lease flocks the data file
and os.replace()s it under its own flock, splitting the lock domain across
inodes. Here the SIDECAR (never replaced) is the only flocked file; the data
file is written atomically (tmp+os.replace) while holding the sidecar lock.
🔴 C-ONE-PATH: one pure formula, no env channel (the P0d/12.07 class).
"""
from __future__ import annotations
import fcntl, json, logging, os, tempfile, time
from pathlib import Path

logger = logging.getLogger("KrabEar.Backend.MemoryLedger")
LEDGER_FILENAME = "memory_ledger.json"
LEDGER_LOCK_FILENAME = "memory_ledger.lock"
LEDGER_TTL_SEC = 120.0
_CORRUPT_KEEP = 5
_SCHEMA_V = 1

def resolve_ledger_path() -> Path:
    return (Path.home() / ".openclaw" / LEDGER_FILENAME).resolve()

class LedgerClient:
    def __init__(self, owner, path=None, lock_timeout_sec=1.0):
        self._owner = str(owner)
        self._path = Path(path) if path else resolve_ledger_path()
        self._lock_path = self._path.with_name(LEDGER_LOCK_FILENAME)
        self._lock_timeout = float(lock_timeout_sec)
        self.skipped_publishes = 0
        self._logged_path = False

    def _log_path_once(self):
        if not self._logged_path:
            logger.info("memory ledger: %s (owner=%s)", self._path, self._owner)
            self._logged_path = True

    def _acquire(self, nowait: bool):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if nowait:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        except OSError:
            os.close(fd)
            raise

    def _load(self) -> dict:
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("entries"), dict):
                return doc
        except FileNotFoundError:
            return {"v": _SCHEMA_V, "entries": {}}
        except Exception:
            pass
        # corrupt → backup aside with retention, start fresh (never raise)
        try:
            bk = self._path.with_name(f"{self._path.name}.corrupt-{int(time.time())}")
            os.replace(self._path, bk)
            siblings = sorted(self._path.parent.glob(f"{self._path.name}.corrupt-*"))
            for old in siblings[:-_CORRUPT_KEEP]:
                old.unlink(missing_ok=True)
            logger.warning("memory ledger corrupt — backed up to %s", bk.name)
        except OSError:
            pass
        return {"v": _SCHEMA_V, "entries": {}}

    def _write_atomic(self, doc: dict):
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".ledger_tmp_")
        try:
            os.write(fd, json.dumps(doc, ensure_ascii=False).encode())
            os.close(fd); fd = -1
            os.replace(tmp, self._path)
            self._path.chmod(0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            Path(tmp).unlink(missing_ok=True)

    def publish_own(self, entries: dict) -> bool:
        self._log_path_once()
        now = time.time()
        try:
            lock_fd = self._acquire(nowait=False)
        except OSError:
            self.skipped_publishes += 1
            logger.warning("memory ledger: lock contention — publish skipped (%d total)",
                           self.skipped_publishes)
            return False
        try:
            doc = self._load()
            kept = {}
            for key, e in doc["entries"].items():
                if key.startswith(self._owner + "/"):
                    continue  # replaced below
                ts = e.get("updated_ts") if isinstance(e, dict) else None
                if isinstance(ts, (int, float)) and (now - ts) <= LEDGER_TTL_SEC:
                    kept[key] = e  # missing/stale ts = dead → GC (fail-closed)
            for name, e in entries.items():
                kept[f"{self._owner}/{name}"] = {**e, "owner": self._owner,
                                                 "resident": name, "updated_ts": now}
            self._write_atomic({"v": _SCHEMA_V, "entries": kept})
            return True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)

    def remove_own(self) -> None:
        """Graceful shutdown: drop own entries (spec L2). Best-effort."""
        try:
            lock_fd = self._acquire(nowait=True)
        except OSError:
            return
        try:
            doc = self._load()
            doc["entries"] = {k: v for k, v in doc["entries"].items()
                              if not k.startswith(self._owner + "/")}
            self._write_atomic(doc)
        except Exception:
            logger.debug("memory ledger: remove_own failed", exc_info=True)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)

    def read_all(self, nowait: bool = False) -> dict:
        self._log_path_once()
        try:
            lock_fd = self._acquire(nowait=nowait)
        except OSError:
            return {"v": _SCHEMA_V, "entries": {}}
        try:
            return self._load()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)
```

(Worker: remove the dead `publish_own({}) if False else None` line — it is a
plan artefact; `remove_own` body starts at the try/_acquire.)
- [ ] **Step 4: tests green** — `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_memory_ledger_2026_08_19.py -q`
- [ ] **Step 5: gates + commit** `feat(memory): write-only ledger client (sidecar flock, TTL fail-closed GC)`

---

### Task 2: MLXWhisperSession in-flight hardening

**Files:** Modify `KrabEar/core/mlx_whisper_session.py`, Test `KrabEar/tests/test_whisper_session_close_if_idle_2026_08_19.py`
**Interfaces produced:** `MLXWhisperSession.close_if_idle(idle_sec: float) -> bool`; module-level `peek_session() -> MLXWhisperSession | None` (NEVER creates); `session.last_used_ts: float` (monotonic), `session.inflight: int` (read-only properties ok).

🔴 Gotchas (verified in code): `self._lock = threading.Lock()` is NON-reentrant and
`_send()` calls `self.close()` INSIDE `with self._lock:` (≈ lines 313-346) — naive
"close takes lock" self-deadlocks (W-#1872 class). `transcribe()` releases the lock
between `start()` and `_send()` — that gap is the race the tests must pin.

- [ ] **Step 1: failing tests** — (a) close_if_idle returns False and does NOT kill the proc while a transcribe is mid-flight (thread A in a stubbed slow \_send holding the lock is not enough — also cover the start→_send GAP: stub start to complete, pause before _send, call close_if_idle, assert False because inflight>0); (b) close_if_idle True + proc terminated when idle ≥ threshold; (c) internal error path in _send still closes without deadlock (simulate BrokenPipeError; test completes < 5 s); (d) peek_session() returns None when no session exists and does NOT create one.
- [ ] **Step 2: run — FAIL (attributes missing)**
- [ ] **Step 3: implement** — in `__init__`: `self._inflight = 0`, `self._last_used_ts = time.monotonic()`. In `transcribe()` wrap the whole body:

```python
with self._lock:
    self._inflight += 1
    self._last_used_ts = time.monotonic()
try:
    ...existing start()+_send() flow...
finally:
    with self._lock:
        self._inflight -= 1
        self._last_used_ts = time.monotonic()
```

Split close: public `close()` = `with self._lock: self._close_unlocked()`; rename the
current body to `_close_unlocked()`; every `self.close()` call already inside the
lock (in \_send) becomes `self._close_unlocked()`. Add:

```python
def close_if_idle(self, idle_sec: float) -> bool:
    with self._lock:
        if self._proc is None or self._inflight > 0:
            return False
        if (time.monotonic() - self._last_used_ts) < idle_sec:
            return False
        self._close_unlocked()
        return True

def peek_session():
    with _session_lock:
        return _session
```

- [ ] **Step 4: green + existing suite** — also run `KrabEar/tests/` files matching `mlx_whisper` (regression).
- [ ] **Step 5: gates + commit** `fix(whisper): in-flight-safe close, close_if_idle, peek_session`

---

### Task 3: lm_studio model_loaded helper

**Files:** Modify `KrabEar/backend/lm_studio_lifecycle.py`, Test `KrabEar/tests/test_lm_studio_model_loaded_2026_08_19.py`
**Produces:** `model_loaded(base_url: str, model_id: str, timeout: float = 5.0) -> bool | None` — True/False from LM Studio's model list (reuse THIS module's existing endpoint constants/normalization — do not invent URLs, #396/#1815); **None = unknown** (HTTP error/timeout) — the three-state lesson: callers must not read None as False.

- [ ] Steps: failing tests (loaded / not loaded / connection error → None; mock requests) → implement (~25 lines, mirror \_try_rest_unload's URL building) → green → gates → commit `feat(lmstudio): model_loaded three-state probe`.

---

### Task 4: whisper cold-start bench (informs settings default)

**Files:** Create `scripts/bench_whisper_cold_start.py` (dev-only, NOT in CI).
- [ ] Measure: kill worker (existing `kill_mlx_whisper_session`-style path via REST restart on DEV instance only), time first `/v1/stt/transcribe` (persist_history=false) vs warm second call, 3 rounds. Print median cold/warm.
- [ ] Numeric gate (write result into the PR description AND task 7's settings step): cold-start > 5 s → `reload_cost="expensive"` for whisper (pressure-only eviction); ≤ 5 s → keep cheap, default `whisper_idle_unload_sec=900`.
- [ ] Commit `chore(bench): whisper cold-start measurement`.

---

### Task 5: backend/memory_conductor.py

**Files:** Create `KrabEar/backend/memory_conductor.py`, Test `KrabEar/tests/test_memory_conductor_2026_08_19.py`
**Consumes:** LedgerClient (T1), model_loaded (T3), `unload_model_async`/`load_model_async`, `current_lease_holder`.
**Produces:** `class MemoryConductor(settings_service, ledger, *, is_recording, is_conversation_active, pressure_fn, gigaam_evict_fn, tick_sec=30.0)` with `start()/stop()`, `handle_oom_event(event_type, payload)` (EventBus listener — returns immediately, work on own thread), `get_diagnostics() -> dict` (thread_alive, last_tick_ts, shadow_since, pressure_streak, per-resident counters attempted/succeeded/skipped_gate + last decisions ring ≤20), `on_recording_start()` (sequenced LM Studio worker), `reload_brain_allowed() -> bool` (for T7's no-pingpong).

Ladder per tick (spec §5, ALL decisions logged with reason; enforce flag checked
LAST so shadow logs the would-be action):
1. gigaam: idle ≥ `gigaam_idle_unload_sec` (600) AND not is_recording() → evict via injected fn (adapter's own lock inside).
2. rewriter: idle ≥ `rewriter_idle_unload_sec` (1800) → unload_model_async + verify via model_loaded (three-state; None → outcome "unknown", cooldown NOT burned).
3. brain: pressure_fn() ≥ 2 on ≥3 consecutive ticks AND lease holder not foreign AND not is_conversation_active() AND not is_recording() → unload + verify. Cooldown 600 s только после verified success.
4. `handle_oom_event`: mlx.oom CONFIRMED by pressure_fn() ≥ 2 → same brain branch (single decision point; OomAutoRelief wiring change is T7).
5. `on_recording_start()`: ONE worker thread: brain unload → poll model_loaded until False (≤10 s) → rewriter load. Shadow-gated like everything.

- [ ] **Step 1: failing tests** — state-matrix: shadow logs but никогда не вызывает executors; hysteresis needs exactly 3 ticks (2 не хватает); recording blocks gigaam+brain; foreign lease blocks brain; conversation blocks brain; verify-None не жжёт cooldown; verify-True жжёт; sequencing: rewriter load не стартует до подтверждённой выгрузки brain; handle_oom без давления → skipped_gate; thread liveness fields present; ladder module does NOT import ledger reader (AST: no `read_all` usage — C-POLICY-SOURCE; publish is fine).
- [ ] **Step 2-4: RED → implement → GREEN.** Implementation ~200 строк; каждый executor call в try/except с three-state outcome; тик публикует декларации (`publish_own`) ПОСЛЕ решений.
- [ ] **Step 5: gates + commit** `feat(memory): conductor — moderate ladder, shadow-first`

---

### Task 6: REST idle-reaper

**Files:** Modify `KrabEar/backend/rest_server.py` (+ `KrabEar/backend/rest_inprocess.py` start hook), Test `KrabEar/tests/test_rest_idle_reaper_2026_08_19.py`
**Consumes:** T1 LedgerClient(owner="krab_ear_rest"), T2 close_if_idle/peek_session.

- [ ] Failing tests: (a) reaper thread NOT started at module import (import rest_server in a fresh subprocess, assert no thread named "whisper-idle-reaper"); (b) start hook launches it in serve path; (c) tick with idle session → close_if_idle called; (d) peek only — no session created when none exists; (e) publishes `krab_ear_rest/mlx_whisper_worker` with state active|idle from inflight/last_used.
- [ ] Implement: `def start_whisper_idle_reaper(interval_sec=60.0)` — daemon thread, guarded by `mlx_whisper_worker_enabled()`; called from BOTH run paths: the `__main__` serve block AND `InProcessRestServer.start` (M2 mode — иначе реапер мёртв там). Threshold/classification читаются из settings snapshot на тик (cheap: REST уже умеет читать settings).
- [ ] Gates (включая chunk-repro осторожность: никаких module-level стартов) + commit `feat(rest): whisper idle-reaper (run-path start only)`

---

### Task 7: wiring + settings + IPC + no-pingpong

**Files:** Modify `KrabEar/backend/service.py` (рядом с ErrorBus, ~660), `KrabEar/core/config.py` (DEFAULT_SETTINGS), `KrabEar/backend/settings_validator.py` (_RANGE_FIELDS), `KrabEar/backend/recording_core_service.py` (start ~1304, stop ~2781), `KrabEar/backend/oom_auto_relief.py`; Tests: `KrabEar/tests/test_memory_conductor_wiring_2026_08_19.py`.

- [ ] Settings (+bounds): `memory_conductor_enabled=True`, `memory_conductor_enforce=False`, `gigaam_idle_unload_sec=600 (60..86400)`, `whisper_idle_unload_sec=900 (60..86400)` (adjust per T4 bench), `rewriter_idle_unload_sec=1800 (300..86400)`, `memory_pressure_streak_ticks=3 (2..20)`, `memory_evict_cooldown_sec=600 (60..86400)`.
- [ ] service.py: construct `LedgerClient("krab_ear")` + `MemoryConductor(...)`, `event_bus.add_listener(conductor.handle_oom_event)` (🔴 синхронный листенер — handle_oom_event обязан возвращаться мгновенно, как OomAutoRelief), conductor.start(); close() зовёт conductor.stop() + ledger.remove_own(). Source-contract тест на все три вызова (урок setupErrorBus).
- [ ] OomAutoRelief: перестаёт действовать сам — его триггер делегирует в conductor (single decision point). Обновить его тесты соответственно (они закрепляют старое поведение — переписать, не удалять классы гейтов).
- [ ] recording_core_service: (a) start ~1304 — заменить прямой unload_model_async на `conductor.on_recording_start()` (sequenced); (b) stop ~2781 — обернуть reload гейтом `if conductor.reload_brain_allowed():` + лог/счётчик skip (C-NO-PINGPONG).
- [ ] IPC `get_memory_ledger {}` → `{ok, ledger: read_all(nowait=True), conductor: get_diagnostics()}` (nowait — health-probe lesson) + секция в get_diagnostics + запись в docs/IPC_API_REFERENCE.md.
- [ ] Gates: полный смежный прогон (recording tests, oom tests, dispatch invariants), e2e smoke `scripts/run_e2e_smokes.command`, commit `feat(memory): wire conductor (shadow), no-pingpong, get_memory_ledger`.

---

### Task 8: Swift menu-bar line

**Files:** Create `native/KrabEarAgent/Sources/KrabEarAgent/main+MemoryLine.swift` (образец: `main+BrainLease.swift` — B3 pattern), Test `native/KrabEarAgent/Tests/KrabEarAgentTests/MainMemoryLineWiringTests.swift`.
- [ ] Disabled-строка в статус-меню после brain-lease строки: «Память: brain 19Г · whisper idle 4м»; refresh в menuWillOpen через `get_memory_ledger` (IPC off-main — AGENT-3); скрыта при `memory_conductor_enabled=false`; при shadow ≥7 дней — суффикс «· shadow N дн». IPC-провал → «Память: —» (не скрывать).
- [ ] Source-contract тест на реальный вызов setup из main.swift (класс setupErrorBus). Глиф-гейт: только уже используемые символы. `swift build -c release` + `swift test`.
- [ ] Commit `feat(agent): memory line in status menu (B3 pattern)`.

---

### Task 9: live smoke + rollout docs

- [ ] Dev-instance (throwaway data-dir, порядок `scripts/run_e2e_smokes.command`): (a) idle→evict→respawn round-trip для whisper (форсировать маленький threshold через KRAB_EAR_-env); (b) injected pressure_fn прогон brain-ветки в enforce на DEV (боевой путь до прода — emergency-mechanism lesson); (c) конкурентные publish из двух процессов — леджер цел.
- [ ] Прод: деплой в shadow; NOW.md — карточка волны + правило «enforce включает владелец после недели shadow»; бриф Крабу (cross-session + .remember/): схема §4, префикс `krab/`, их лестница.
