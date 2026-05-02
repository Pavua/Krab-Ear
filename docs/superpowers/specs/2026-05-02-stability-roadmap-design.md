# Stability Roadmap — Phase A/B/C Design

**Status**: Draft (awaiting user review)
**Author**: Claude (brainstorm session 2026-05-02)
**Co-author**: pavelr7@gmail.com
**Target**: Krab Ear daily-driver stability

---

## Context

После мега-сессии 2026-04-29..05-02 (LLM bench 60+ models, Tor/MCP setup, hot-swap GUI rewriter, brand regex 30+ patterns, adaptive length guard) транскрипция качественная и быстрая, но **стабильность отстаёт**. Юзер dictate'ит через сам Krab Ear ежедневно и встречает:

- "backend недоступен" toast — вынужденный manual restart
- Silent failures — paste не сработал, пользователь не понимает почему
- Memory leaks → 3 forced reboots за бенч-сессию
- TCC permissions drift после rebuild

User priority: **stability > quality > speed > workflow**.
Approach: incremental **A → B → C** (auto-heal first, loud errors second, root-cause third).

## Goals

- **Phase A**: backend сам поднимается за <3s, юзер видит toast, диктовка продолжается
- **Phase B**: каждое silent failure становится видимым с actionable button "Fix"
- **Phase C**: источники проблем устранены — A и B всё реже срабатывают со временем

## Non-goals

- Rewrite backend на Rust/Go
- Full Swift 6 strict concurrency
- Multi-user / multi-tenant
- Web/mobile дашборд (это workflow priority, не stability)

---

## Phase A — Auto-heal

### Architecture

Двухкольцевой supervisor:

- **Inner ring** (Swift `BackendSupervisor` + новый `HealthMonitor`) — health-check ping каждые 3s через IPC; 2 fail подряд → SIGTERM → 3s wait → SIGKILL → respawn. Покрывает "процесс жив но завис" (MLX deadlock, infinite loop)
- **Outer ring** (launchd KeepAlive=true via existing plist) — процесс полностью умер → launchd сам поднимает. Покрывает SIGSEGV / hard crashes

Belt + suspenders: оба кольца независимы.

### Components

| Component | Layer | New / Modified |
|---|---|---|
| `HealthMonitor` actor | Swift | New (`native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift`) |
| `BackendSupervisor` | Swift | Modified — добавить `healthMonitor`, restart с exp backoff |
| `StatusIndicatorView` | Swift | New — menu bar dot + History panel header dot |
| `handle_ping` IPC | Python | New method in `backend/service.py` |
| `health_metrics.py` | Python | New — RSS/uptime/active task counter (переиспользуется в Phase C) |

### IPC contract

```jsonc
// Request
{"id": "...", "method": "ping", "params": {}}

// Response
{"id": "...", "ok": true, "result": {
  "uptime_sec": 1234,
  "rss_mb": 410,
  "active_requests": 0,
  "version": "1.0.0"
}}
```

### Restart policy

- 1st attempt: immediate
- 2nd: 2s backoff
- 3rd: 5s backoff
- 4th: 15s backoff
- Max 5 attempts in 60s window
- After 5 fail → **circuit breaker open** → toast "Backend не запускается, открой логи" + button "Open log" → opens `~/Library/Logs/KrabEar/supervisor.log`
- Circuit breaker auto-resets after 5 min cooldown

### UI feedback

- **Toast** "Backend перезапущен" — non-modal, 3s auto-dismiss
- **Status dot** — green (OK) / yellow (restarting) / red (circuit broken)
- **Menu bar icon** colour matches status dot
- **History panel header** shows status + uptime tooltip

### Acceptance criteria

- `kill -9 <python-pid>` → toast appears within 3s → backend up within 3s → next dictation works ✅
- `time.sleep(60)` injected in handler → hang detected within 8s → kill + restart ✅
- 5 consecutive crashes → circuit broken, no further restart attempts, toast persists ✅

### Files

```
native/KrabEarAgent/Sources/KrabEarAgent/
  BackendSupervisor.swift          [modified]
  HealthMonitor.swift              [new]
  StatusIndicatorView.swift        [new]

KrabEar/backend/
  service.py                       [modified — handle_ping]
  health_metrics.py                [new]

KrabEar/tests/
  test_health_monitor.py           [new]
```

---

## Phase B — Loud errors

### Catalog of silent failures (9 classes)

