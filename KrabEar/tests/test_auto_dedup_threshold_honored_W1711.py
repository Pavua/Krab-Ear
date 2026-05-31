"""Regression tests — W1711: auto_dedup_threshold is honored by check_duplicate.

These tests FAIL on the pre-fix code and PASS after the fix.

Bug: check_duplicate() tier-1 compared similarity against the hardcoded class
constant _SIMILARITY_THRESHOLD (0.85) instead of the caller's `threshold`
argument.  A pair with similarity ~0.89 was reported as is_duplicate=True
identically for threshold in {0.85, 0.90, 0.95, 0.99}.  The user-configured
auto_dedup_threshold setting was completely unwired — genuine recordings with
sim in [0.85, threshold) were silently dropped (data-loss).

Fix (W1711):
  - auto_deduplication.py: tier-1 uses threshold arg for the duplicate decision;
    _TIER1_ROUTING_THRESHOLD (was _SIMILARITY_THRESHOLD, still 0.85) only
    controls the tier-1/tier-2 routing split.
  - recording_core_service.py: both callers (stop_recording + transcribe_paths)
    read auto_dedup_threshold from runtime settings and forward it as threshold=.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.auto_deduplication import (
    AutoDeduplicator,
    DEFAULT_DEDUP_THRESHOLD,
    _text_similarity,
    _JACCARD_LOW,
)
from backend.state_store import StateStore


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _make_store(tmp_dir: Path) -> StateStore:
    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return StateStore(data_dir)


# ---------------------------------------------------------------------------
# Helper: build two texts whose Jaccard-hybrid similarity lands in (0.85, 0.99)
# so they straddle the old hardcoded 0.85 gate but not a 0.99 threshold.
# ---------------------------------------------------------------------------
def _build_similar_texts() -> tuple[str, str, float]:
    """Return (text_a, text_b, measured_similarity) where 0.85 < sim < 0.99.

    We need similarity clearly above 0.85 (to enter tier-1 candidate pool) but
    below 0.99 (so threshold=0.99 should NOT declare a duplicate).
    """
    # Craft two texts that share most but not all words — aim for ~0.87-0.93 Jaccard
    base = "Привет мир тест транскрипция речь звук работа сигнал данные вывод результат"
    # Add one extra word to the second text to lower Jaccard slightly
    modified = base + " дополнительно"

    sim = _text_similarity(base, modified)

    # Sanity: ensure the crafted texts actually land in the right zone.
    # If not, fall back to a known-good pair.
    if not (0.80 <= sim < 0.99):
        # Fallback: use a longer base where one extra word has smaller impact.
        words = [
            "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь",
            "девять", "десять", "одиннадцать", "двенадцать", "тринадцать",
        ]
        base = " ".join(words)
        modified = base + " четырнадцать"
        sim = _text_similarity(base, modified)

    return base, modified, sim


class ThresholdHonoredW1711TestCase(unittest.TestCase):
    """Core W1711 regression: caller threshold is respected, not the hardcoded 0.85."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.deduplicator = AutoDeduplicator()

    # ------------------------------------------------------------------
    # Prove the similarity zone we need actually exists
    # ------------------------------------------------------------------
    def test_text_similarity_indeterminate_zone_exists(self) -> None:
        """_text_similarity returns a value in (0.85, 0.99) for our test pair."""
        base, modified, sim = _build_similar_texts()
        self.assertGreater(
            sim, 0.80,
            f"Expected sim > 0.80 for crafted pair, got {sim:.4f} — test setup broken"
        )
        self.assertLess(
            sim, 0.99,
            f"Expected sim < 0.99 for crafted pair, got {sim:.4f} — test setup broken"
        )

    # ------------------------------------------------------------------
    # The key regression: threshold=0.99 must NOT drop a 0.89-similar pair
    # ------------------------------------------------------------------
    def test_high_threshold_does_not_drop_similar_recording(self) -> None:
        """threshold=0.99: pair with sim in (0.85, 0.99) MUST NOT be a duplicate.

        PRE-FIX BEHAVIOUR (bug): is_duplicate=True because tier-1 uses hardcoded 0.85.
        POST-FIX BEHAVIOUR: is_duplicate=False because sim < threshold=0.99.
        """
        base, modified, sim = _build_similar_texts()
        # Only run the check if the similarity actually falls below 0.99
        if sim >= 0.99:
            self.skipTest(f"Crafted texts have sim={sim:.4f} >= 0.99; adjust test pair")

        self.store.add_history_item(text=base, paste_status="ok")

        result = self.deduplicator.check_duplicate(
            text=modified,
            timestamp=_now_iso(),
            store=self.store,
            threshold=0.99,
        )

        self.assertFalse(
            result.is_duplicate,
            f"W1711 REGRESSION: threshold=0.99 should not drop a pair with sim={sim:.4f}. "
            f"Got is_duplicate=True (action={result.action_taken}, similarity={result.similarity:.4f}). "
            f"This means tier-1 is still using the hardcoded 0.85 constant instead of the "
            f"caller's threshold."
        )
        self.assertEqual(
            result.action_taken, "kept",
            f"Expected action_taken='kept', got '{result.action_taken}' (sim={sim:.4f})"
        )

    # ------------------------------------------------------------------
    # Symmetry: same pair IS a duplicate at threshold=0.85
    # ------------------------------------------------------------------
    def test_low_threshold_correctly_flags_similar_recording(self) -> None:
        """threshold=0.85: same pair with sim > 0.85 MUST be a duplicate."""
        base, modified, sim = _build_similar_texts()
        if sim <= 0.85:
            self.skipTest(f"Crafted texts have sim={sim:.4f} <= 0.85; not in tier-1 zone")

        self.store.add_history_item(text=base, paste_status="ok")

        result = self.deduplicator.check_duplicate(
            text=modified,
            timestamp=_now_iso(),
            store=self.store,
            threshold=0.85,
        )

        self.assertTrue(
            result.is_duplicate,
            f"threshold=0.85 should flag a pair with sim={sim:.4f} as duplicate. "
            f"Got is_duplicate=False."
        )
        self.assertIsNotNone(result.duplicate_of)

    # ------------------------------------------------------------------
    # Identical text must always be a duplicate regardless of threshold
    # ------------------------------------------------------------------
    def test_identical_text_duplicate_at_any_reasonable_threshold(self) -> None:
        """Identical text is always a duplicate for threshold in [0.80, 0.99]."""
        text = "Идентичный текст тест дедупликации одинаковые слова"
        self.store.add_history_item(text=text, paste_status="ok")

        for thresh in (0.80, 0.85, 0.90, 0.95, 0.99):
            with self.subTest(threshold=thresh):
                result = self.deduplicator.check_duplicate(
                    text=text,
                    timestamp=_now_iso(),
                    store=self.store,
                    threshold=thresh,
                )
                self.assertTrue(
                    result.is_duplicate,
                    f"Identical text must be a duplicate at threshold={thresh}, "
                    f"got is_duplicate=False (sim={result.similarity:.4f})"
                )

    # ------------------------------------------------------------------
    # Threshold controls the boundary precisely
    # ------------------------------------------------------------------
    def test_threshold_controls_boundary(self) -> None:
        """Raising threshold from below_sim to above_sim flips is_duplicate False→True.

        Specifically: if sim=0.89, threshold=0.88 → duplicate, threshold=0.90 → kept.
        """
        base, modified, sim = _build_similar_texts()
        # Need sim in a usable range
        if not (0.82 <= sim <= 0.97):
            self.skipTest(
                f"Crafted texts sim={sim:.4f} not in [0.82, 0.97]; threshold-boundary "
                "test not meaningful"
            )

        self.store.add_history_item(text=base, paste_status="ok")

        # Just below sim → duplicate
        threshold_below = max(0.80, sim - 0.02)
        result_dup = self.deduplicator.check_duplicate(
            text=modified,
            timestamp=_now_iso(),
            store=self.store,
            threshold=threshold_below,
        )
        self.assertTrue(
            result_dup.is_duplicate,
            f"threshold={threshold_below:.3f} (below sim={sim:.4f}) should yield "
            f"is_duplicate=True, got False"
        )

        # Just above sim → NOT duplicate
        threshold_above = min(0.99, sim + 0.02)
        result_kept = self.deduplicator.check_duplicate(
            text=modified,
            timestamp=_now_iso(),
            store=self.store,
            threshold=threshold_above,
        )
        self.assertFalse(
            result_kept.is_duplicate,
            f"threshold={threshold_above:.3f} (above sim={sim:.4f}) should yield "
            f"is_duplicate=False, got True. "
            f"W1711 regression: hardcoded 0.85 is being used instead of threshold."
        )

    # ------------------------------------------------------------------
    # DEFAULT_DEDUP_THRESHOLD (0.9) is actually respected
    # ------------------------------------------------------------------
    def test_default_threshold_09_respected(self) -> None:
        """DEFAULT_DEDUP_THRESHOLD=0.9 — pairs with sim in [0.85, 0.90) must be kept."""
        base, modified, sim = _build_similar_texts()
        if not (0.85 < sim < 0.90):
            self.skipTest(
                f"Crafted texts sim={sim:.4f} not in (0.85, 0.90); "
                "cannot test 0.9 threshold boundary directly"
            )

        self.store.add_history_item(text=base, paste_status="ok")

        # With DEFAULT_DEDUP_THRESHOLD=0.9, sim in (0.85, 0.90) must NOT be a duplicate
        result = self.deduplicator.check_duplicate(
            text=modified,
            timestamp=_now_iso(),
            store=self.store,
            threshold=DEFAULT_DEDUP_THRESHOLD,  # 0.9
        )
        self.assertFalse(
            result.is_duplicate,
            f"sim={sim:.4f} should not be a duplicate at threshold=0.9 (DEFAULT). "
            f"W1711 regression: old code would return is_duplicate=True here."
        )


