"""W1646 — StateStore.add_history_item field coverage tests.

Verifies that all HistoryItem fields are properly passed through
add_history_item, specifically the 9 fields that were silently dropped
before the W1643 F2 HIGH fix:

Priority (HIGH):
  - privacy_mode  — privacy audit incomplete without this flag
  - audio_path    — re-transcription unreachable via standard write path
  - is_protected  — bulk-operation guard silently not set
  - reasoning     — Voxtral output silently dropped

Completeness (LOW/MED):
  - tags, favorite, action_items, decisions, questions
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402


def _make_store(tmp_dir: str) -> StateStore:
    return StateStore(Path(tmp_dir) / "data")


class TestAddHistoryItemPrivacyMode(unittest.TestCase):
    """privacy_mode persists through add_history_item (W1643 F2 HIGH)."""

    def test_add_history_item_persists_privacy_mode_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("hello", privacy_mode=True)
            self.assertTrue(item.privacy_mode)

    def test_add_history_item_persists_privacy_mode_false_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("hello")
            self.assertFalse(item.privacy_mode)

    def test_privacy_mode_survives_ndjson_round_trip(self):
        """privacy_mode=True must appear in the written NDJSON line."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("secret recording", privacy_mode=True)

            # Read back directly from disk
            lines = store.history_path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines, "history.ndjson should have content")
            raw = json.loads(lines[-1])
            self.assertEqual(raw["id"], item.id)
            self.assertTrue(raw.get("privacy_mode"), "privacy_mode must be True in NDJSON")

    def test_privacy_mode_false_in_ndjson(self):
        """privacy_mode=False must serialize as False (not missing key)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("normal recording", privacy_mode=False)

            lines = store.history_path.read_text(encoding="utf-8").splitlines()
            raw = json.loads(lines[-1])
            self.assertEqual(raw["id"], item.id)
            self.assertFalse(raw.get("privacy_mode", True))


class TestAddHistoryItemAudioPath(unittest.TestCase):
    """audio_path persists through add_history_item (W1643 F2 HIGH)."""

    def test_add_history_item_persists_audio_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("text", audio_path="/tmp/recording.m4a")
            self.assertEqual(item.audio_path, "/tmp/recording.m4a")

    def test_audio_path_empty_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("text")
            self.assertEqual(item.audio_path, "")

    def test_audio_path_survives_ndjson_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            path_val = "/Users/pablito/recordings/call_2026-05-30.wav"
            item = store.add_history_item("call", audio_path=path_val)

            lines = store.history_path.read_text(encoding="utf-8").splitlines()
            raw = json.loads(lines[-1])
            self.assertEqual(raw["id"], item.id)
            self.assertEqual(raw.get("audio_path"), path_val)


class TestAddHistoryItemIsProtectedAndReasoning(unittest.TestCase):
    """is_protected and reasoning persist through add_history_item (W1643 F2 HIGH)."""

    def test_add_history_item_persists_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("important", is_protected=True)
            self.assertTrue(item.is_protected)

    def test_is_protected_false_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("ordinary")
            self.assertFalse(item.is_protected)

    def test_add_history_item_persists_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            reasoning_text = "The caller mentioned budget cuts in Q3."
            item = store.add_history_item("transcript", reasoning=reasoning_text)
            self.assertEqual(item.reasoning, reasoning_text)

    def test_reasoning_none_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("text without reasoning")
            self.assertIsNone(item.reasoning)

    def test_is_protected_and_reasoning_ndjson_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item(
                "voxtral transcript",
                is_protected=True,
                reasoning="Summary: budget discussion.",
            )

            lines = store.history_path.read_text(encoding="utf-8").splitlines()
            raw = json.loads(lines[-1])
            self.assertEqual(raw["id"], item.id)
            self.assertTrue(raw.get("is_protected"))
            self.assertEqual(raw.get("reasoning"), "Summary: budget discussion.")


class TestAddHistoryItemBackwardCompat(unittest.TestCase):
    """Existing callers that omit new kwargs must continue to work (W1646)."""

    def test_backward_compat_no_new_kwargs(self):
        """Call with only the original 19 kwargs — must succeed without error."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item(
                text="Привет мир",
                paste_status="ok",
                source_text="hello world",
                translated_text="Привет мир",
                translation_mode="auto",
                source_lang="en",
                target_lang="ru",
                translation_status="ok",
                translation_engine="opus",
                chat_id="123",
                message_id="456",
                cleaned_text="Привет мир.",
                llm_applied=True,
                llm_latency_ms=200,
                diarization=None,
                audio_duration_sec=5.0,
                confidence=0.95,
                emotion="neutral",
                word_timestamps=None,
                speaker_turns=None,
            )
            # Defaults applied for new fields
            self.assertFalse(item.privacy_mode)
            self.assertEqual(item.audio_path, "")
            self.assertFalse(item.is_protected)
            self.assertIsNone(item.reasoning)
            self.assertEqual(item.tags, [])
            self.assertFalse(item.favorite)
            self.assertIsNone(item.action_items)
            self.assertIsNone(item.decisions)
            self.assertIsNone(item.questions)

    def test_backward_compat_text_only(self):
        """Minimal caller: text only."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("minimal")
            self.assertEqual(item.text, "minimal")
            self.assertFalse(item.privacy_mode)
            self.assertEqual(item.audio_path, "")
            self.assertFalse(item.is_protected)
            self.assertIsNone(item.reasoning)


class TestAddHistoryItemCompletenessFields(unittest.TestCase):
    """Completeness: tags, favorite, action_items, decisions, questions (W1646)."""

    def test_tags_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("tag test", tags=["work", "meeting"])
            self.assertEqual(item.tags, ["work", "meeting"])

    def test_favorite_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item("fav test", favorite=True)
            self.assertTrue(item.favorite)

    def test_action_items_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            actions = [{"text": "Send report", "priority": "high"}]
            item = store.add_history_item("meeting", action_items=actions)
            self.assertEqual(item.action_items, actions)

    def test_decisions_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            decisions = [{"text": "Approve budget"}]
            item = store.add_history_item("board meeting", decisions=decisions)
            self.assertEqual(item.decisions, decisions)

    def test_questions_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            questions = [{"text": "When is the deadline?"}]
            item = store.add_history_item("standup", questions=questions)
            self.assertEqual(item.questions, questions)

    def test_all_new_fields_together(self):
        """All 9 previously-missing fields passed together."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item(
                "full item",
                tags=["voxtral", "private"],
                favorite=True,
                audio_path="/tmp/audio.wav",
                is_protected=True,
                reasoning="Voxtral analysis result.",
                action_items=[{"text": "Follow up"}],
                decisions=[{"text": "Approved"}],
                questions=[{"text": "Timeline?"}],
                privacy_mode=True,
            )
            self.assertEqual(item.tags, ["voxtral", "private"])
            self.assertTrue(item.favorite)
            self.assertEqual(item.audio_path, "/tmp/audio.wav")
            self.assertTrue(item.is_protected)
            self.assertEqual(item.reasoning, "Voxtral analysis result.")
            self.assertEqual(item.action_items, [{"text": "Follow up"}])
            self.assertEqual(item.decisions, [{"text": "Approved"}])
            self.assertEqual(item.questions, [{"text": "Timeline?"}])
            self.assertTrue(item.privacy_mode)

    def test_all_new_fields_ndjson_round_trip(self):
        """All 9 new fields survive NDJSON serialisation."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            item = store.add_history_item(
                "full item",
                tags=["t1"],
                favorite=True,
                audio_path="/tmp/a.wav",
                is_protected=True,
                reasoning="reason text",
                action_items=[{"text": "do it"}],
                decisions=[{"text": "decided"}],
                questions=[{"text": "why?"}],
                privacy_mode=True,
            )
            lines = store.history_path.read_text(encoding="utf-8").splitlines()
            raw = json.loads(lines[-1])
            self.assertEqual(raw["id"], item.id)
            self.assertEqual(raw["tags"], ["t1"])
            self.assertTrue(raw["favorite"])
            self.assertEqual(raw["audio_path"], "/tmp/a.wav")
            self.assertTrue(raw["is_protected"])
            self.assertEqual(raw["reasoning"], "reason text")
            self.assertEqual(raw["action_items"], [{"text": "do it"}])
            self.assertEqual(raw["decisions"], [{"text": "decided"}])
            self.assertEqual(raw["questions"], [{"text": "why?"}])
            self.assertTrue(raw["privacy_mode"])


if __name__ == "__main__":
    unittest.main()
