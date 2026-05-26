"""Tests for StateStore.add_history_item() forwarding all 8 missing HistoryItem fields.

W1228 F1 MED fix verification.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore


class TestAddHistoryItemForwardsReasoning(unittest.TestCase):
    """test_add_history_item_forwards_reasoning"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_add_history_item_forwards_reasoning(self):
        item = self.store.add_history_item(
            text="Hello",
            reasoning="This is the reasoning output from Voxtral.",
        )
        self.assertEqual(item.reasoning, "This is the reasoning output from Voxtral.")

    def test_add_history_item_reasoning_default_is_none(self):
        item = self.store.add_history_item(text="Hello")
        self.assertIsNone(item.reasoning)


class TestAddHistoryItemForwardsAudioPath(unittest.TestCase):
    """test_add_history_item_forwards_audio_path"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_add_history_item_forwards_audio_path(self):
        item = self.store.add_history_item(
            text="Recording",
            audio_path="/tmp/recording.wav",
        )
        self.assertEqual(item.audio_path, "/tmp/recording.wav")

    def test_add_history_item_audio_path_default_is_empty(self):
        item = self.store.add_history_item(text="Hello")
        self.assertEqual(item.audio_path, "")


class TestAddHistoryItemForwardsIsProtected(unittest.TestCase):
    """test_add_history_item_forwards_is_protected"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_add_history_item_forwards_is_protected_true(self):
        item = self.store.add_history_item(
            text="Protected recording",
            is_protected=True,
        )
        self.assertTrue(item.is_protected)

    def test_add_history_item_is_protected_default_is_false(self):
        item = self.store.add_history_item(text="Hello")
        self.assertFalse(item.is_protected)


class TestAddHistoryItemForwardsTags(unittest.TestCase):
    """test_add_history_item_forwards_tags"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_add_history_item_forwards_tags(self):
        item = self.store.add_history_item(
            text="Tagged recording",
            tags=["meeting", "important"],
        )
        self.assertEqual(item.tags, ["meeting", "important"])

    def test_add_history_item_tags_default_is_empty_list(self):
        item = self.store.add_history_item(text="Hello")
        self.assertEqual(item.tags, [])


class TestAddHistoryItemForwardsFavoriteActionItemsDecisionsQuestions(unittest.TestCase):
    """test_add_history_item_forwards_favorite_action_items_decisions_questions"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmpdir.name))

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_add_history_item_forwards_favorite(self):
        item = self.store.add_history_item(
            text="Favorite recording",
            favorite=True,
        )
        self.assertTrue(item.favorite)

    def test_add_history_item_favorite_default_is_false(self):
        item = self.store.add_history_item(text="Hello")
        self.assertFalse(item.favorite)

    def test_add_history_item_forwards_action_items(self):
        action_items = [
            {"text": "Fix the bug", "assignee": "Alice", "due": "2026-06-01", "priority": "high"}
        ]
        item = self.store.add_history_item(
            text="Meeting recording",
            action_items=action_items,
        )
        self.assertEqual(item.action_items, action_items)

    def test_add_history_item_action_items_default_is_none(self):
        item = self.store.add_history_item(text="Hello")
        self.assertIsNone(item.action_items)

    def test_add_history_item_forwards_decisions(self):
        decisions = [{"text": "We will migrate to Postgres"}]
        item = self.store.add_history_item(
            text="Architecture meeting",
            decisions=decisions,
        )
        self.assertEqual(item.decisions, decisions)

    def test_add_history_item_decisions_default_is_none(self):
        item = self.store.add_history_item(text="Hello")
        self.assertIsNone(item.decisions)

    def test_add_history_item_forwards_questions(self):
        questions = [{"text": "Who is responsible for deployment?"}]
        item = self.store.add_history_item(
            text="Q&A recording",
            questions=questions,
        )
        self.assertEqual(item.questions, questions)

    def test_add_history_item_questions_default_is_none(self):
        item = self.store.add_history_item(text="Hello")
        self.assertIsNone(item.questions)

    def test_add_history_item_all_8_fields_together(self):
        """All 8 previously missing fields forwarded in a single call."""
        item = self.store.add_history_item(
            text="Full meeting recording",
            reasoning="Voxtral output",
            audio_path="/var/audio/meet.wav",
            is_protected=True,
            tags=["meeting", "q3"],
            favorite=True,
            action_items=[{"text": "Deploy", "assignee": "Bob", "due": "", "priority": "low"}],
            decisions=[{"text": "Use gRPC"}],
            questions=[{"text": "Timeline?"}],
        )
        self.assertEqual(item.reasoning, "Voxtral output")
        self.assertEqual(item.audio_path, "/var/audio/meet.wav")
        self.assertTrue(item.is_protected)
        self.assertEqual(item.tags, ["meeting", "q3"])
        self.assertTrue(item.favorite)
        self.assertIsNotNone(item.action_items)
        self.assertEqual(len(item.action_items), 1)
        self.assertIsNotNone(item.decisions)
        self.assertEqual(len(item.decisions), 1)
        self.assertIsNotNone(item.questions)
        self.assertEqual(len(item.questions), 1)


class TestAddHistoryItemSignatureAST(unittest.TestCase):
    """AST check: verify the 8 new params exist in add_history_item signature."""

    def test_signature_contains_all_8_new_params(self):
        state_store_path = Path(__file__).parent.parent / "backend" / "state_store.py"
        source = state_store_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "add_history_item":
                func_node = node
                break

        self.assertIsNotNone(func_node, "add_history_item not found in state_store.py")

        param_names = {arg.arg for arg in func_node.args.args}
        param_names.update(arg.arg for arg in func_node.args.kwonlyargs)

        expected_new_params = {
            "reasoning",
            "audio_path",
            "is_protected",
            "tags",
            "favorite",
            "action_items",
            "decisions",
            "questions",
        }
        missing = expected_new_params - param_names
        self.assertEqual(
            missing,
            set(),
            f"add_history_item is missing parameters: {missing}",
        )

    def test_create_call_passes_all_8_new_params(self):
        """Verify HistoryItem.create() is called with all 8 new kwargs."""
        state_store_path = Path(__file__).parent.parent / "backend" / "state_store.py"
        source = state_store_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        func_node = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "add_history_item":
                func_node = node
                break

        self.assertIsNotNone(func_node, "add_history_item not found in state_store.py")

        # Find the HistoryItem.create() call inside add_history_item
        create_call = None
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "create"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "HistoryItem"
                ):
                    create_call = node
                    break

        self.assertIsNotNone(create_call, "HistoryItem.create() call not found in add_history_item")

        passed_kwargs = {kw.arg for kw in create_call.keywords}
        expected_new_kwargs = {
            "reasoning",
            "audio_path",
            "is_protected",
            "tags",
            "favorite",
            "action_items",
            "decisions",
            "questions",
        }
        missing = expected_new_kwargs - passed_kwargs
        self.assertEqual(
            missing,
            set(),
            f"HistoryItem.create() call is missing kwargs: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
