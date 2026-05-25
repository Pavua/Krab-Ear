# Wave 731 — Memory Budget for Sub-Agent Sessions

**Date:** 2026-05-26
**Branch:** wave731/memory-budget-doc
**Status:** Reference document — no code changes

---

## Live Memory Snapshot (2026-05-26)

Measured via `ps -axo rss,command | sort -rn | head -15`:

| Process | RSS (MB) | Notes |
|---------|----------|-------|
| `gigaam_worker.py` (Python 3.12) | ~1529 | GigaAM RNNT weights + MPS buffer pool — legit; one only |
| `OrbStack Helper vmgr` | ~1380 | Constant, not reducible |
| `WebKit WebContent XPC` | ~847 | Safari/browser pages |
| `Claude.app` (main) | ~769 | Claude Desktop UI |
| `rest_server.py` (Python 3.14) | ~750 | Krab Ear REST server |
| `Claude Helper (Renderer)` | ~663 | Claude Desktop renderer |
| `openclaw gateway (node)` | ~612 | OpenClaw gateway |
| `service.py` (Python 3.14) | ~497 | **Krab Ear IPC backend** |
| `claude` CLI sub-agent (×2) | ~470 + ~432 | Per-agent processes |
| `src.main` Krab (Python 3.13) | ~425 | Main Krab Telegram userbot |

**Total resident (top-10):** ~8.4 GB out of 36 GB physical RAM.

---

## Memory Hogs — Classification

### Fixed (cannot avoid)
- **GigaAM worker** (~1.5 GB): GigaAM RNNT weights (~500 MB) + PyTorch MPS buffer pool (~1 GB after warm-up). Legit, one instance. Duplicate was root cause of ~10 reboots during the mega-marathon (Wave 716). PR #619 ships permanent fix (singleton lock). Manual cron workaround: `kill_dup_gigaam.command` every 10 min.
- **OrbStack** (~1.4 GB): constant overhead, always running.

### Variable (reducible)
- **Krab Ear backend** (`service.py`): idle baseline 35–40 MB post-Wave 63 MLX leak fix (PR #405, `mx.clear_cache()` + `AudioLanguageID` LRU=1). Grows to 500 MB+ under load or after 12h without restart due to Python allocator fragmentation. **Restart every 6h** to recover.
- **Sub-agent `claude` CLI processes**: ~400–470 MB each. Hard floor from LLVM + MCP plugin loading. Grows with context length.
- **LM Studio** (not in current snapshot): idle ~200 MB; active inference ~2–4 GB MLX. Kill when not in active use.

---

## Sub-Agent Memory Budget

### Hard limits
- **Max 2 concurrent sub-agents.** At 400–470 MB each, 3+ agents push total above safe threshold given gigaam + OrbStack constant load.
- **No `pytest` runs in agent tasks.** A full test suite run (`pytest KrabEar/tests/`) spawns multiple workers and loads every module — peak RSS per pytest run is 800 MB–1.2 GB. Use targeted `unittest` on a single file if testing is required.
- **No `swift build` in agent tasks.** Swift compiler and linker peak at 2–3 GB RSS during full build. Agent tasks MUST NOT trigger `swift build` or `make build`.

### Allowed operations (low memory footprint)
- `read`, `grep`, `git`, `gh`, `find`, `ls` — negligible RSS
- Minimal file writes and edits
- Single-file `python -c` smoke checks
- `git log`, `git diff`, `git status`

### Per-agent budget
- **Target: ≤400 MB RSS per agent**
- Agents that import large Python modules (torch, mlx, transformers) will exceed this — reject such tasks from agent scope.

---

## Operating Procedure

| Action | Schedule | Command |
|--------|----------|---------|
| Kill duplicate gigaam worker | Every 10 min (cron) | `*/10 * * * * .../scripts/kill_dup_gigaam.command` |
| Restart Krab Ear backend | Every 6h (rolling) | `pkill -f "KrabEar/backend/service.py"` — launchd restarts automatically |
| Kill LM Studio when idle | Manual | `pkill -f "LM Studio"` |
| Check sub-agent count | Before spawning | `pgrep -c -f "claude --output-format"` — must be ≤2 |

---

## Cross-References

- Wave 716 gigaam dup root cause: `docs/USER_ACTION_CHECKLIST.md` (P0 section)
- Wave 63 MLX leak fix: PR #405, `docs/audit/gigaam-worker-memory-2026-05-05.md`
- Memory baseline Wave 195: `docs/memory-baseline-comparison-wave195.md` (35–40 MB stable vs 392 MB pre-fix)
- GigaAM worker architecture: `docs/audit/gigaam-worker-memory-2026-05-05.md`
