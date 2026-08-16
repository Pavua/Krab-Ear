# P0c MLX Whisper OS-worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SEGV в `mlx_whisper.transcribe` убивает только worker-PID, а не `ai.krab.ear.rest`.

**Architecture:** Тот же JSON-line протокол, что у GigaAM (`stdin`/`stdout`, stderr drain, Timer-kill). Воркер — тот же интерпретатор, что у родителя (mlx-whisper в основном venv, не GigaAM-venv). Родитель пишет temp WAV, не гоняет numpy по stdin. `core/mlx_subprocess.py` остаётся in-process watchdog для пути без воркера.

**Tech Stack:** `subprocess.Popen`, существующий `mlx_whisper`, unittest. Без новых зависимостей.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/p0c-mlx-whisper-worker`.

**Баны:** база только `origin/codex/krab-ear-v2`; `git add` явными путями; не запускать `KrabEarAgent`; не `kickstart -k`; не мержить PR #1875; не `REST_IN_PROCESS_ENABLED`; не коммитить `wake_word_models/hard_negatives_raw/`; не трогать Main Krab / VG `.env`; не stash/reset чужой WIP в общем чекауте; не `signal.signal` на SIGSEGV; не включать `KRAB_EAR_MLX_INTER_PROCESS_LOCK` как замену изоляции.

**Вне скоупа:** live smoke на прод-REST (только если владелец явно попросит + `safe_backend_restart.command --with-rest` вне звонка). IPC-backend воркер не включаем по умолчанию (ежедневная диктовка остаётся in-process).

---

### Task 1: Флаг включения + сессия + crash protocol

**Files:**
- Create: `KrabEar/core/mlx_whisper_session.py`
- Create: `KrabEar/core/workers/mlx_whisper_worker.py`
- Test: `KrabEar/tests/test_mlx_whisper_worker_2026_08_16.py`

- [ ] **Step 1: Write the failing test**

Кейсы (полный файл теста в сессии):

1. `mlx_whisper_worker_enabled()`: env `0` → False даже при `rest_server.py` в argv; env `1` → True; без env + argv с `rest_server.py` → True; без env + pytest argv → False.
2. Пустой stdout + `poll() == -11` → `MLXWorkerCrashed`, сессия сбрасывает процесс.
3. `{"ok": true, "result": {...}}` → родитель возвращает dict.
4. `{"ok": false, "error": "TypeError: unexpected keyword"}` → наружу `TypeError` (variants loop в engine).
5. Шаблон `ai.krab.ear.rest.plist.template` содержит `KRAB_EAR_MLX_WHISPER_WORKER=1`.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_mlx_whisper_worker_2026_08_16.py -v`

Expected: FAIL — нет модуля `core.mlx_whisper_session`

- [ ] **Step 3: Write minimal implementation**

`mlx_whisper_worker_enabled()`: env `KRAB_EAR_MLX_WHISPER_WORKER` 0/1 побеждает; иначе True если в argv есть `rest_server.py`.

Сессия: `Popen([sys.executable, "-u", worker.py])`, `PYTHONUNBUFFERED=1`, в child `KRAB_EAR_MLX_WHISPER_WORKER=0` (анти-рекурсия), stderr drain, Timer-kill как GigaAM `_send`. Пустой readline → `MLXWorkerCrashed`. stdout воркера — только одна JSON-строка (`redirect_stdout` на stderr вокруг `mlx_whisper.transcribe`). faulthandler в воркере, без `signal.signal` на SIGSEGV.

Протокол: `{"op":"transcribe","audio_path":"...","params":{...}}` → `{"ok":true,"result":{...}}`.

- [ ] **Step 4: Run test to verify it passes**

Та же команда. Expected: PASS

---

### Task 2: engine + adapter + warmup

**Files:**
- Modify: `KrabEar/core/engine.py` (`_transcribe_model`, `warmup`, `close`)
- Modify: `KrabEar/core/pipeline/stt_whisper_mlx_adapter.py`
- Modify: `KrabEar/launchagents/ai.krab.ear.rest.plist.template`
- Modify: `scripts/start_rest_production.command`

- [ ] **Step 1: Failing tests in the same test file**

6. worker ON → `_transcribe_model` зовёт `transcribe_via_mlx_worker`, **не** `get_watchdog`.
7. `MLXWorkerCrashed` на первом variant → сразу наружу, watchdog не трогается.
8. `warmup()` при worker ON не зовёт `mlx_whisper.transcribe` в родителе.
9. `WhisperMLXAdapter.transcribe` при worker ON идёт через `transcribe_via_mlx_worker`.

- [ ] **Step 2: RED then GREEN**

При worker ON: `mlx_inter_process_lock` держит родитель на время RPC (IPC-диктовка ждёт GPU); `mlx_lock` и in-process watchdog **не** оборачивают вызов — Metal только в child. Child flock не берёт (иначе deadlock: родитель ждёт JSON, child ждёт flock).

`AudioEngine.close()` закрывает сессию. Parent пишет temp WAV для ndarray.

- [ ] **Step 3: Не сломать**

`KrabEar/tests/test_engine_mlx_timeout_variant_fallthrough_W1628.py` + `KrabEar/tests/test_stt_warmup.py` (worker OFF по умолчанию в pytest argv).

- [ ] **Step 4:** `scripts/pre_merge_py312_check.sh` на новый тест + `make audit-dead-modules`

- [ ] **Step 5: Commit** явными путями.
