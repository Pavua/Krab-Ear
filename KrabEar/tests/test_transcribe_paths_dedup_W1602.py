"""W1602 — unit tests for _transcribe_paths_core dedup guard.

W1588 F2 MED: batch-import path (_transcribe_paths_core) was calling
store.add_history_item unconditionally, bypassing auto_dedup_enabled.
This file verifies the W1602 fix mirrors the W1572 dedup pattern.

Covers:
  - test_transcribe_paths_skips_duplicate_when_dedup_enabled
  - test_transcribe_paths_persists_when_dedup_disabled
  - test_transcribe_paths_persists_when_no_deduplicator_attached
  - test_transcribe_paths_includes_skipped_count_in_response
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import wave
import struct
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.auto_deduplication import DedupResult
from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _write_wav(path: Path, text: str = "audio") -> None:
    """Write a minimal valid WAV file so soundfile.info() can read it."""
    n_samples = 1600  # 0.1 s at 16 kHz
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(struct.pack("<" + "h" * n_samples, *([0] * n_samples)))


class _FakeTranslator:
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


class _FakeTranscriber:
    """Fake transcriber that returns configurable text."""

    def __init__(self, text="hello world"):
        self._text = text

    def transcribe(self, audio_path, **kwargs):
        return {"text": self._text, "confidence": 0.9, "engine": "fake"}

    # _transcribe_paths_core may call transcriber.engine.set_quality_profile
    # when a progress_callback is supplied — keep engine attribute present.
    @property
    def engine(self):
        eng = MagicMock()
        eng.transcribe.return_value = {"text": self._text, "confidence": 0.9, "engine": "fake"}
        return eng


class _SettingsSvc:
    def __init__(self, dedup_enabled=True, privacy_mode=False):
        self._dedup = dedup_enabled
        self._privacy = privacy_mode

    def cached_settings(self):
        return {
            "auto_dedup_enabled": self._dedup,
            "privacy_mode_enabled": self._privacy,
        }

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_service(
    tmp_dir,
    *,
    dedup_enabled: bool = True,
    privacy_mode: bool = False,
    auto_deduplicator=None,
    transcription_text: str = "hello world",
):
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.load.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None

    return RecordingCoreService(
        recorder=MagicMock(),
        transcriber=_FakeTranscriber(text=transcription_text),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_SettingsSvc(dedup_enabled=dedup_enabled, privacy_mode=privacy_mode),
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
        auto_deduplicator=auto_deduplicator,
    )


def _duplicate_deduplicator(original_id: str = "orig-001", similarity: float = 0.97):
    """AutoDeduplicator mock that always reports a duplicate."""
    mock = MagicMock()
    mock.check_duplicate.return_value = DedupResult(
        is_duplicate=True,
        duplicate_of=original_id,
        similarity=similarity,
        action_taken="skipped",
    )
    return mock


def _non_duplicate_deduplicator():
    """AutoDeduplicator mock that never reports a duplicate."""
    mock = MagicMock()
    mock.check_duplicate.return_value = DedupResult(
        is_duplicate=False,
        duplicate_of=None,
        similarity=0.0,
        action_taken="kept",
    )
    return mock


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestTranscribePathsDedupEnabled(unittest.TestCase):
    """W1602: dedup guard in _transcribe_paths_core when auto_dedup_enabled=True."""

    def test_transcribe_paths_skips_duplicate_when_dedup_enabled(self):
        """Duplicate file must NOT be added to history when dedup is enabled."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            _write_wav(wav)

            dedup = _duplicate_deduplicator()
            svc = _make_service(tmp, dedup_enabled=True, auto_deduplicator=dedup)

            result = svc._transcribe_paths_core({"paths": [str(wav)]})

        # Item was skipped — not added to items list
        self.assertEqual(result["processed"], 0)
        self.assertEqual(len(result["items"]), 0)
        # check_duplicate must have been called
        dedup.check_duplicate.assert_called_once()

    def test_transcribe_paths_persists_when_dedup_disabled(self):
        """Item must be persisted when auto_dedup_enabled=False even if deduplicator attached."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            _write_wav(wav)

            dedup = _duplicate_deduplicator()
            svc = _make_service(tmp, dedup_enabled=False, auto_deduplicator=dedup)

            result = svc._transcribe_paths_core({"paths": [str(wav)]})

        # Persisted because dedup was disabled
        self.assertEqual(result["processed"], 1)
        self.assertEqual(len(result["items"]), 1)
        # check_duplicate must NOT have been called
        dedup.check_duplicate.assert_not_called()

    def test_transcribe_paths_persists_when_no_deduplicator_attached(self):
        """Item must be persisted when no auto_deduplicator is set at all."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            _write_wav(wav)

            svc = _make_service(tmp, dedup_enabled=True, auto_deduplicator=None)

            result = svc._transcribe_paths_core({"paths": [str(wav)]})

        # No deduplicator → persisted regardless of flag
        self.assertEqual(result["processed"], 1)
        self.assertEqual(len(result["items"]), 1)

    def test_transcribe_paths_includes_skipped_count_in_response(self):
        """Return payload must include skipped_duplicates count."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            _write_wav(wav)

            dedup = _duplicate_deduplicator()
            svc = _make_service(tmp, dedup_enabled=True, auto_deduplicator=dedup)

            result = svc._transcribe_paths_core({"paths": [str(wav)]})

        self.assertIn("skipped_duplicates", result)
        self.assertEqual(result["skipped_duplicates"], 1)

    def test_transcribe_paths_skipped_count_zero_when_no_duplicates(self):
        """skipped_duplicates must be 0 when nothing is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            _write_wav(wav)

            dedup = _non_duplicate_deduplicator()
            svc = _make_service(tmp, dedup_enabled=True, auto_deduplicator=dedup)

            result = svc._transcribe_paths_core({"paths": [str(wav)]})

        self.assertEqual(result["skipped_duplicates"], 0)
        # Item must have been persisted
        self.assertEqual(result["processed"], 1)

    def test_transcribe_paths_dedup_check_exception_falls_through_to_persist(self):
        """If dedup check raises, item must still be persisted (soft-fail)."""
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            _write_wav(wav)

            dedup = MagicMock()
            dedup.check_duplicate.side_effect = RuntimeError("dedup boom")
            svc = _make_service(tmp, dedup_enabled=True, auto_deduplicator=dedup)

            result = svc._transcribe_paths_core({"paths": [str(wav)]})

        # Exception → soft-fail → item persisted
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["skipped_duplicates"], 0)

    def test_transcribe_paths_multiple_files_counts_duplicates_correctly(self):
        """Multiple files: duplicate ones counted, non-duplicates persisted."""
        with tempfile.TemporaryDirectory() as tmp:
            wav1 = Path(tmp) / "clip1.wav"
            wav2 = Path(tmp) / "clip2.wav"
            _write_wav(wav1)
            _write_wav(wav2)

            call_count = [0]
            def _side_effect(**kwargs):
                call_count[0] += 1
                # First call → duplicate, second → not duplicate
                if call_count[0] == 1:
                    return DedupResult(is_duplicate=True, duplicate_of="x", similarity=0.98, action_taken="skipped")
                return DedupResult(is_duplicate=False, duplicate_of=None, similarity=0.0, action_taken="kept")

            dedup = MagicMock()
            dedup.check_duplicate.side_effect = _side_effect
            svc = _make_service(tmp, dedup_enabled=True, auto_deduplicator=dedup)

            result = svc._transcribe_paths_core({"paths": [str(wav1), str(wav2)]})

        self.assertEqual(result["skipped_duplicates"], 1)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(len(result["items"]), 1)


if __name__ == "__main__":
    unittest.main()
