# W2 CallAssist ownership bypass Implementation Plan

**Goal:** `call_assist/stop` больше не останавливает чужую диктовку/встречу —
recorder останавливается только когда никто другой им не владеет.

**Architecture:** `CallAssistService` делит сырой `recorder` с
`RecordingCoreService`, но в модели generation не участвует: start — сырой
`recorder.start()`, stop — сырой `recorder.stop()` по собственному флагу
`active`. Сценарий потери: диктовка пишет (owner=dictation) → `call/start`
видит `is_recording`, пропускает старт, но взводит `active` → `call/stop`
видит `active+is_recording` → `recorder.stop()` → диктовка обрезана.
Фикс минимально-архитектурный (без публикации generation звонком —
это отдельный редизайн; без untouched start-пути):
1. `CallAssistService.__init__`: новый keyword-only `recording_core=None`
   + `self._recording_core` (дефолт None = legacy для прямых юнит-конструкций).
2. `service.py`: late-inject `self._call_assist._recording_core =
   self._recording_core_svc` рядом с R2 Task 5 (тот же безопасный порядок —
   core сконструирован позже).
3. `handle_stop`: сырой `recorder.stop()` только через `_may_stop_shared_recorder()`
   (owner None → можно; чужой owner → пропуск + warning; core None → legacy;
   исключение чтения owner → fail-closed False).
`handle_start` НЕ трогаем (уже безопасен: пропускает старт при записи;
асимметрия владения звонком задокументирована, не фиксится здесь — YAGNI).