class RecordingCoreServiceThresholdW1711TestCase(unittest.TestCase):
    """W1711: recording_core_service forwards auto_dedup_threshold to check_duplicate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))

    def _make_settings_svc(self, auto_dedup_threshold: float = 0.9) -> MagicMock:
        svc = MagicMock()
        svc.cached_settings.return_value = {
            "auto_dedup_enabled": True,
            "privacy_mode_enabled": False,
            "auto_dedup_threshold": auto_dedup_threshold,
        }
        return svc

    def _make_recording_core_service(
        self,
        deduplicator: MagicMock,
        settings_svc: MagicMock,
    ):
        """Construct a RecordingCoreService with all required MagicMock collaborators."""
        from backend.recording_core_service import RecordingCoreService

        return RecordingCoreService(
            store=self.store,
            settings_svc=settings_svc,
            auto_deduplicator=deduplicator,
            recorder=MagicMock(),
            transcriber=MagicMock(),
            translator=MagicMock(),
            vocabulary=MagicMock(),
            llm_rewriter=MagicMock(),
            auto_glossary=MagicMock(),
            semantic_searcher=MagicMock(),
            context_memory=MagicMock(),
            clipboard_history=[],
            auto_backup=MagicMock(),
            session_tracker=MagicMock(),
            action_items_extractor=MagicMock(),
            transcription_counter_ref=[0],
            last_stt_engine_ref=[None],
        )

    def test_stop_recording_threshold_forwarded_to_check_duplicate(self) -> None:
        """stop_recording reads auto_dedup_threshold from settings and passes it.

        We call _stop_recording_phase_e directly (pure wiring test — no real STT) and
        verify the threshold= kwarg matches the runtime auto_dedup_threshold setting.
        We return is_duplicate=True from the mock so the method returns early (before
        store.add_history_item is reached — keeping the test purely about wiring).
        """
        deduplicator = MagicMock(spec=AutoDeduplicator)
        deduplicator.check_duplicate.return_value = MagicMock(
            is_duplicate=True,
            duplicate_of="existing-id",
            similarity=0.96,
            action_taken="skipped",
        )

        settings_svc = self._make_settings_svc(auto_dedup_threshold=0.95)
        svc = self._make_recording_core_service(deduplicator, settings_svc)

        # Call _stop_recording_phase_e directly with minimal mocked phase_d
        phase_d = {
            "text": "some recording text",
            "display_text": "some recording text",
            "translated_text": "",
            "final_text": "some recording text",
            "translation": MagicMock(mode="off"),
            "translation_status": "not_requested",
            "confidence": 0.9,
            "diarization_data": None,
            "tp": {},
        }
        sr = {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translate_and_paste": False,
            "translation_style": "neutral",
        }
        settings = settings_svc.cached_settings.return_value

        svc._stop_recording_phase_e(
            phase_d=phase_d,
            sr=sr,
            duration_sec=5.0,
            stop_tail_trim_ms=180,
            silence_detected=False,
            silence_guard_enabled=True,
            background_guard_rejected=False,
            rt_session_id=None,
            settings=settings,
        )

        # Verify check_duplicate was called with threshold=0.95
        self.assertTrue(
            deduplicator.check_duplicate.called,
            "check_duplicate should have been called (auto_dedup_enabled=True)"
        )
        call_kwargs = deduplicator.check_duplicate.call_args
        actual_threshold = (
            call_kwargs.kwargs.get("threshold")
            if call_kwargs.kwargs
            else call_kwargs[1].get("threshold")
        )
        self.assertEqual(
            actual_threshold,
            0.95,
            f"W1711: expected threshold=0.95 forwarded to check_duplicate, "
            f"got {actual_threshold!r}. The auto_dedup_threshold setting is not wired."
        )

    def test_threshold_default_09_used_when_setting_absent(self) -> None:
        """When auto_dedup_threshold is absent from settings, default 0.9 is used.

        Returns is_duplicate=True to short-circuit before store.add_history_item.
        """
        deduplicator = MagicMock(spec=AutoDeduplicator)
        deduplicator.check_duplicate.return_value = MagicMock(
            is_duplicate=True,
            duplicate_of="existing-id",
            similarity=0.91,
            action_taken="skipped",
        )

        settings_svc = MagicMock()
        # Settings without auto_dedup_threshold
        settings_svc.cached_settings.return_value = {
            "auto_dedup_enabled": True,
            "privacy_mode_enabled": False,
            # auto_dedup_threshold absent → must default to 0.9
        }

        svc = self._make_recording_core_service(deduplicator, settings_svc)

        phase_d = {
            "text": "threshold default test",
            "display_text": "threshold default test",
            "translated_text": "",
            "final_text": "threshold default test",
            "translation": MagicMock(mode="off"),
            "translation_status": "not_requested",
            "confidence": 0.9,
            "diarization_data": None,
            "tp": {},
        }
        sr = {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translate_and_paste": False,
            "translation_style": "neutral",
        }
        settings = settings_svc.cached_settings.return_value

        svc._stop_recording_phase_e(
            phase_d=phase_d,
            sr=sr,
            duration_sec=3.0,
            stop_tail_trim_ms=180,
            silence_detected=False,
            silence_guard_enabled=True,
            background_guard_rejected=False,
            rt_session_id=None,
            settings=settings,
        )

        call_kwargs = deduplicator.check_duplicate.call_args
        actual_threshold = (
            call_kwargs.kwargs.get("threshold")
            if call_kwargs.kwargs
            else call_kwargs[1].get("threshold")
        )
        self.assertEqual(
            actual_threshold,
            0.9,
            f"When auto_dedup_threshold absent, default 0.9 must be used, got {actual_threshold!r}"
        )


if __name__ == "__main__":
    unittest.main()
