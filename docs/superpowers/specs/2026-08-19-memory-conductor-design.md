# Memory Conductor (Дирижёр памяти) — design

Date: 2026-08-19 · Status: draft, pending adversarial review
Owner decisions: scope (в) full (own workers + LM Studio + Krab budget), symmetric ledger
(no arbiter), moderate eviction policy, UI = menu-bar line now + agy panel later.

**RU TL;DR:** единый взгляд на память между Krab Ear и Главным Крабом через общий
JSON-леджер деклараций. Каждый выгружает только своё, по одинаковым правилам:
дешёвое к перезагрузке — по idle-таймауту, дорогое (brain 19 ГБ) — только под
давлением памяти. Отказ леджера = молчаливый откат к сегодняшнему поведению.

## 1. Problem

M4 Max 36 GB is chronically saturated: swap at 92%, mlx.oom toasts, P0 SEGV
(second MLX checkpoint under pressure), 107 unclean process deaths in 24 days,
VG-observed timeout spike correlated with the pressure window. Residents today:

| Resident | Size | Lifetime today |
|---|---|---|
| LM Studio brain (qwen/qwen3.6-27b) | ~19-20 GB | unloaded on recording start (flag), reactive on OOM (new) |
| LM Studio rewriter | ~5 GB | always-on, never evicted |
| mlx_whisper_worker (REST, P0c) | ~1.8 GB | forever after first use — heaviest process in the system |
| gigaam_worker | ~0-1.5 GB | forever after first use |
| Krab's own LM Studio usage | varies | invisible to us |

Nobody sees the whole picture. Coordination is only the advisory brain-lease
(mutual exclusion for inference, not memory) plus two point fixes.

## 2. Goals / non-goals

Goals: (a) one shared, glanceable picture of who holds what; (b) deterministic,
symmetric eviction policy each side applies to ITS OWN residents; (c) zero new
failure modes for dictation.

Non-goals: no central arbiter process; no merging runtimes (PRD non-goal); no
eviction of the OTHER side's residents, ever; no realtime negotiation protocol —
the ledger is declarations + shared rules, not RPC.

## 3. Ledger

Path: `~/.openclaw/memory_ledger.json` (sibling of `lm_studio_brain.lock`).
Concurrency: `fcntl.flock` on a sidecar `.lock` + atomic write (tmp + os.replace)
— same pattern as brain_lease. Schema (versioned):

```json
{
  "v": 1,
  "entries": {
    "krab_ear/mlx_whisper_worker": {
      "owner": "krab_ear", "resident": "mlx_whisper_worker",
      "size_mb": 1800, "state": "idle",          // active | warm | idle
      "idle_since_ts": 1766140000.0,             // unix, absent unless idle
      "reload_cost": "cheap",                    // cheap | expensive
      "pid": 12345, "updated_ts": 1766140100.0
    }
  }
}
```

Rules: each owner writes ONLY keys prefixed `<owner>/`; a read-modify-write under
flock patches the own subset as a delta (RMW-wipe class guarded by test). Entries
with `updated_ts` older than `LEDGER_TTL_SEC=120` are treated as dead (crashed
process) and ignored by policy AND garbage-collected by any writer. Corrupt file
→ back up aside (`.corrupt-<ts>`), start fresh — never raise into callers.

## 4. Policy (moderate)

Deterministic ladder, evaluated by each side on its own tick over its own residents:

1. NEVER evict while own recording/meeting is active (existing `is_recording`
   callbacks) — dictation always wins.
2. `reload_cost=cheap` + `state=idle` for ≥ `idle_unload_sec` (default 600) → evict.
   Ear residents: mlx_whisper_worker (terminate; P0f respawn covers reload),
   gigaam_worker (existing spawn lock covers reload).
3. rewriter: same rule with its own default 1800 s (`rewriter_idle_unload_sec`).
4. `reload_cost=expensive` (brain): evicted ONLY on memory pressure —
   `kern.memorystatus_vm_pressure_level >= 2` on tick, or mlx.oom event (existing
   OomAutoRelief becomes a trigger source feeding the conductor instead of a
   parallel actor). Foreign brain-lease holder blocks eviction (existing gate).
5. All thresholds in DEFAULT_SETTINGS + `_RANGE_FIELDS`; master switch
   `memory_conductor_enabled` (default True), per-resident opt-outs not needed v1.

## 5. Components (Ear side)

- `backend/memory_ledger.py` — read/patch-own-delta/gc; pure functions + small
  `LedgerClient`; no imports of heavy modules (cheap for REST process too).
- `backend/memory_conductor.py` — daemon thread in the IPC backend, tick 30 s:
  refresh own declarations (whisper worker aliveness/idleness comes from REST
  process? — NO: REST process declares its own worker itself, see below),
  apply ladder to own residents, log every eviction decision with reason.
- REST process (`rest_server.py`) declares `mlx_whisper_worker` itself (it owns
  the child); IPC backend declares gigaam_worker, rewriter (via LM Studio API
  state), brain (when it loaded it). Two writers, disjoint key prefixes
  (`krab_ear_rest/`, `krab_ear/`) — no write conflicts by construction.
- Wiring in `service.py` + source-contract test (add_listener/start called) —
  the setupErrorBus lesson.
- IPC: `get_memory_ledger {}` → full ledger + own policy verdicts (for UI/agents);
  section in `get_diagnostics`.
- Eviction executors reuse existing, battle-tested paths only: worker terminate
  (P0f respawn), `unload_model_async` (lease-gated), no new kill paths.

## 6. UI

v1 (this wave): menu-bar line via the B3 brain-lease pattern (`main+BrainLease.swift`
sibling): "Память: brain 19Г · whisper idle 4м · rewriter warm", refreshed in
`menuWillOpen`, no background polling. Hidden when `memory_conductor_enabled=false`.
v2 (follow-up brief to agy/Gemini): panel with usage graph; data via
`get_memory_ledger` only; styling-only brief, contract keys pinned by us.

## 7. Krab's half (cross-repo)

Brief sent via cross-session message + `.remember/` file: implement the same
LedgerClient contract (schema above), declare their LM Studio models under
`krab/` prefix, apply the same ladder to their residents. Until implemented we
run solo: their residents are simply absent from the picture; the existing
brain-lease gate already prevents us from pulling models out from under them.
Schema version field `v` allows evolution without lockstep deploys.

## 8. Failure directions

- Ledger unreadable/corrupt → conductor logs once, backs up the corrupt file, and
  this tick behaves as if empty; dictation pipeline NEVER touched by failures here.
- Conductor thread death must be visible: `thread_alive` + `last_tick_ts` in
  diagnostics (the sent=0/failed=0 lesson: liveness is a separate signal, and a
  never-ticking conductor must not look healthy).
- Pressure sysctl unavailable → treat as "no pressure" (fail toward keeping
  models warm, not toward eviction storms).
- Eviction is fire-and-forget with cooldown per resident (600 s, the OomAutoRelief
  pattern) — no retry loops.

## 9. Testing / rollout

TDD throughout. Unit: ledger RMW-delta under concurrent writers, TTL-gc, ladder
decisions per state matrix, never-evict-while-recording, foreign-lease block.
Contract: source-contract wiring test; ubuntu-parity (no mlx). Live: dev-instance
smoke — make whisper worker idle > threshold, verify terminate+respawn round-trip;
artificial pressure path exercised via injected pressure_fn (the emergency-
mechanism-zero-runs lesson: battle path must be executed at least once before
prod). Rollout: ship enabled with conservative defaults (600/1800 s); menu-bar
line lands same wave; agy panel + Krab brief after core merges.