| # | Class | Today | Should be |
|---|---|---|---|
| 1 | Paste fails (AX denied) | Silent, text in clipboard | Toast "Не смог вставить — Cmd+V" + "Open Privacy Settings" button |
| 2 | LLM rewriter timeout | Fallback to raw | Badge "rewriter недоступен, raw text" |
| 3 | STT model load fail | Cryptic error | Toast "GigaAM не загрузился, попробуй balanced" |
| 4 | Diarization fail | Single speaker output | Toast "Diarization недоступна (HF token?)" + "Указать токен" |
| 5 | Translation timeout | Returns original | Toast "Перевод недоступен, оригинал сохранён" |
| 6 | MLX OOM | SIGSEGV / hang | Detect SIGABRT/OOM signal → "Не хватило памяти, выгрузи LM Studio" |
| 7 | History save fail | Lost transcript | Critical alert + retry с tmpfile |
| 8 | Vocabulary load fail | STT works without bias | Warn в Settings UI |
| 9 | Hotkey conflict | Silent no-op | "Right Option занят другим приложением" |

### Architecture (3 layers)

#### Layer 1 — Structured Error Bus (Python)

Новый `KrabEar/backend/error_bus.py` — обёртка над существующим `event_bus.py`.

```python
from pydantic import BaseModel
from typing import Literal
from datetime import datetime

class KrabError(BaseModel):
    severity: Literal["info", "warn", "error", "critical"]
    component: str           # "stt", "rewriter", "paste", "diarization", "translation", "mlx", "history", "vocabulary", "hotkey"
    code: str                # registry key, see error_codes.py
    message_user: str        # ru, для UI
    message_debug: str       # en/ru, для лога
    timestamp: datetime
    context: dict            # arbitrary {"model": "...", "duration_ms": 15000}
    actionable: bool         # есть ли button "Fix" в UI
    action_id: str | None    # ID для handle_error_action IPC
```

Все service'ы пушат `KrabError` в bus вместо silent log. Bus → IPC event → Swift подписан через SSE-like stream.

#### Layer 2 — Error UI (Swift)

- **`ErrorToastView.swift`** — non-modal toast, severity-aware auto-dismiss:
  - `info` 2s, `warn` 5s, `error` 10s, `critical` manual dismiss only
- **Diagnostics tab** — новая **отдельная вкладка** в главном окне (не collapsible section!) с:
  - Severity filter (4 chips)
  - Component filter
  - Last 200 errors visible, full log on disk
  - "Copy to clipboard" / "Open in Console.app"
- **Status badges** scaling by severity:
  - `critical` → red modal alert (blocking)
  - `error` → orange toast + persistent menu bar badge
  - `warn` → yellow toast, no menu bar change
  - `info` → light fade-in/out

#### Layer 3 — Actionable buttons

Single source of truth `error_codes.py`:

```python
ERROR_REGISTRY = {
    "paste.ax_denied": {
        "user_msg_ru": "Не смог вставить — текст в clipboard, нажми Cmd+V",
        "actionable": True,
        "action_id": "open_privacy_settings",
        "severity": "error",
    },
    "diarization.no_token": {
        "user_msg_ru": "Diarization недоступна — нужен HF token",
        "actionable": True,
        "action_id": "open_hf_token_setting",
        "severity": "warn",
    },
    "rewriter.timeout": {
        "user_msg_ru": "Rewriter timeout — raw text использован",
        "actionable": True,
        "action_id": "disable_rewriter",
        "severity": "warn",
    },
    "mlx.oom": {
        "user_msg_ru": "Не хватило памяти — выгрузи LM Studio",
        "actionable": True,
        "action_id": "kill_lm_studio_via_telegram",
        "severity": "critical",
    },
    "hotkey.conflict": {
        "user_msg_ru": "Right Option занят другим приложением",
        "actionable": True,
        "action_id": "open_hotkey_settings",
        "severity": "warn",
    },
    # NOTE: registry строится инкрементально — каждый новый error code добавляется
    # с регистрацией в этом dict + регрессионным тестом. Initial seed = 9 codes
    # из catalog (paste.ax_denied, rewriter.timeout, stt.load_fail, diarization.no_token,
    # translation.timeout, mlx.oom, history.save_fail, vocabulary.load_fail, hotkey.conflict).
}
```

`handle_error_action` IPC method dispatches by `action_id`.

### Sentry/GlitchTip tier (per user agreement)

| Severity | Local log | Diagnostics tab | Sentry |
|---|---|---|---|
| `info` | ✅ | ✅ (filter) | ❌ (quota) |
| `warn` | ✅ | ✅ | ✅ batch by 10 |
| `error` | ✅ | ✅ default view | ✅ immediate |
| `critical` | ✅ | ✅ + modal | ✅ immediate + breadcrumbs |

Privacy: continuing rule — никогда не шлём transcript text в Sentry, только metadata.

### Acceptance criteria

