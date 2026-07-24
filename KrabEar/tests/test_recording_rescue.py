"""Тесты recording_rescue.run_rescue_scan (R1 Фаза 1, Task 4).

Сеет .f32.part+.meta.json через РЕАЛЬНЫЙ RecordingSpillWriter (Task 1),
восстановление гоняется против фейковых recording_core/error_bus/
collection_manager — соответствует интерфейсам, зафиксированным Task 3.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_rescue import run_rescue_scan  # noqa: E402
from backend.recording_spill import RecordingSpillWriter  # noqa: E402


def _seed_part(rescue_dir: Path, source: str = "dictation", seconds: float = 1.0) -> Path:
    w = RecordingSpillWriter(rescue_dir=rescue_dir, sample_rate=16000, channels=1, source=source)
    assert w.open()
    n = int(16000 * seconds)
    w.append((np.ones(n, dtype=np.float32) * 0.2))
    w.close()
    return w.part_path


class FakeRecordingCore:
    def __init__(self, resp: dict | None = None, raise_exc: Exception | None = None):
        self.calls: list[dict] = []
        self._resp = resp
        self._raise_exc = raise_exc

    def handle_transcribe_paths(self, params):
        self.calls.append(params)
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._resp is not None:
            return self._resp
        wav_path = params["paths"][0]
        return {
            "items": [{"path": wav_path, "history_id": f"hist-{len(self.calls)}"}],
            "processed": 1,
            "errors": [],
        }


class _SlowRecordingCore:
    """Блокируется, пока не отпустят — для проверки single-flight."""

    def __init__(self, release_event: threading.Event):
        self._release_event = release_event
        self.calls = 0

    def handle_transcribe_paths(self, params):
        self.calls += 1
        self._release_event.wait(timeout=2.0)
        return {
            "items": [{"path": params["paths"][0], "history_id": "hist-slow"}],
            "processed": 1,
            "errors": [],
        }


class FakeErrorBus:
    def __init__(self):
        self.pushed: list = []

    def push(self, err):
        self.pushed.append(err)


class FakeCollectionManager:
    def __init__(self):
        self._collections: dict[str, list[str]] = {}
        self.added: list[tuple[str, str]] = []

    def list_collections(self):
        return [{"name": name} for name in self._collections]

    def create_collection(self, name, description=""):
        if name in self._collections:
            raise ValueError(f"'{name}' already exists")
        self._collections[name] = []
        return {"name": name}

    def add_to_collection(self, collection_name, item_id):
        self._collections.setdefault(collection_name, []).append(item_id)
        self.added.append((collection_name, item_id))
        return {"name": collection_name}


def _settings(overrides: dict | None = None):
    data = dict(overrides or {})
    return lambda key, default=None: data.get(key, default)


class RunRescueScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_ctx.cleanup)
        self.rescue_dir = Path(self._tmp_ctx.name) / "rescue"

    def test_scan_finalizes_and_transcribes(self):
        _seed_part(self.rescue_dir, source="dictation")
        core = FakeRecordingCore()
        error_bus = FakeErrorBus()
        collections = FakeCollectionManager()
        result = run_rescue_scan(
            rescue_dir=self.rescue_dir,
            recording_core=core,
            error_bus=error_bus,
            settings_get=_settings(),
            collection_manager=collections,
        )
        self.assertEqual(result, {"rescued": 1, "transcribed": 1, "kept_wavs": 0})
        self.assertEqual(len(core.calls), 1)
        self.assertFalse(list(self.rescue_dir.glob("*.f32.part")))
        self.assertFalse(list(self.rescue_dir.glob("*.rescued.wav")))
        self.assertEqual(len(error_bus.pushed), 1)
        self.assertEqual(error_bus.pushed[0].code, "audio.recording_rescued")
        self.assertEqual(len(collections.added), 1)
        self.assertEqual(collections.added[0][1], "hist-1")

    def test_privacy_mode_keeps_wav_no_transcription(self):
        _seed_part(self.rescue_dir)
        core = FakeRecordingCore()
        result = run_rescue_scan(
            rescue_dir=self.rescue_dir,
            recording_core=core,
            error_bus=FakeErrorBus(),
            settings_get=_settings({"privacy_mode_enabled": True}),
            collection_manager=FakeCollectionManager(),
        )
        self.assertEqual(result, {"rescued": 1, "transcribed": 0, "kept_wavs": 1})
        self.assertEqual(core.calls, [])
        self.assertEqual(len(list(self.rescue_dir.glob("*.rescued.wav"))), 1)

    def test_transcribe_failure_keeps_wav(self):
        _seed_part(self.rescue_dir)
        core = FakeRecordingCore(resp={"items": [], "processed": 0, "errors": ["stt boom"]})
        result = run_rescue_scan(
            rescue_dir=self.rescue_dir,
            recording_core=core,
            error_bus=FakeErrorBus(),
            settings_get=_settings(),
            collection_manager=FakeCollectionManager(),
        )
        self.assertEqual(result, {"rescued": 1, "transcribed": 0, "kept_wavs": 1})
        self.assertEqual(len(list(self.rescue_dir.glob("*.rescued.wav"))), 1)

    def test_single_flight(self):
        _seed_part(self.rescue_dir)
        release = threading.Event()
        slow_core = _SlowRecordingCore(release)
        results: list[dict] = []

        def _run_first():
            results.append(run_rescue_scan(
                rescue_dir=self.rescue_dir,
                recording_core=slow_core,
                error_bus=FakeErrorBus(),
                settings_get=_settings(),
                collection_manager=FakeCollectionManager(),
            ))

        t = threading.Thread(target=_run_first, daemon=True)
        t.start()
        deadline = time.monotonic() + 2.0
        while slow_core.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(slow_core.calls, 1)

        second_core = FakeRecordingCore()
        second_result = run_rescue_scan(
            rescue_dir=self.rescue_dir,
            recording_core=second_core,
            error_bus=FakeErrorBus(),
            settings_get=_settings(),
            collection_manager=FakeCollectionManager(),
        )
        self.assertEqual(second_result, {"rescued": 0, "transcribed": 0, "kept_wavs": 0})
        self.assertEqual(second_core.calls, [])

        release.set()
        t.join(timeout=2.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], {"rescued": 1, "transcribed": 1, "kept_wavs": 0})

    def test_scan_never_raises_on_garbage(self):
        self.rescue_dir.mkdir(parents=True, exist_ok=True)
        garbage = self.rescue_dir / "not-a-real-session.f32.part"
        garbage.write_bytes(b"\x00\x01\x02\x03garbage-no-sidecar")
        try:
            result = run_rescue_scan(
                rescue_dir=self.rescue_dir,
                recording_core=FakeRecordingCore(),
                error_bus=FakeErrorBus(),
                settings_get=_settings(),
                collection_manager=FakeCollectionManager(),
            )
        except Exception as exc:  # pragma: no cover - тест обязан не дойти сюда
            self.fail(f"run_rescue_scan бросил исключение на мусорном файле: {exc}")
        self.assertEqual(result, {"rescued": 0, "transcribed": 0, "kept_wavs": 0})
        # Без сайдкара finalize_part_to_wav не трогает файл — он остаётся.
        self.assertTrue(garbage.exists())

    def test_limit_10_per_pass(self):
        for _ in range(11):
            _seed_part(self.rescue_dir)
        core = FakeRecordingCore()
        result = run_rescue_scan(
            rescue_dir=self.rescue_dir,
            recording_core=core,
            error_bus=FakeErrorBus(),
            settings_get=_settings(),
            collection_manager=FakeCollectionManager(),
        )
        self.assertEqual(result["rescued"], 10)
        self.assertEqual(result["transcribed"], 10)
        self.assertEqual(len(core.calls), 10)
        remaining = list(self.rescue_dir.glob("*.f32.part"))
        self.assertEqual(len(remaining), 1)

    def test_parts_param_ignores_files_outside_frozen_snapshot(self):
        """R1 HIGH-1 (adversarial-гейт целого диффа, 2026-07-24): при
        переданном ``parts=`` скан обязан работать ТОЛЬКО с этим списком,
        даже если в директории уже лежит ДРУГОЙ .f32.part-файл (симулирует
        гонку — новая ЖИВАЯ запись, начатая, пока фоновый
        startup-recovery-тред ещё был занят check_and_collect()). Живой файл
        не должен быть тронут: не финализирован, не удалён, не
        транскрибирован."""
        old_part = _seed_part(self.rescue_dir, source="dictation")
        # "Новая" запись появляется УЖЕ ПОСЛЕ заморозки снимка вызывающей
        # стороной — вызывающая сторона передаёт список БЕЗ этого файла.
        new_live_part = _seed_part(self.rescue_dir, source="meeting")
        core = FakeRecordingCore()
        result = run_rescue_scan(
            rescue_dir=self.rescue_dir,
            recording_core=core,
            error_bus=FakeErrorBus(),
            settings_get=_settings(),
            collection_manager=FakeCollectionManager(),
            parts=[old_part],
        )
        self.assertEqual(result, {"rescued": 1, "transcribed": 1, "kept_wavs": 0})
        self.assertEqual(len(core.calls), 1)
        # Старый файл обработан и удалён (обычное восстановление).
        self.assertFalse(old_part.exists())
        # Живой файл НЕ в снимке — обязан пережить скан нетронутым.
        self.assertTrue(new_live_part.exists())

    def test_parts_param_none_falls_back_to_live_glob(self):
        """Обратная совместимость: без parts= — прежнее поведение (живой
        glob под локом), используется прямыми вызовами/существующими
        тестами этого файла."""
        part = _seed_part(self.rescue_dir)
        core = FakeRecordingCore()
        result = run_rescue_scan(
            rescue_dir=self.rescue_dir,
            recording_core=core,
            error_bus=FakeErrorBus(),
            settings_get=_settings(),
            collection_manager=FakeCollectionManager(),
        )
        self.assertEqual(result["rescued"], 1)
        self.assertFalse(part.exists())


if __name__ == "__main__":
    unittest.main()
