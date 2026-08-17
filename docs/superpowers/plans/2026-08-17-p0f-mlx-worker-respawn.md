# P0f mlx_whisper worker respawn Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Мёртвый mlx_whisper child (`poll() is not None`) не блокирует следующий REST STT: `start()` респавнит, как GigaAM W1216 F1.

**Architecture:** Сиблинг `GigaAMAdapter._get_subprocess_session` F1. `MLXWhisperSession.start()` сейчас early-return при `_proc is not None` без `poll()` — idle SEGV/OOM между запросами оставляет мёртвый Popen, первый POST после этого ловит BrokenPipe/`MLXWorkerCrashed`. Чистить мёртвый child и спавнить один раз. In-flight SEGV по-прежнему `MLXWorkerCrashed` наружу (не ретраить то же аудио).

**Tech Stack:** уже есть `core/mlx_whisper_session.py`, unittest, без новых зависимостей.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/p0f-mlx-worker-respawn`.

**Баны:** база только `origin/codex/krab-ear-v2`; `git add` явными путями; не запускать `KrabEarAgent`; не `kickstart -k`; не мержить PR #1875; не `REST_IN_PROCESS_ENABLED`; не коммитить `wake_word_models/hard_negatives_raw/`; не трогать Main Krab / VG `.env`; не stash/reset чужой WIP в общем чекауте; не `signal.signal` на SIGSEGV; не включать worker на IPC-диктовке.

**Вне скоупа:** IPC-backend worker по умолчанию; ретрай того же transcribe после in-flight SEGV; GigaAM; живой REST restart.

---

### Task 1: Dead-child respawn в start()

**Files:**
- Modify: `KrabEar/core/mlx_whisper_session.py` (`start`)
- Modify: `KrabEar/tests/test_mlx_whisper_worker_2026_08_16.py`
- Modify: `docs/NOW.md` (HEAD + следующая волна)
- Modify: `docs/ROADMAP-2026H2.md` (журнал)

- [x] **Step 1: Write the failing test**

В `test_mlx_whisper_worker_2026_08_16.py` рядом с `test_empty_read_after_sigsegv_raises_crashed`:

```python
def test_dead_child_respawns_before_next_transcribe(self):
    """W1216 F1 sibling: poll()!=None → новый child, этот запрос успешен."""
    from core.mlx_whisper_session import get_mlx_whisper_session

    dead = self._fake_popen("", -11)
    live = self._fake_popen(
        '{"ok": true, "result": {"text": "ok", "segments": []}}\n',
        None,
    )
    pops = [dead, live]
    session = get_mlx_whisper_session()
    with patch(
        "core.mlx_whisper_session.subprocess.Popen",
        side_effect=lambda *a, **k: pops.pop(0),
    ):
        session.start()
        self.assertIs(session._proc, dead)
        result = session.transcribe(
            "/tmp/a.wav",
            {"path_or_hf_repo": "mlx-community/whisper-large-v3-turbo"},
            timeout_sec=2.0,
            model_name="turbo",
        )
    self.assertEqual(result["text"], "ok")
    self.assertIs(session._proc, live)
    self.assertEqual(pops, [])

def test_live_child_start_does_not_respawn(self):
    from core.mlx_whisper_session import get_mlx_whisper_session

    live = self._fake_popen(
        '{"ok": true, "result": {"text": "ok", "segments": []}}\n',
        None,
    )
    session = get_mlx_whisper_session()
    with patch(
        "core.mlx_whisper_session.subprocess.Popen",
        return_value=live,
    ) as popen:
        session.start()
        session.start()
        result = session.transcribe(
            "/tmp/a.wav",
            {"path_or_hf_repo": "mlx-community/whisper-large-v3-turbo"},
            timeout_sec=2.0,
            model_name="turbo",
        )
    self.assertEqual(popen.call_count, 1)
    self.assertEqual(result["text"], "ok")
    self.assertIs(session._proc, live)
```

Существующий `test_empty_read_after_sigsegv_raises_crashed` остаётся: in-flight пустой stdout → `MLXWorkerCrashed`, не бесконечный респавн внутри `_send`.

- [x] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_mlx_whisper_worker_2026_08_16.py::MLXWhisperSessionProtocolTests::test_dead_child_respawns_before_next_transcribe -v`

Expected: FAIL — `MLXWorkerCrashed` или `IndexError`/`text` не `ok` (start() не респавнит).

- [x] **Step 3: Write minimal implementation**

`start()`: под `_lock`, если `_proc is not None` и `poll() is None` — return; если `_proc` мёртвый — `_reap_dead_unlocked()` (kill + закрыть pipes, без shutdown JSON); затем один `_spawn_unlocked()`. Не крутить цикл «spawn пока poll None» — мок с `poll()=-11` сразу после spawn не должен зациклиться.

- [x] **Step 4: Run tests**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_mlx_whisper_worker_2026_08_16.py -v`

Expected: PASS, включая старые crash/ok/kill.

- [x] **Step 5: py312 + flake8 + docs**

Run: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_mlx_whisper_worker_2026_08_16.py`

`NOW.md`: HEAD = `e33509d8` (P0e) до мержа этой волны; следующая = эта карточка. После кода — P0f закрыта в коде. `ROADMAP-2026H2.md` журнал: сиблинг W1216 F1.

- [ ] **Step 6: Commit** явными путями.
