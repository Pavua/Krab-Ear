# P0e `/v1/stream` REST STT singleflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** native `/v1/stream` не грузит MLX параллельно с POST `/v1/stt/transcribe` в том же REST PID.

**Architecture:** `LiveSubsService.ingest()` STT не делает (F3: снапшот + слот-1). Гейт только вокруг `self._transcriber.transcribe` в `_process_window`. Optional `stt_acquire`/`stt_release` (default None) — REST WS передаёт `try_acquire_stt_singleflight` / `release_stt_singleflight`; IPC `BackendService` не передаёт. Live subs: `acquire(0)` — занято POST → дроп окна, не ждать `deadline_sec`. Лок не держать на WS-соединение, resample, translate, emit. Cloud `/v1/stream` не трогать.

**Tech Stack:** уже в репо (`live_subs_service.py`, `rest_server.py` `_STT_SINGLEFLIGHT`, unittest). Без новых зависимостей.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/p0e-stream-singleflight`.

**Баны:** база только `origin/codex/krab-ear-v2`; `git add` явными путями; не запускать `KrabEarAgent`; не `kickstart -k` под запись; не мержить PR #1875; не `REST_IN_PROCESS_ENABLED`; не второй EventBridge; не wake-word SSE; не трогать `service.py`/`engine.py`; не коммитить `wake_word_models/hard_negatives_raw/` и `docs/HANDOFF_WHISPER_TURBO_SEGV_2026-08-16_RU.md`.

**Вне скоупа:** cloud backend `/v1/stream`; POST `deadline_sec`; IPC LiveSubsService gate; живой REST restart (не обязателен).

---

### Task 1: RED — LiveSubsService acquire/release вокруг transcribe

**Files:**
- Create: `KrabEar/tests/test_live_subs_stt_singleflight_p0e_2026_08_17.py`
- Modify: `KrabEar/backend/live_subs_service.py` (`__init__`, `_process_window`)
- Modify: `KrabEar/backend/rest_server.py` (`_ws_stream_handler` LiveSubsService(...))

- [x] **Step 1: Write the failing tests**

(a) `stt_acquire` → False: `transcribe` не зовётся, `stt_release` не зовётся, text пустой.
(b) `stt_acquire` → True: `transcribe` зовётся с timeout 0; `stt_release` в `finally` даже если transcribe бросает.
(c) AST: REST `_ws_stream_handler` передаёт `stt_acquire`/`stt_release`; `BackendService` — нет.
Существующие live-subs тесты без kwargs остаются зелёными (default None).

- [x] **Step 2: RED**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_live_subs_stt_singleflight_p0e_2026_08_17.py -v`
Expected: FAIL (`TypeError` unexpected kwargs / assert kwargs отсутствуют)

- [x] **Step 3: GREEN — optional kwargs + wrap transcribe + REST WS wiring**

`LiveSubsService.__init__(..., stt_acquire=None, stt_release=None)`.
В `_process_window` сразу перед `transcribe`: `acquire(0.0)`; False → дроп (пустой result, `dropped_windows++`); True → `try: transcribe finally: release`.
REST: `stt_acquire=try_acquire_stt_singleflight`, `stt_release=release_stt_singleflight`. POST путь не менять.

- [x] **Step 4: GREEN**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_live_subs_stt_singleflight_p0e_2026_08_17.py KrabEar/tests/test_live_subs_backpressure_F3_2026_08_12.py KrabEar/tests/test_live_subs_service.py KrabEar/tests/test_rest_v1_stream.py KrabEar/tests/test_rest_stt_singleflight_2026_08_16.py -v`
Expected: PASS

- [x] **Step 5: ubuntu-parity + flake8 + NOW/ROADMAP**

Run: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_live_subs_stt_singleflight_p0e_2026_08_17.py`
Run: flake8 CI-флагами по изменённым py (W293 в тестах не расслаблен).
Обновить `docs/NOW.md` (HEAD = `afc30fe8`, P0e закрыта) и журнал `docs/ROADMAP-2026H2.md`.

- [x] **Step 6: Commit явными путями**

```bash
git add docs/superpowers/plans/2026-08-17-p0e-stream-stt-singleflight.md \
  KrabEar/tests/test_live_subs_stt_singleflight_p0e_2026_08_17.py \
  KrabEar/backend/live_subs_service.py \
  KrabEar/backend/rest_server.py \
  docs/NOW.md docs/ROADMAP-2026H2.md
git commit -m "fix(stt): /v1/stream native STT под REST singleflight"
```
