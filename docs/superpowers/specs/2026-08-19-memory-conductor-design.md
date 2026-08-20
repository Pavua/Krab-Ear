# Memory Conductor (Дирижёр памяти) — design v2.1

Date: 2026-08-19 · Status: v2 reworked after review round 1 (3H/8M/5L); v2.1 applies round 2
(H3 CLOSED, H1/H2 PARTIAL + 1 new HIGH, all gated by hand). Ready for owner review.
Owner decisions: scope (в) full; symmetric ledger, no arbiter; moderate policy;
UI = menu-bar line now, agy panel later.

**RU TL;DR:** общий JSON-леджер деклараций памяти между Krab Ear и Крабом.
Каждый выгружает только своё. Дешёвое — по idle, brain 19 ГБ — только под
устойчивым давлением. Политика читает ТОЛЬКО своё in-process состояние; леджер —
write-only витрина. Первая неделя — shadow (решения логируются, не исполняются).

## 1. Problem

M4 Max 36 GB chronically saturated: swap 92%, mlx.oom toasts, P0 SEGV, 107
unclean deaths/24d, VG timeout spike in the pressure window. Residents:
brain ~19-20 GB (unload-on-recording flag + reactive OOM), rewriter ~5 GB
(always-on), mlx_whisper_worker ~1.8 GB in the REST process (lives forever after
first use — heaviest process in the system), gigaam_worker (forever after first
use), Krab's LM Studio usage (invisible to us). Nobody sees the whole picture.

## 2. Goals / non-goals

Goals: one glanceable shared picture; deterministic symmetric policy over ONE'S
OWN residents; zero new failure modes for dictation.
Non-goals: no central arbiter; no runtime merging (PRD); never evict the other
side's residents; no RPC negotiation — declarations + shared rules only.

## 3. Hard contracts (review-driven, each maps to a finding)

- **C-POLICY-SOURCE (M2):** eviction policy reads ONLY in-process state (adapter
  locks, live timestamps, sysctl). The ledger is a WRITE-ONLY observability
  output; no policy code imports the ledger reader. Pinned by an AST/source test.
