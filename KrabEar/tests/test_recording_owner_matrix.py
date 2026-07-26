"""Матрица start-переходов владельца и spill-meta promote (R2 Task 4).

Тесты фиксируют, что существующая generation обрабатывается до новой попытки
physical start: повтор своего owner идемпотентен, единственный разрешённый
promote — dictation → meeting, остальные пары получают owner_conflict.
Spill-сайдкар следует за CAS-переходом owner, включая компенсационный rollback,
и перезаписывается атомарно с fail-open семантикой.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.recording_spill import RecordingSpillWriter  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _CountingRecorder:
    """Управляемый recorder, считающий реальные попытки start."""

    sample_rate = 16000
    channels = 1

    def __init__(self, *, start_ok: bool = True) -> None:
        self.is_recording = False
        self.start_ok = start_ok
        self.start_calls = 0
        self.stop_calls = 0
        self.set_device_calls: list[object] = []
        self.received_spill = None

    def set_device(self, device) -> None:
        self.set_device_calls.append(device)

    def start(self, spill=None):
        self.start_calls += 1
        self.received_spill = spill
        if not self.start_ok or self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.stop_calls += 1
        if not self.is_recording:
            return None
        self.is_recording = False
        timeline = np.linspace(
            0.0,
            1.0,
            self.sample_rate,
            endpoint=False,
            dtype=np.float32,
        )
        audio = (
            np.sin(2.0 * np.pi * 440.0 * timeline) * 0.3
        ).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _FakeTranscriber:
    """STT-заглушка: Task 4 не проверяет тяжёлую финализацию."""

    def transcribe(self, audio, **kwargs):
        return {
            "text": "owner matrix",
            "confidence": 0.9,
            "engine": "fake",
        }


class _FakeTranslator:
    """Переводчик-заглушка для полноценного RecordingCoreService."""

    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult

        return TranslationResult(
            text=text,
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
        )


class _FakeSettingsService:
    """Кэш настроек без дискового или IPC-доступа."""

    def __init__(self, overrides: dict | None = None) -> None:
        self.overrides = dict(overrides or {})

    def cached_settings(self):
        settings = {
            "recording_spill_enabled": True,
            "silence_guard_enabled": False,
            "background_guard_enabled": False,
            "realtime_preview_enabled": False,
            "realtime_partial_enabled": False,
            "realtime_silence_filter_enabled": False,
            "llm_brain_unload_on_recording": False,
            "llm_brain_lease_enabled": False,
            "auto_glossary_enabled": False,
            "stt_hotwords_enabled": False,
            "diarization_enabled": False,
        }
        settings.update(self.overrides)
        return settings

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    """Отключённый semantic search для конструктора Core."""

    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_service(
    data_dir: Path,
    *,
    recorder: _CountingRecorder | None = None,
    settings_overrides: dict | None = None,
) -> RecordingCoreService:
    """Собрать Core с реальным spill и без настоящих audio/STT worker-ов."""
    vocabulary = MagicMock()
    vocabulary.get_words.return_value = []
    vocabulary.load.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=recorder or _CountingRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=StateStore(data_dir=data_dir),
        vocabulary=vocabulary,
        settings_svc=_FakeSettingsService(settings_overrides),
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
        rescue_dir=data_dir / "rescue",
    )


class RecordingOwnerMatrixTest(unittest.TestCase):
    """Проверить полную конечную матрицу owner-переходов."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self._service_index = 0

    def _service(
        self,
        *,
        recorder: _CountingRecorder | None = None,
        settings_overrides: dict | None = None,
    ) -> RecordingCoreService:
        self._service_index += 1
        service = _make_service(
            self._tmp / f"service-{self._service_index}",
            recorder=recorder,
            settings_overrides=settings_overrides,
        )
        self.addCleanup(service.close_background_workers)
        return service

    @staticmethod
    def _read_meta(spill: RecordingSpillWriter) -> dict:
        return json.loads(spill._meta_path.read_text(encoding="utf-8"))

    def test_fresh_start_assigns_requested_owner(self):
        for owner in ("dictation", "quick_capture", "meeting"):
            with self.subTest(owner=owner):
                recorder = _CountingRecorder()
                service = self._service(recorder=recorder)

                response = service.handle_start_recording(
                    {"source": owner}
                )

                self.assertEqual(response["status"], "recording")
                self.assertEqual(response["owner"], owner)
                self.assertEqual(
                    response["generation_token"],
                    service._active_generation["token"],
                )
                self.assertEqual(recorder.start_calls, 1)

    def test_empty_or_null_source_keeps_legacy_dictation_owner(self):
        for raw_source in (None, "", "   "):
            with self.subTest(source=raw_source):
                service = self._service()

                response = service.handle_start_recording(
                    {"source": raw_source}
                )

                self.assertEqual(response["status"], "recording")
                self.assertEqual(response["owner"], "dictation")
                self.assertEqual(
                    service._active_generation["owner"],
                    "dictation",
                )

    def test_same_owner_repeat_is_idempotent_without_second_start(self):
        for owner in ("dictation", "quick_capture", "meeting"):
            with self.subTest(owner=owner):
                recorder = _CountingRecorder()
                service = self._service(recorder=recorder)
                started = service.handle_start_recording(
                    {"source": owner}
                )
                generation = service._active_generation
                spill = service._active_spill
                revision = generation["revision"]

                repeated = service.handle_start_recording(
                    {"source": owner}
                )

                self.assertEqual(repeated["status"], "already_recording")
                self.assertFalse(repeated["promoted"])
                self.assertFalse(repeated["owner_promoted"])
                self.assertEqual(
                    repeated["generation_token"],
                    started["generation_token"],
                )
                self.assertEqual(repeated["owner"], owner)
                self.assertEqual(repeated["owner_revision"], revision)
                self.assertIs(service._active_generation, generation)
                self.assertIs(service._active_spill, spill)
                self.assertEqual(recorder.start_calls, 1)

    def test_meeting_promotes_dictation_without_second_start(self):
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"selected_input_device": 7},
        )
        started = service.handle_start_recording(
            {"source": "dictation"}
        )
        generation = service._active_generation
        spill = service._active_spill
        revision_before = generation["revision"]
        spill.append(np.linspace(
            -0.2,
            0.2,
            320,
            dtype=np.float32,
        ))
        part_before = spill.part_path.read_bytes()
        session_id_before = spill.session_id
        paths_before = set(service._rescue_dir.iterdir())

        promoted = service.handle_start_recording(
            {"source": "meeting"}
        )

        self.assertEqual(promoted["status"], "already_recording")
        self.assertTrue(promoted["promoted"])
        self.assertTrue(promoted["owner_promoted"])
        self.assertEqual(
            promoted["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(promoted["owner"], "meeting")
        self.assertGreater(promoted["owner_revision"], revision_before)
        self.assertIs(service._active_generation, generation)
        self.assertIs(service._active_spill, spill)
        self.assertEqual(spill.session_id, session_id_before)
        self.assertEqual(spill.part_path.read_bytes(), part_before)
        self.assertEqual(generation["owner"], "meeting")
        self.assertEqual(generation["promoted_from"], "dictation")
        self.assertEqual(recorder.start_calls, 1)
        self.assertEqual(recorder.set_device_calls, [7])
        self.assertEqual(
            set(service._rescue_dir.iterdir()),
            paths_before,
        )
        self.assertEqual(
            self._read_meta(spill)["source"],
            "meeting",
        )
        self.assertEqual(
            self._read_meta(spill)["promoted_from"],
            "dictation",
        )

    def test_all_foreign_owner_pairs_return_conflict_without_side_effects(
        self,
    ):
        forbidden_pairs = (
            ("dictation", "quick_capture"),
            ("quick_capture", "dictation"),
            ("quick_capture", "meeting"),
            ("meeting", "dictation"),
            ("meeting", "quick_capture"),
        )
        for current_owner, requested_owner in forbidden_pairs:
            with self.subTest(
                current=current_owner,
                requested=requested_owner,
            ):
                recorder = _CountingRecorder()
                service = self._service(
                    recorder=recorder,
                    settings_overrides={"selected_input_device": 9},
                )
                started = service.handle_start_recording(
                    {"source": current_owner}
                )
                generation = service._active_generation
                spill = service._active_spill
                revision = generation["revision"]
                meta_before = spill._meta_path.read_bytes()

                response = service.handle_start_recording(
                    {"source": requested_owner}
                )

                self.assertEqual(response["status"], "owner_conflict")
                self.assertEqual(response["owner"], current_owner)
                self.assertNotIn("generation_token", response)
                self.assertIs(service._active_generation, generation)
                self.assertEqual(generation["owner"], current_owner)
                self.assertEqual(generation["revision"], revision)
                self.assertEqual(
                    generation["token"],
                    started["generation_token"],
                )
                self.assertIs(service._active_spill, spill)
                self.assertEqual(
                    spill._meta_path.read_bytes(),
                    meta_before,
                )
                self.assertEqual(recorder.start_calls, 1)
                self.assertEqual(recorder.set_device_calls, [9])

    def test_recording_without_generation_is_unmanaged_for_every_owner(self):
        for requested_owner in (
            "dictation",
            "quick_capture",
            "meeting",
        ):
            with self.subTest(requested=requested_owner):
                recorder = _CountingRecorder()
                recorder.is_recording = True
                service = self._service(recorder=recorder)

                response = service.handle_start_recording(
                    {"source": requested_owner}
                )

                self.assertEqual(
                    response["status"],
                    "unmanaged_recording",
                )
                self.assertTrue(response["is_recording"])
                self.assertIsNone(service._active_generation)
                self.assertIsNone(service._active_spill)
                self.assertEqual(recorder.start_calls, 0)
                self.assertEqual(recorder.set_device_calls, [])
                rescue_dir = service._rescue_dir
                self.assertFalse(
                    rescue_dir.exists() and any(rescue_dir.iterdir())
                )

    def test_recorder_stopping_without_generation_stays_typed(self):
        recorder = _CountingRecorder(start_ok=False)
        service = self._service(recorder=recorder)

        response = service.handle_start_recording(
            {"source": "meeting"}
        )

        self.assertEqual(response["status"], "recorder_stopping")
        self.assertFalse(response["is_recording"])
        self.assertIsNone(service._active_generation)
        self.assertIsNone(service._active_spill)
        self.assertEqual(recorder.start_calls, 1)

    def test_promote_rollback_rewrites_spill_meta_back_to_dictation(self):
        service = self._service()
        service.handle_start_recording({"source": "dictation"})
        spill = service._active_spill
        promoted = service.handle_start_recording(
            {"source": "meeting"}
        )
        promoted_meta = self._read_meta(spill)
        self.assertEqual(promoted_meta["source"], "meeting")
        self.assertEqual(
            promoted_meta["promoted_from"],
            "dictation",
        )

        rolled_back = service.rollback_owner_transition(
            expected_revision=promoted["owner_revision"],
            expected_owner="meeting",
            restore_owner="dictation",
        )

        self.assertTrue(rolled_back)
        self.assertEqual(
            service._active_generation["owner"],
            "dictation",
        )
        rollback_meta = self._read_meta(spill)
        self.assertEqual(rollback_meta["source"], "dictation")
        self.assertNotIn("promoted_from", rollback_meta)

    def test_stale_rollback_does_not_rewrite_spill_meta(self):
        service = self._service()
        service.handle_start_recording({"source": "dictation"})
        spill = service._active_spill
        promoted = service.handle_start_recording(
            {"source": "meeting"}
        )
        meta_before = spill._meta_path.read_bytes()

        rolled_back = service.rollback_owner_transition(
            expected_revision=promoted["owner_revision"] - 1,
            expected_owner="meeting",
            restore_owner="dictation",
        )

        self.assertFalse(rolled_back)
        self.assertEqual(
            service._active_generation["owner"],
            "meeting",
        )
        self.assertEqual(spill._meta_path.read_bytes(), meta_before)

    def test_spill_rewrite_failure_does_not_rollback_owner_transition(self):
        service = self._service()
        started = service.handle_start_recording(
            {"source": "dictation"}
        )
        spill = service._active_spill
        spill.rewrite_source = MagicMock(
            side_effect=OSError("meta недоступна")
        )

        promoted = service.handle_start_recording(
            {"source": "meeting"}
        )

        self.assertEqual(promoted["status"], "already_recording")
        self.assertTrue(promoted["promoted"])
        self.assertEqual(
            promoted["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(
            service._active_generation["owner"],
            "meeting",
        )
        spill.rewrite_source.assert_called_once_with(
            "meeting",
            promoted_from="dictation",
        )


class RecordingSpillRewriteSourceTest(unittest.TestCase):
    """Проверить атомарный fail-open контракт сайдкара."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmp_ctx.cleanup)
        self.rescue_dir = Path(self._tmp_ctx.name) / "rescue"

    def _writer(
        self,
        *,
        source: str = "dictation",
        session_id: str = "generation-token",
    ) -> RecordingSpillWriter:
        writer = RecordingSpillWriter(
            rescue_dir=self.rescue_dir,
            sample_rate=16000,
            channels=1,
            source=source,
            session_id=session_id,
        )
        self.addCleanup(writer.discard)
        return writer

    @staticmethod
    def _read_meta(writer: RecordingSpillWriter) -> dict:
        return json.loads(
            writer._meta_path.read_text(encoding="utf-8")
        )

    def test_rewrite_source_preserves_format_and_adds_promotion(self):
        writer = self._writer()
        self.assertTrue(writer.open())
        before = self._read_meta(writer)

        rewritten = writer.rewrite_source(
            "meeting",
            promoted_from="dictation",
        )

        self.assertTrue(rewritten)
        after = self._read_meta(writer)
        self.assertEqual(after["source"], "meeting")
        self.assertEqual(after["promoted_from"], "dictation")
        self.assertEqual(
            after["sample_rate"],
            before["sample_rate"],
        )
        self.assertEqual(after["channels"], before["channels"])
        self.assertEqual(
            after["started_at_iso"],
            before["started_at_iso"],
        )
        self.assertEqual(writer.source, "meeting")
        self.assertEqual(list(self.rescue_dir.glob("*.tmp")), [])

    def test_rewrite_source_with_none_removes_old_promoted_from(self):
        writer = self._writer()
        self.assertTrue(writer.open())
        self.assertTrue(writer.rewrite_source(
            "meeting",
            promoted_from="dictation",
        ))

        rewritten = writer.rewrite_source(
            "dictation",
            promoted_from=None,
        )

        self.assertTrue(rewritten)
        meta = self._read_meta(writer)
        self.assertEqual(meta["source"], "dictation")
        self.assertNotIn("promoted_from", meta)
        self.assertEqual(writer.source, "dictation")

    def test_rewrite_source_remains_available_after_close(self):
        writer = self._writer()
        self.assertTrue(writer.open())
        writer.close()

        rewritten = writer.rewrite_source(
            "meeting",
            promoted_from="dictation",
        )

        self.assertTrue(rewritten)
        self.assertEqual(self._read_meta(writer)["source"], "meeting")

    def test_atomic_replace_failure_keeps_old_meta_and_cleans_temp(self):
        writer = self._writer()
        self.assertTrue(writer.open())
        before = writer._meta_path.read_bytes()

        with patch(
            "core.atomic_io.os.replace",
            side_effect=OSError("rename недоступен"),
        ):
            rewritten = writer.rewrite_source(
                "meeting",
                promoted_from="dictation",
            )

        self.assertFalse(rewritten)
        self.assertEqual(writer._meta_path.read_bytes(), before)
        self.assertEqual(writer.source, "dictation")
        self.assertEqual(list(self.rescue_dir.glob("*.tmp")), [])

    def test_writer_without_path_ownership_cannot_rewrite_collision(self):
        first = self._writer(session_id="shared-token")
        self.assertTrue(first.open())
        before = first._meta_path.read_bytes()
        second = self._writer(
            source="meeting",
            session_id="shared-token",
        )
        self.assertFalse(second.open())

        rewritten = second.rewrite_source(
            "meeting",
            promoted_from="dictation",
        )

        self.assertFalse(rewritten)
        self.assertEqual(first._meta_path.read_bytes(), before)
        self.assertEqual(first.source, "dictation")

    def test_rewrite_after_discard_does_not_recreate_meta(self):
        writer = self._writer()
        self.assertTrue(writer.open())
        writer.discard()
        self.assertFalse(writer._meta_path.exists())

        rewritten = writer.rewrite_source(
            "meeting",
            promoted_from="dictation",
        )

        self.assertFalse(rewritten)
        self.assertFalse(writer._meta_path.exists())


if __name__ == "__main__":
    unittest.main()
