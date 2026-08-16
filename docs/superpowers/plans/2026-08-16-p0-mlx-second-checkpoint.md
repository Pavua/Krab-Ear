# P0a MLX second-checkpoint + REST singleflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SEGV `whisper-large-v3-turbo` в `ai.krab.ear.rest` не должен повторяться из-за загрузки второго MLX-чекпоинта под vm_pressure и из-за параллельных `/v1/stt/transcribe`.

**Architecture:** Каскад `_maybe_multipass_retry` грузит `whisper-large-v3-mlx` после `turbo` при confidence &lt; 0.65. Под `kern.memorystatus_vm_pressure_level >= 1` локальные MLX-retry пропускаются, первый текст остаётся. REST берёт process-wide lock на STT. Worker-процесс для mlx_whisper — **не эта карточка** (`mlx_subprocess` = in-process watchdog, не изоляция PID).

**Tech Stack:** существующие `core/engine.py`, `backend/rest_server.py`, unittest, без новых зависимостей.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/p0-mlx-pressure-gate`.

**Баны:** база только `origin/codex/krab-ear-v2`; `git add` явными путями; не запускать `KrabEarAgent`; не `kickstart -k`; не мержить PR #1875; не `REST_IN_PROCESS_ENABLED`; не коммитить `wake_word_models/hard_negatives_raw/`; не трогать Main Krab / VG `.env`; не stash/reset чужой WIP в общем чекауте.

**Вне скоупа (P0c, отдельная карточка):** OS-subprocess для mlx_whisper. Не включать `KRAB_EAR_MLX_INTER_PROCESS_LOCK` как замену — LM Studio этот flock не знает.

---

### Task 1: Гейт второго MLX-чекпоинта

**Files:**
- Create: `KrabEar/core/mlx_memory_gate.py`
- Modify: `KrabEar/core/engine.py` (`_maybe_multipass_retry`)
- Modify: `KrabEar/tests/test_engine_multipass.py` (setUpModule: гейт = False, иначе macOS warn ломает старые retry-тесты)
- Test: `KrabEar/tests/test_mlx_second_checkpoint_gate_2026_08_16.py`

- [ ] **Step 1: Write the failing test**

Полный тест в `KrabEar/tests/test_mlx_second_checkpoint_gate_2026_08_16.py` (см. реализацию в сессии). Кейсы:

1. `should_skip_second_mlx_checkpoint()` True при `vm_pressure_level() >= 1`
2. False при `None` (Linux/CI) и при `0`
3. `_maybe_multipass_retry` при low conf + skip=True **не** зовёт `_transcribe_model`, пишет `skipped: vm_pressure`
4. skip=False + другой max-model — retry как раньше
5. кандидат с тем же `model_used`, что первый pass — `_transcribe_model` не зовётся (turbo→turbo)

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_mlx_second_checkpoint_gate_2026_08_16.py -v`

Expected: FAIL — нет `core.mlx_memory_gate` / нет skip-ветки

- [ ] **Step 3: Write minimal implementation**

`vm_pressure_level()` читает `sysctl -n kern.memorystatus_vm_pressure_level` (timeout 1с). Исключение / пусто / не-int → `None`. `should_skip_second_mlx_checkpoint()`: env `KRAB_EAR_STT_SKIP_SECOND_MLX=1` → True; `=0` → False; иначе `level is not None and level >= 1`.

В `_maybe_multipass_retry`: перед `_transcribe_model` пропустить candidate `kind==model`, если имя совпадает с `first_result["model_used"]` **или** `should_skip_second_mlx_checkpoint()`. Remote не трогать.

- [ ] **Step 4: Run test to verify it passes**

Та же команда + `KrabEar/tests/test_engine_multipass.py`

Expected: PASS

- [ ] **Step 5: Commit** после GREEN Task 1+2 (явные пути).

---

### Task 2: REST STT singleflight

**Files:**
- Modify: `KrabEar/backend/rest_server.py` (`transcribe_audio`)
- Test: `KrabEar/tests/test_rest_stt_singleflight_2026_08_16.py`

- [ ] **Step 1: Write the failing test**

Два параллельных POST `/v1/stt/transcribe` с mock, который спит 0.4с и считает `in_flight`. Assert `max_in_flight == 1`. Второй запрос, который не взял lock за `deadline_sec=5` пока первый держит lock — 503 `stt_busy`, без второго `transcribe()`.

- [ ] **Step 2: Run to verify FAIL**

Expected: `max_in_flight == 2` (сейчас каждый запрос свой ThreadPoolExecutor)

- [ ] **Step 3: Minimal lock**

Модульный `threading.Lock()`. `acquire(timeout=deadline)` до submit. Не взяли → 503 `{"error":"stt_busy"}`. Release в `finally` после `result()`/timeout-ветки, **до** `_exit_poisoned_rest_process` если тот зовётся (poisoned lock всё равно умрёт с процессом).

- [ ] **Step 4: PASS + `scripts/pre_merge_py312_check.sh` на новые test-файлы**

- [ ] **Step 5: Commit** явными путями.