- Revoke Accessibility → dictate → toast "paste failed, Cmd+V" + clipboard contains text + button "Open Privacy Settings" works ✅
- Disable internet → dictate → rewriter timeout toast → raw text pasted ✅
- Remove HF token → dictate → diarization warn toast + "Указать токен" button → opens settings ✅
- Diagnostics tab shows last 200 errors with filters working ✅

### Files

```
KrabEar/backend/
  error_bus.py                     [new]
  error_codes.py                   [new — registry]
  llm_rewriter.py                  [modified — replace logger.warning with error_bus.push]
  transcriber.py                   [modified — push errors on model load fail, OOM]
  service.py                       [modified — handle_error_action]

native/KrabEarAgent/Sources/KrabEarAgent/
  ErrorToastView.swift             [new]
  DiagnosticsTabView.swift         [new]
  ErrorActionHandler.swift         [new]
  HistoryPanelController+Diagnostics.swift  [new extension]

KrabEar/tests/
  test_error_bus.py                [new]
  test_error_codes.py              [new]
```

---

## Phase C — Root cause

### Принцип

Phase A прячет симптомы, Phase B показывает симптомы, **Phase C убирает симптомы целиком**. Marathon, не sprint. 6 направлений с приоритизацией.

### C.1 — Memory leaks 🔴 highest priority

**Symptom**: 3 reboots during bench session, MLX GPU stuck

**Hypotheses**:
- `mlx_whisper.transcribe` не освобождает intermediate buffers
- LM Studio + Krab Ear MLX inference одновременно = pressure
- pyannote audio buffers держат references

**Plan**:
1. **Tiered soak tests**:
   - 2h smoke (initial check)
   - 4h дневной (background while user works)
   - 24h nightly (while user sleeps)
   - 1 dictation/min, log RSS/VRAM каждую минуту → graph
2. Если RSS растёт линейно — `tracemalloc` snapshots каждые 100 итераций → identify leak
3. Если VRAM растёт — добавить `mlx.core.metal.clear_cache()` after every `transcribe()` (known workaround)
4. RSS watermark в `health_metrics.py` (из Phase A): >6 GB warn, >10 GB trigger Phase A restart

**Files**:
```
KrabEar/scripts/memory_soak_test.py             [new]
KrabEar/core/engine.py                          [modified — clear_cache after transcribe]
KrabEar/backend/health_metrics.py               [modified — watermark + auto-restart]
docs/memory_profiling_results.md                [new — soak test results]
```

### C.3 — MLX lock audit 🟡 medium priority