**Tech Stack:** то, что в репо. Существующий accessor
`RecordingCoreService.current_recording_owner()` (lock-protected, создан
для watchdog — переиспользуем, нового API нет). Тест: REAL Core
(паттерн `test_recording_generation._make_service`) + SHARED FakeRecorder
+ `CallAssistService(..., recording_core=core)`; teardown —
`addCleanup(core.close_background_workers)` (паттерн line 324 того файла)
+ tmp cleanup. `service.close()` не нужен (нет BackendService).

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/w2-callassist-ownership`,
ветка `fix/callassist-ownership-20260916` (откат = удалить ветку/worktree).

**Баны:** вставь список из `docs/EXECUTOR_PLAYBOOK.md` §1. Плюс: `handle_start`
не трогать; публикацию generation звонком не делать (отдельный редизайн);
`git add` явными путями; мерж в колею после зелени; деплой — отдельным решением.

---

### Task 1: ownership-gate на stop

**Files:**
- Modify: `KrabEar/backend/call_assist_service.py` (`__init__` kwarg +
  `_may_stop_shared_recorder` + условие в `handle_stop`)
- Modify: `KrabEar/backend/service.py` (late-inject после R2 Task 5 блока)
- Test: `KrabEar/tests/test_call_assist_ownership_20260916.py` (новый)

- [ ] **Step 1: Write the failing tests**

```python
"""W2: call/stop не останавливает чужую диктовку (ownership bypass)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient
from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore


class SharedRecorder:
    """Один рекордер на Core и CallAssist (как продовый shared)."""

    def __init__(self) -> None:
        self.is_recording = False
        self.stop_calls = 0

    def start(self, spill=None) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        self.stop_calls += 1
        if not self.is_recording:
            return None
        self.is_recording = False
        return None

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(32000, dtype=np.float32), 2.0


class FakeStore:
    def __init__(self) -> None:
        self._settings = {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "test-key-42",
            "call_auto_summary": False,
            "call_notify_default": True,
        }

    def load_settings(self, lock_timeout_sec=None, nowait=False):
        return dict(self._settings)


class FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "x", "confidence": 0.9, "engine": "fake"}

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return {"text": "x"}


class _GwOk(VoiceGatewayClient):
    def start_session(self, voice_gateway_url, api_key, payload):
        return {"ok": True, "session_id": "gw-w2-001"}

    def stop_session(self, voice_gateway_url, api_key, session_id):
        return {"ok": True}

    def get(self, voice_gateway_url, api_key, path):
        return {"ok": True, "payload": {}}

    def post(self, voice_gateway_url, api_key, path, payload):
        return {"ok": True}

    def delete(self, voice_gateway_url, api_key, path):
        return {"ok": True}


class _FakeSettingsService:
    def __init__(self):
        self._settings = {
            "silence_guard_enabled": False,
            "background_guard_enabled": False,
            "realtime_preview_enabled": False,
            "realtime_partial_enabled": False,
            "realtime_silence_filter_enabled": False,
            "llm_brain_unload_on_recording": False,
            "llm_brain_lease_enabled": False,
        }

    def cached_settings(self):
        return dict(self._settings)

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_core(tmp_dir, recorder):
    vocabulary = MagicMock()
    vocabulary.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=recorder,
        transcriber=_FakeTranscriber(),
        translator=MagicMock(),
        store=StateStore(data_dir=tmp_dir),
        vocabulary=vocabulary,
        settings_svc=_FakeSettingsService(),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=session_tracker,
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
        rescue_dir=tmp_dir / "rescue",
    )


def _make_call(store, recorder, core):
    return CallAssistService(
        store=store,
        recorder=recorder,
        transcriber=_FakeTranscriber(),
        gateway=_GwOk(),
        recording_core=core,
    )


class CallAssistOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self.tmp = Path(self._tmp_ctx.name)
        self.recorder = SharedRecorder()
        self.core = _make_core(self.tmp / "data", self.recorder)
        self.addCleanup(self.core.close_background_workers)
        self.store = FakeStore()
        self.call = _make_call(self.store, self.recorder, self.core)

    def test_stop_does_not_kill_foreign_dictation(self) -> None:
        start = self.core.handle_start_recording({})
        self.assertEqual(start["status"], "recording")
        self.assertEqual(self.core.current_recording_owner(), "dictation")
        self.assertTrue(self.call.handle_start({})["ok"])
        stopped = self.call.handle_stop({"auto_summary": False})
        self.assertEqual(stopped["status"], "stopped")
        self.assertTrue(
            self.recorder.is_recording,
            "call/stop остановил чужую диктовку (ownership bypass)",
        )

    def test_stop_stops_call_owned_capture(self) -> None:
        self.assertTrue(self.call.handle_start({})["ok"])
        self.assertTrue(self.recorder.is_recording)
        stopped = self.call.handle_stop({"auto_summary": False})
        self.assertEqual(stopped["status"], "stopped")
        self.assertFalse(self.recorder.is_recording)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=<worktree>/KrabEar <venv>/bin/python -m pytest KrabEar/tests/test_call_assist_ownership_20260916.py -v` (cwd = worktree)
Expected: `test_stop_does_not_kill_foreign_dictation` FAIL (recorder
остановлен); `test_stop_stops_call_owned_capture` PASS.

- [ ] **Step 3: Write minimal implementation**

```python
# call_assist_service.py, __init__ signature:
        settings_get: Callable[[str, Any], Any] | None = None,
        recording_core: Any = None,
    ) -> None:
```

```python
# call_assist_service.py, __init__ body (после self._settings_get):
        # W2 (2026-09-16): сырой recorder shared с RecordingCoreService.
        # Без ссылки на core stop() не может проверить чужое владение.
        # None = прямые юнит-конструкции (legacy-поведение, см. гейт ниже).
        self._recording_core: Any = recording_core
```

```python
# call_assist_service.py, новый метод рядом с handle_stop:
    def _may_stop_shared_recorder(self) -> bool:
        """Может ли call assist останавливать общий рекордер.

        Останавливаем, только если захватом никто другой не владеет:
        owner None (idle либо захват открыт самим звонком без публикации) —
        можно; чужой owner (dictation/meeting/...) — нельзя, иначе stop
        звонка обрежет чужую запись. Без привязанного core — legacy.
        Ошибка чтения owner — fail-closed False (потеря записи хуже
        зависшего микрофона; тот чинится следующим явным stop).
        """
        core = self._recording_core
        if core is None:
            return True
        try:
            owner = core.current_recording_owner()
        except Exception:
            logger.exception("call_assist stop: owner не прочитан — не останавливаем")
            return False
        if owner is not None:
            logger.warning(
                "call_assist stop: рекордер занят владельцем %r — пропуск stop()",
                owner,
            )
            return False
        return True
```

```python
# call_assist_service.py, handle_stop: заменить условие
        if active and self.recorder.is_recording:
```
```python
        if active and self.recorder.is_recording and self._may_stop_shared_recorder():
```

```python
# service.py, после R2 Task 5 блока (self._history._recording_core = ...):
        # W2 (2026-09-16): CallAssistService создан раньше RecordingCoreService,
        # прямой late-inject повторяет безопасный порядок R2 Task 5. Нужен для
        # ownership-гейта recorder.stop() (чужую диктовку/встречу не останавливать).
        self._call_assist._recording_core = self._recording_core_svc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: 2 PASS. Плюс регрессия:
`test_call_assist_service_deep.py`, `test_call_assist_service_edges.py`,
`test_call_assist_service.py`, `test_recording_generation.py`,
`test_recording_owner_state.py`, `test_recording_owner_matrix.py` — все PASS.

- [ ] **Step 5: Gate + commit (без мержа в колею)**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_call_assist_ownership_20260916.py
<repo-flake8 на 3 изменённых .py>
git add KrabEar/backend/call_assist_service.py KrabEar/backend/service.py KrabEar/tests/test_call_assist_ownership_20260916.py docs/superpowers/plans/2026-09-16-callassist-ownership.md
git commit -m "fix(call): call/stop не останавливает чужую запись (W2 ownership)"
```

Мерж в колею + push — после зелени и отдельной отмашки. NOW-квест
обновить отдельным docs-коммитом (W1→W2→W3 в очереди).
