# P0d EventBridge token reload after dual restart

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** парный `kickstart` backend+REST больше не оставляет `/internal/event` на 401 из‑за протухшего bridge-токена.

**Architecture:** REST кэширует токен навсегда после первого успешного чтения; IPC EventBridge держит токен в RAM с `start()`. После `--with-rest` один процесс видит файл, другой — старый кэш → 12 мин 401, SSE молчит. REST на mismatch один раз сбрасывает кэш и читает файл; EventBridge после неуспешного POST перечитывает файл и повторяет один раз, только если токен сменился. `safe_backend_restart --with-rest` поднимает REST **после** IPC ping, не параллельно с backend.

**Tech Stack:** уже в репо (`event_bridge.py`, `rest_server.py`, `scripts/safe_backend_restart.command`, unittest). Без новых зависимостей.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/p0d-event-bridge-token`.

**Баны:** база только `origin/codex/krab-ear-v2`; `git add` явными путями; не запускать `KrabEarAgent`; не `kickstart -k` под запись; не мержить PR #1875; не `REST_IN_PROCESS_ENABLED`; не коммитить `wake_word_models/hard_negatives_raw/`; не трогать Main Krab / VG `.env`; не второй EventBridge; не печатать токен.

**Вне скоупа:** `/v1/stream` singleflight; plist reinstall; live STT smoke с turbo.

---

### Task 1: REST re-read on mismatch

**Files:**
- Modify: `KrabEar/backend/rest_server.py` (`_require_loopback_and_bridge_token`)
- Test: `KrabEar/tests/test_rest_internal_event.py`

- [ ] **Step 1: Write the failing test** — stale cache + file token = supplied → 200; attacker token after re-read → 401.
- [ ] **Step 2: RED**
- [ ] **Step 3: clear `_event_bridge_token_cache` on mismatch, re-read once, compare again.**
- [ ] **Step 4: GREEN**

### Task 2: EventBridge reload token on failed POST

**Files:**
- Modify: `KrabEar/backend/event_bridge.py` (`_drain_and_send`)
- Test: `KrabEar/tests/test_event_bridge.py`

- [ ] Disk token changed → retry once with new token, state `up`.
- [ ] Disk token unchanged → no second POST (connection-refused не дублировать).

### Task 3: safe restart orders REST after backend ping

**Files:**
- Modify: `scripts/safe_backend_restart.command`
- Test: `KrabEar/tests/test_safe_backend_restart_contract.py`

- [ ] `--with-rest` при idle: launchctl backend, затем ping, затем REST. Не наоборот.