**Symptom**: SIGSEGV in `__hash_table<MTL::Resource*>` (per CLAUDE.md, PR #71 partial fix)

**Plan**:
1. `grep -rn "mlx_whisper\|mlx.core" KrabEar/` → audit ALL touchpoints
2. Verify each is wrapped in `with mlx_lock():`
3. Special attention: profile switch (balanced↔max) — model reload без lock
4. Add `mlx_lock_observer` opt-in mode → log enter/exit critical section
5. Stress test: 10 parallel transcriptions via ThreadPoolExecutor

**Files**:
```
KrabEar/core/mlx_lock.py                        [modified — observer mode]
KrabEar/tests/test_mlx_concurrency.py           [new]
```

### C.2 — IPC reconnect protocol 🟡 medium priority

**Symptom**: backend restart → Swift "недоступен" until manual hotkey

**Plan**:
- Swift: socket disconnect → auto-reconnect with exp backoff (1s, 2s, 4s, max 16s)
- Server: on `accept()` send heartbeat-handshake with protocol version
- Buffer in-flight requests: IPC torn mid-dictation → Swift locally buffers → retry after reconnect
- Boundary: 30s timeout total, then loud error "IPC permanently lost"

**Files**:
```
native/KrabEarAgent/Sources/KrabEarAgent/IPCClient.swift     [modified]
native/KrabEarAgent/Sources/KrabEarAgent/IPCReconnectStrategy.swift  [new]
KrabEar/backend/service.py                                   [modified — _handle_handshake]
KrabEar/tests/test_ipc_reconnect.py                          [new]
```

### C.5 — Structured concurrency Swift 🟢 low priority

**Symptom**: AGENT-3 hangs (per core memory) — 25-30 `@objc` funcs with sync `ipcClient.call` on main thread

**Plan**:
1. Audit ALL `@objc` methods in 12 `HistoryPanelController+*.swift` extensions
2. Convert sync IPC → `async/await` with MainActor isolation
3. Pattern reference: `+Analytics.swift` (already migrated)
4. Acceptance: AppHang Sentry events drop to 0

**Files**:
```
native/KrabEarAgent/Sources/KrabEarAgent/IPCClient+Async.swift  [new]
native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+*.swift  [12 modified]
```

### C.4 — Brand regex + dedup edge cases 🟢 low priority (quick wins)

**Symptom**: real dictation samples от user'а ("buy ловить байк" вместо "баги ловить баги", "0лспользовать" вместо "использовать", "разместить разместить" repeats)

**Plan**:
1. Build **regression test corpus**: `KrabEar/tests/data/dictation_artefacts.txt`
   - Format: `raw_text\texpected_text` per line
   - Seed with 50+ samples from current session + production logs
2. Each future fix → add regression test (mandatory)
3. Expand `_WORD_REPEAT_RE`: drop minimum word length, add case-insensitive
4. Multi-pass cleanup: 3 passes instead of 1 (5x→2x→1x → verified clean)
5. Test: `pytest KrabEar/tests/test_text_artefacts.py::test_regression_corpus`

**Files**:
```
KrabEar/tests/data/dictation_artefacts.txt      [new — 50+ samples]
KrabEar/core/utils.py                           [refactor — multi-pass cleanup]
KrabEar/tests/test_text_artefacts.py            [new — regression suite]
```

### C.6 — Single-instance + hotkey bulletproof 🟢 low priority

**Symptom**: occasionally 2 KrabEarAgent processes running simultaneously

**Plan**:
- `SingleInstanceGuard.swift` — replace PID check with **POSIX file lock** on `~/Library/Application Support/KrabEar/agent.lock` (`flock` syscall)
- `HotkeyManager.swift` — detect `eventHotKeyExistsErr` from `RegisterEventHotKey` → push `hotkey.conflict` error to bus

**Files**:
```
native/KrabEarAgent/Sources/KrabEarAgent/SingleInstanceGuard.swift  [modified]
native/KrabEarAgent/Sources/KrabEarAgent/HotkeyManager.swift        [modified]
```

### Phase C order

**C.1 → C.3 → C.2 → C.5 → C.4 → C.6**

Foundation first (memory + concurrency), polish last (regex + UX).

### Acceptance criteria for Phase C

- 24h soak test passes без OOM, RSS drift < 200 MB ✅
- Kill backend mid-request → auto-reconnect → retry succeeds without user action ✅
- 10× parallel transcriptions → 0 SIGSEGV ✅
- Regression corpus 50+ cases → 100% pass ✅
- AppHang Sentry events = 0 за неделю ✅
- 2 параллельных запуска агента невозможны ✅

---

## Cross-cutting concerns

### Testing strategy

- **Phase A**: kill backend / inject hangs / verify restart timing — unit + integration
- **Phase B**: revoke permissions / disable internet / remove tokens — manual + integration tests
- **Phase C**: soak tests (long-running) + stress tests (parallel) + regression corpus

CI matrix:
- backend-tests (existing, ~6500 tests)
- + new health_monitor + error_bus + mlx_concurrency + text_artefacts
- Performance regression gate (existing PR #237/#242) — extended to memory

### Sentry tags

All Phase B+C errors → tagged with `phase` (a/b/c) and `code` for grouping in Sentry dashboard.

### Rollout

Incremental:
1. Phase A merge → 1 week observation period
2. Phase B merge → 1 week observation
3. Phase C subdirections in parallel sub-agents (C.1+C.4 can run together, C.5+C.6 in another batch)

### Backward compatibility

- All new IPC methods (`ping`, `handle_error_action`, `_handle_handshake`) — additive, не breaking
- Error events на existing event bus → existing consumers ignore unknown event types
- Settings: добавляем `enable_diagnostics_tab: bool = True`, `error_severity_threshold: str = "info"` — defaults preserve current behaviour

---

## Risks

| Risk | Mitigation |
|---|---|
| Health-check ping IPC overhead | 3s interval = ~5ms latency / 3000ms = 0.16% — negligible |
| Restart loop on persistent bug | Circuit breaker after 5 fails — limits damage |
| Toast fatigue from too many errors | Severity-aware UI + Diagnostics tab consolidation |
| Sentry quota exhaustion | Tier system: warn batched, info local-only |
| 24h soak test blocks user macbook | Tiered approach (2h/4h day, 24h night) |
| MLX clear_cache breaks something | A/B test with feature flag `mlx_aggressive_cleanup` |
| Phase C scope creep | Strict 6 sub-direction list, anything new = separate spec |

---

## Open questions

1. **Telegram bridge for `kill_lm_studio_via_telegram` action** — does main Krab support kill-process API? → research before implementing C.6
2. **launchd plist** — currently optional; should we install автоматически on first run? → user pref question
3. **Sentry release tagging** — already exists per PR #241, verify it covers Phase A/B/C events

---

## Next steps

After user reviews this spec:
1. Invoke `writing-plans` skill to create detailed implementation plan
2. Plan splits into PRs per phase / per direction
3. Implementation in parallel sub-agents where direction-isolated

---

*End of design.*
