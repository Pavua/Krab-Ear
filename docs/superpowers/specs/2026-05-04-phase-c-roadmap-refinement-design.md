# Phase C — Root Cause Roadmap Refinement (post-B.1+B.2)

**Status:** design refinement, supersedes parent spec's Phase C section (`2026-05-02-stability-roadmap-design.md` lines 256-389) с учётом empirical data из B.1+B.2 sessions.
**Date:** 2026-05-04
**Branch base:** `codex/krab-ear-v2` (post-merge of PR #363 + PR #364)
**Author:** Pavel + Claude

---

## Empirical findings driving priority refinement

Five concrete observations from B.1+B.2 implementation:

### Finding 1 — 4 system reboots in single session under MLX memory pressure
- **Trigger:** `LLMHttpProbe` POST `/v1/chat/completions` каждые 30s + LM Studio idle-eviction → JIT reload churn (6.86 GB gemma reload from external SSD per cycle)
- **Concurrent factor:** Whisper-large-v3 MLX in Krab Ear backend (~3 GB) actively used via dictation + Claude Code session (1M context, ~6-8 GB) + LM Studio gemma loaded
- **Resolved by:** F2 probe HEAD migration (passive `/v1/models` no longer triggers JIT)
- **Phase C implication:** **C.1 Memory leaks** confirmed as 🔴 critical, but the surface area is **process coordination**, not just leaks. Real leaks may be smaller than the *coordination mistakes*.

### Finding 2 — Two-binary drift recurring 5+ times in single session
- **Trigger:** subagents с `isolation: "worktree"` создают `Krab Ear.app/` копии в worktree, macOS LaunchServices индексирует их → `open` resolves to wrong path
- **Resolved by:** F1 `cleanup_worktree_shadows.command` script (manual run)
- **Phase C implication:** **C.6 single-instance bulletproof** promote из 🟢 low priority до 🟡 medium. Add automated cleanup hook (e.g. on launchd-managed startup or via SingleInstanceGuard).

### Finding 3 — Live Subs silent crash без `.ips` reports
- **Trigger:** SystemAudioCapture starts → `[NSApp terminate:]` fires from unknown source ~30-90s later
- **Mitigation:** F4 Sentry DSN active — next repro produces breadcrumbs
- **Phase C implication:** **NEW direction C.7 — Observability completeness**. Currently we know app terminated but not why. Need:
  - Sentry breadcrumbs at all `NSApp.terminate` sites + `applicationShouldTerminate`
  - sigaction hook for SIGABRT/SIGKILL → write `.ips`-equivalent to local logs
  - distributed notification audit (Track who posts «quit» / «stop» actions)

### Finding 4 — Phase A latent gaps closed retroactively
- **Discovery:** `StatusIndicatorView.swift`, `HealthMonitor.swift`, `BackendToast.swift` were described in CLAUDE.md as Phase A shipped, но never landed на `codex/krab-ear-v2`. Tasks 12+13 of B.1 created them.
- **Phase C implication:** **NEW direction C.8 — Documentation drift audit**. CLAUDE.md descriptions vs actual codebase need automated check. Possibly via `make verify-claude-md` script.

### Finding 5 — Whisper hallucination loops («Атакса хвостимда», «согласен да согласен да...»)
- **Trigger:** Whisper architecture: на коротких / silent segments или repeated audio модель entera repetition loop
- **Phase C implication:** New error code candidate `stt.repetition_loop` — heuristic detect (≥5 repeated bigrams in output) → reject + fallback. Promote priority since user-visible quality issue. Add to **C.4 STT quality edge cases** (was «Brand regex + dedup» — broader scope now).

---

## Refined Phase C direction order

| Priority | Direction | Notes |
|---|---|---|
| 🔴 C.1 | Memory profiling + leak fixes | 4 reboots — most painful issue. Use `tracemalloc` + `pympler` + macOS `vmmap` for snapshots. |
| 🔴 C.7 | Observability completeness (NEW) | Live Subs silent crash blind spot. Hook all terminate sites. Sigaction handler. Distributed notification audit. |
| 🟡 C.3 | MLX lock audit | Concurrent MLX inference triggers Metal GPU stuck (memory `feedback_mlx_memory_constraint.md`). All MLX call sites must use `mlx_lock` (RLock). |
| 🟡 C.6 | Single-instance bulletproof | Two-binary drift recurring. Promote priority. Automated cleanup. |
| 🟡 C.2 | IPC reconnect protocol | Silent IPC breakage today during HealthMonitor restart cascade. Need handshake + auto-reconnect from Swift. |
| 🟢 C.4 | STT quality edge cases | Whisper repetition loops + brand mishears + dedup. Quick wins via post-processing rules. |
| 🟢 C.8 | Documentation drift (NEW) | CLAUDE.md vs codebase verifier. |
| 🟢 C.5 | Structured concurrency Swift | Swift 6 strict concurrency cleanups. Long tail. |

---

## C.1 — Memory profiling + leak fixes (🔴)

### Acceptance criteria
1. **Baseline measurement** — record RSS/VSZ of Krab Ear backend after fresh boot, after 1h idle, after 100 dictation cycles. Baseline → CSV in repo `docs/measurements/memory-baseline-2026-05-04.csv`.
2. **Top-3 leak suspects identified** via `tracemalloc.start()` + snapshots at 5min intervals. Suspects committed as inline TODO comments or filed as separate issues.
3. **Top-1 leak fixed** with regression test that asserts RSS doesn't grow >10MB over 1000 cycles of the leak-triggering operation.
4. **Probe HEAD migration validation** (already done in F2) — verified probe doesn't accumulate memory over 24h soak test.

### Files / deliverables
- `KrabEar/scripts/memory_baseline.py` — script that pings backend, records RSS via `psutil.Process(pid).memory_info()`, dumps to CSV
- `KrabEar/scripts/memory_soak_test.command` — runs 100 dictate cycles via IPC, captures RSS deltas
- `docs/measurements/memory-baseline-2026-05-04.csv`
- 1+ leak fix commit + regression test

### Out of scope
- Whisper model RAM (it's a fact of life; we accept ~3GB MLX)
- LM Studio gemma (separate process)

---

## C.7 — Observability completeness (🔴 NEW)

### Acceptance criteria
1. **All NSApp.terminate sites Sentry breadcrumbed** — main.swift has 5 sites (per audit done today). Each gains `Sentry.addBreadcrumb(category: "lifecycle", message: "terminate from \(callsite)")` BEFORE the actual terminate call.
2. **Sigaction handler installed** — backend Python sets `signal.signal(SIGABRT, _sentry_capture_then_die)` and equivalent for SIGSEGV / SIGTERM. Captures last frame to Sentry before propagating signal. Pattern from `backend/observability.py`.
3. **Distributed notification audit committed** — `docs/audit/distributed-notifications-2026-05-04.md` listing all `DistributedNotificationCenter.default().postNotificationName(...)` callers + handlers + payload shape. Identifies any "quit" / "stop_agent" notifiers.
4. **Live Subs repro yields actionable Sentry trace** — after deploying C.7, next Live Subs repro should show last 20 breadcrumbs leading to terminate, including which subsystem triggered it.

### Files / deliverables
- `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` — breadcrumb at each terminate site
- `KrabEar/backend/observability.py` — sigaction handler installation
- `docs/audit/distributed-notifications-2026-05-04.md`

---

## C.3 — MLX lock audit (🟡)

### Acceptance criteria
1. **All MLX call sites listed** in `docs/audit/mlx-call-sites-2026-05-04.md`. Categorized by:
   - In `mlx_lock()`: ✅ safe
   - Without lock: 🔴 must wrap
   - In subprocess (own GPU context): ⚠️ separate process, lock not needed but document
2. **All unwrapped sites wrapped** — add `with mlx_lock(): ...` around each. Regression test: parallel-call test from N threads asserts no SIGSEGV.
3. **Documentation in CLAUDE.md** updated with audit reference.

### Files
- `docs/audit/mlx-call-sites-2026-05-04.md`
- `KrabEar/core/engine.py`, `KrabEar/backend/llm_rewriter.py`, others (TBD by audit)
- `KrabEar/tests/test_mlx_concurrency.py` (extend existing or new)

---

## C.6 — Single-instance bulletproof + drift prevention (🟡)

### Acceptance criteria
1. **Cleanup-on-startup automated** — `SingleInstanceGuard.swift` calls `cleanup_worktree_shadows.command` (or its Swift equivalent inline) при first launch detection. No more manual runs needed.
2. **Build script invariant**: any `swift build` to `.build/release/KrabEarAgent` automatically `cp` to BOTH `native/runtime/KrabEarAgent` AND `Krab Ear.app/Contents/MacOS/KrabEarAgent` AND signs both with `--identifier`. Implemented in `Makefile` `make sign` target (verify it does this; if not, fix).
3. **`launchctl` plist canonicalization** — single canonical plist `ai.krab.ear.agent.plist` pointing to bundle path. `install_agent.command` deprecated (already noted in script header). Provide migration script.
4. **POSIX file lock** in addition to PID-based guard (memory `blocker_two_binary_drift_2026-05-03.md` mentions). `flock()` on `~/Library/Application Support/KrabEar/agent.lock`.

### Files
- `native/KrabEarAgent/Sources/KrabEarAgent/SingleInstanceGuard.swift` — extend
- `Makefile` — verify sign target
- `scripts/cleanup_worktree_shadows.command` (already shipped in B.2 F1)
- `scripts/migrate_to_canonical_launchagent.command` (new)

---

## C.2 — IPC reconnect protocol (🟡)

### Acceptance criteria
1. **Swift IPCClient gains exponential-backoff reconnect** when socket disconnects mid-call. Max 5 retries (250ms, 500ms, 1s, 2s, 4s), then surface error.
2. **Backend handle_handshake IPC** — Swift sends version + capabilities on connect; backend rejects mismatched versions.
3. **Reconnect telemetry** — IPC reconnect events emit `ipc.reconnect` info-severity (new error code).

### Files
- `native/KrabEarAgent/Sources/KrabEarAgent/IPCClient.swift`
- `KrabEar/backend/service.py` — handshake handler
- `KrabEar/backend/error_codes.py` — `ipc.reconnect` info code

---

## C.4 — STT quality edge cases (🟢 quick wins)

### Acceptance criteria
1. **Repetition loop detector** — heuristic `is_likely_repetition_loop(text)`: True if ≥5 repeated bigrams OR ≥3 identical sentences in row. Reject + fallback to previous successful pass OR raw text.
2. **Brand regex expansion** — add `gemma`, `Krab Ear`, `MLX` to existing brand normalization list (memory note 2026-04-26 already partial).
3. **Dedup edge case fixes** — see existing `text_dedup.py` and `dedup_re_articulation` test cases. Whatever's reported but unfixed.

### Files
- `KrabEar/core/text_postprocessor.py` (or wherever repetition detection lives)
- `KrabEar/core/text_dedup.py`
- `KrabEar/tests/test_text_quality.py` extended

---

## C.8 — Documentation drift verifier (🟢 NEW)

### Acceptance criteria
1. **`scripts/verify_claude_md.py`** — parses CLAUDE.md sections describing files (e.g. `**`StatusIndicatorView.swift`** — ...`) and verifies each named file exists at the implied path. Reports missing/extra. Exit code 1 on drift.
2. **CI integration** — `.github/workflows/ci.yml` runs the verifier on PRs touching CLAUDE.md.
3. **Initial drift report** — run verifier on current `codex/krab-ear-v2`, fix gaps OR document acceptable lag.

### Files
- `scripts/verify_claude_md.py`
- `.github/workflows/ci.yml` extension
- Possibly CLAUDE.md fixes

---

## C.5 — Structured concurrency Swift (🟢)

### Acceptance criteria
1. **All `Task { }` → `Task.detached { }` audit** — find unstructured tasks that should be cancelled on owner deinit. Add cancellation registration.
2. **Sendable warnings → 0** in `swift build -c release`. Currently 2 warnings in `main+LiveSubs.swift` (UnsafeRawPointer to inout String). Fix or suppress with explicit `withUnsafePointer`.
3. **Actor isolation review** — HistoryPanelController (large file) — verify all UI mutations are MainActor-isolated.

### Files
- Audit doc: `docs/audit/swift-concurrency-2026-05-04.md`
- Multiple Swift file edits TBD

---

## Sequencing

**Phase C.1 + C.7** in parallel — both 🔴 critical, file-isolated (Python memory script vs Swift breadcrumbs).
**Phase C.3** after C.1 (uses similar profiling tooling).
**Phase C.6** when next two-binary drift recurs (or proactively once).
**Phase C.2 / C.4 / C.5 / C.8** opportunistic — when touching adjacent code.

---

## Total estimated effort

| Direction | Effort |
|---|---|
| C.1 Memory profiling | 1-2 days research + 1 day fix |
| C.7 Observability | 1 day Swift + 0.5 day Python |
| C.3 MLX lock audit | 0.5 day audit + 0.5 day fix |
| C.6 Single-instance | 1 day implementation |
| C.2 IPC reconnect | 1 day implementation |
| C.4 STT quality | 0.5 day per fix x N |
| C.8 Doc verifier | 0.5 day script |
| C.5 Concurrency | opportunistic |
| **Total** | **~5-7 working days** |

---

## Next steps after this spec

1. **User reviews** — does priority order match expectations?
2. **Brainstorm one direction at a time** through `superpowers:brainstorming` skill (similar to B.1)
3. **Per-direction plan via `superpowers:writing-plans`**
4. **Execute via `superpowers:subagent-driven-development`** (proven pattern from B.1+B.2)

---

*End of Phase C refinement design.*