- **C-INFLIGHT (H2):** every eviction executes through the resident's OWN lock so
  "idle check + evict" is atomic with in-flight work: whisper →
  `MLXWhisperSession.close_if_idle(threshold)` taking `self._lock`; gigaam →
  under its `_spawn_lock` with in-flight check; brain/rewriter → LM Studio API
  (no process kill). `is_recording` is NOT a sufficient gate: recorder.py flips
  it False at stop BEFORE final transcription (recorder.py:239) — finalization
  is protected by the in-flight locks, not by recording state.
  🔴 Two implementation constraints (round-2, verified): (a) `_send()` calls
  `self.close()` INSIDE its own `with self._lock:` (lines ~313-346) and the lock
  is a non-reentrant `threading.Lock` — naive "close() takes the lock" is a
  prescribed self-deadlock (W-#1872 class). Required shape: split
  `close()`/`_close_unlocked()`; public close/close_if_idle take the lock,
  internal error paths call `_close_unlocked()`. (b) `transcribe()` releases the
  lock between `start()` and `_send()` — a reaper firing in that gap kills a
  just-started request. Fix: set an in-flight marker / bump `last_used_ts` under
  the lock at `transcribe()` ENTRY; the §10 race test must target exactly this
  window.
- **C-EXECUTOR-LOCALITY (H1):** whoever OWNS a resident runs the eviction for it.
  IPC backend conductor: gigaam, rewriter, brain. REST process: its
  mlx_whisper_worker via its own lightweight idle-reaper thread (tick 60 s,
  calls `close_if_idle`). No cross-process kills, no pid signalling via ledger.
  Reaper start locus (round-2): in the RUN path only (`__main__` serve path AND
  `InProcessRestServer.start`), NEVER at module import — rest_server.py is
  imported by chunked tests and module-level daemon threads are the #1782 class.
  The reaper must NOT call `get_mlx_whisper_session()` (it CREATES a session);
  it peeks the existing `_session` under `_session_lock` only.
- **C-PRESSURE-HYSTERESIS (H3):** brain eviction requires
  `kern.memorystatus_vm_pressure_level >= 2` on ≥3 CONSECUTIVE conductor ticks
  (covers ≥60 s at tick 30 s), never a single observation. `mlx.oom` as a trigger must be CONFIRMED
  by the same sysctl (M7: the oom classifier has a false-positive history).
- **C-NO-PINGPONG (H3):** while pressure-streak is active, the unconditional
  brain reload in `stop_recording` (recording_core_service.py:2781-2788) is
  SKIPPED (logged, counted). This is the explicit interplay with
  `llm_brain_unload_on_recording`: unload-on-start stays; reload-on-stop becomes
  pressure-aware. Without this the ladder and the reload fight over 19 GB.
- **C-EFFECT-CHECK (M6):** every eviction records THREE-state outcome:
  attempted / succeeded / skipped_gate(reason). Success is verified (LM Studio
  model list via the EXISTING probe endpoint — reuse llm_probe, do not invent
  URLs (#396/#1815); worker death via pid wait). Fire-and-forget without effect
  check + cooldown would fake "handled" for 10 minutes.
- **C-OWN-WORK-GATE (M3):** `unload_model_async` is NOT lease-gated itself (the
  gate lives in callers — verified). Conductor gates brain eviction on: foreign
  lease holder OR own active conversation/meeting session.
- **C-ONE-PATH (M8):** ledger path = one pure formula, NO env override channel
  (the P0d/12.07 two-channel class). Both processes log the full resolved path
  on first touch. `resolve_ledger_path()` mirrors `resolve_token_path()`.

## 4. Ledger

Path: `~/.openclaw/memory_ledger.json` + sidecar `memory_ledger.lock`.
Locking (M1 — explicitly NOT the brain_lease pattern, which flocks the data file
and `os.replace`s it under its own flock, splitting the lock domain): flock the
SIDECAR only (it is never replaced); data file written atomically (tmp+replace)
under that flock; blocking flock with short timeout; contention → skip this
publish cycle loudly (counter), never silently drop forever.

Schema v1 — entries keyed `<owner>/<resident>`; owners: `krab_ear` (IPC),
`krab_ear_rest` (REST, owns mlx_whisper_worker — L1 example fixed), `krab`.
Fields: owner, resident, size_mb, state(active|warm|idle), idle_since_ts?,
reload_cost(cheap|expensive), pid, updated_ts. Each writer patches ONLY its own
prefix as a delta under the sidecar lock (RMW-wipe guarded by test). Entries with
missing or stale (`>120 s`) `updated_ts` = dead → ignored + GC by any writer
(fail-closed for malformed partner data, L2). Graceful shutdown removes own
entries (hooked next to GracefulShutdownHandler). Corrupt file → backup
`.corrupt-<ts>` (retention: 5 newest, L4), start fresh, never raise.
Known limit (L3): under a swap storm a live process may miss the 120 s TTL and
vanish from the UI picture — policy is unaffected (C-POLICY-SOURCE), documented.

## 5. Policy (moderate ladder)

1. In-flight work always wins (C-INFLIGHT); recording/meeting also blocks all
   eviction of STT residents as a belt-and-suspenders outer gate.
2. cheap+idle ≥ `idle_unload_sec` → evict. Defaults: gigaam 600 s; whisper
   worker `whisper_idle_unload_sec` — default chosen AFTER a cold-start bench
   (M5: eviction re-opens the VG cold-latency class P0a/P0c closed; if cold
   start > ~5 s, reclassify reload_cost=expensive and only pressure-evict).
   Bench is a plan task with a numeric gate written into settings docs.
3. rewriter: idle 1800 s + PROACTIVE reload on recording start (M4: otherwise
   the first paste after a pause eats a synchronous ~90 s self-heal load).
   Ordering (round-2): recording-start actions to LM Studio are SEQUENCED in one
   worker thread — brain unload → verified → rewriter reload; never two
   concurrent fire-and-forget threads (transient +5 GB on top of undischarged
   19 GB exactly at Whisper start). M4-reload is part of the ladder: it is
   shadow-gated like evictions and has its own per-resident enforce flag.
4. brain: pressure-streak only (C-PRESSURE-HYSTERESIS), gated by
   C-OWN-WORK-GATE. Cooldown per resident 600 s AFTER a verified success;
   failed attempts do not burn the cooldown (reserve-before-send lesson).
5. Master switch `memory_conductor_enabled`; all thresholds in DEFAULT_SETTINGS
   + `_RANGE_FIELDS`.

## 6. Components

- `backend/memory_ledger.py` — resolve_ledger_path(), LedgerClient
  (publish_own/gc/read_all), import-light (REST-safe).
- `backend/memory_conductor.py` — IPC-side daemon thread, tick 30 s: publish own
  declarations, run ladder over gigaam/rewriter/brain, three-state counters,
  `thread_alive` + `last_tick_ts` in diagnostics.
- REST side: idle-reaper thread (60 s) + `close_if_idle` on MLXWhisperSession
  (with the new in-flight lock) + own ledger publishing under `krab_ear_rest/`.
- `OomAutoRelief` becomes a trigger source into the conductor (pressure-confirmed),
  not a parallel actor — single decision point for brain eviction.
- IPC `get_memory_ledger {}` (nowait sidecar flock, L5 — the health-probe lesson)
  + `get_diagnostics.memory_conductor` section.
- Wiring pinned by source-contract tests (the setupErrorBus lesson).

## 7. UI

v1: menu-bar line (B3 pattern): "Память: brain 19Г · whisper idle 4м", refresh
in menuWillOpen, hidden when disabled. v2: agy/Gemini panel, data only via
`get_memory_ledger`, styling-only brief, keys pinned by us.

## 8. Krab's half (cross-repo)

Brief after core merges: same LedgerClient contract, `krab/` prefix, same ladder
over their residents. Until then we run solo. Malformed partner entries are
ignored fail-closed (§4); their absence never blocks our policy (C-POLICY-SOURCE).
Accepted residual (round-2): under sustained pressure Ear evicts brain, Krab's
next message JIT-loads it via LM Studio itself — a slow cycle bounded by the
600 s cooldown. Accepted for v1; revisit with the budget protocol if it shows up
in shadow logs.

## 9. Failure directions

Ledger unreadable → log once, empty view, dictation untouched. Sysctl
unavailable → "no pressure" (keep models warm). Conductor/reaper death visible
via liveness fields. No retry loops; cooldown only after verified success.

## 10. Testing / rollout

TDD. Units: sidecar-lock concurrency (two writers + concurrent GC), TTL/missing-ts
fail-closed, ladder state matrix, C-INFLIGHT atomicity (evict racing a live
request loses), pressure hysteresis, no-pingpong skip, three-state outcomes.
Source-contracts: wiring; C-POLICY-SOURCE import ban. ubuntu-parity (no mlx).
Bench task: whisper cold-start (numeric gate for threshold/classification).
Live smoke on dev instance: idle→evict→respawn round-trip; injected pressure_fn
run of the brain branch (emergency-mechanism lesson).
**Rollout: week one in SHADOW mode — ladder logs decisions, executes nothing**
(risky-change rule; also gives the pressure branch a real-machine dry run);
enforce flips per-resident after shadow logs look sane. Shadow must be VISIBLE
(the never-flipped-flag class): `shadow_since` in diagnostics; after 7 days the
menu-bar line shows «Дирижёр: shadow N дн» until the owner flips enforce.
