"""Unit-тесты для TranscriptVersionManager."""

from __future__ import annotations
from backend.transcript_versioning import TranscriptVersionManager, VALID_SOURCES

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TranscriptVersioningBasicTestCase(unittest.TestCase):
    """Базовые операции: save, get, list."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = TranscriptVersionManager(data_dir=self._tmpdir)

    # ------------------------------------------------------------------
    # save_version
    # ------------------------------------------------------------------

    def test_save_version_returns_dict_with_expected_fields(self) -> None:
        result = self._mgr.save_version("item_001", "Hello world", source="manual")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["item_id"], "item_001")
        self.assertEqual(result["text"], "Hello world")
        self.assertEqual(result["source"], "manual")
        self.assertEqual(result["version_num"], 1)
        self.assertIn("created_at", result)

    def test_save_version_increments_version_num(self) -> None:
        r1 = self._mgr.save_version("item_002", "First", source="stt_raw")
        r2 = self._mgr.save_version("item_002", "Second", source="stt_cleaned")
        r3 = self._mgr.save_version("item_002", "Third", source="manual")
        self.assertEqual(r1["version_num"], 1)
        self.assertEqual(r2["version_num"], 2)
        self.assertEqual(r3["version_num"], 3)

    def test_save_version_different_items_independent_numbering(self) -> None:
        ra1 = self._mgr.save_version("item_A", "Text A1", source="manual")
        rb1 = self._mgr.save_version("item_B", "Text B1", source="manual")
        ra2 = self._mgr.save_version("item_A", "Text A2", source="manual")
        self.assertEqual(ra1["version_num"], 1)
        self.assertEqual(rb1["version_num"], 1)
        self.assertEqual(ra2["version_num"], 2)

    def test_save_version_empty_item_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.save_version("", "Some text", source="manual")

    def test_save_version_whitespace_item_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.save_version("   ", "Some text", source="manual")

    def test_save_version_invalid_source_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.save_version("item_003", "Text", source="unknown_source")

    def test_save_version_all_valid_sources(self) -> None:
        for idx, source in enumerate(sorted(VALID_SOURCES)):
            result = self._mgr.save_version(f"item_src_{idx}", "Text", source=source)
            self.assertEqual(result["source"], source)

    def test_save_version_empty_text_allowed(self) -> None:
        # Пустой текст валиден — может быть результатом STT (тишина)
        result = self._mgr.save_version("item_004", "", source="stt_raw")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["version_num"], 1)

    # ------------------------------------------------------------------
    # get_versions
    # ------------------------------------------------------------------

    def test_get_versions_newest_first(self) -> None:
        self._mgr.save_version("item_005", "v1 text", source="stt_raw")
        self._mgr.save_version("item_005", "v2 text", source="stt_cleaned")
        self._mgr.save_version("item_005", "v3 text", source="manual")
        versions = self._mgr.get_versions("item_005")
        nums = [v["version_num"] for v in versions]
        self.assertEqual(nums, [3, 2, 1])

    def test_get_versions_empty_for_unknown_item(self) -> None:
        versions = self._mgr.get_versions("nonexistent_item")
        self.assertEqual(versions, [])

    def test_get_versions_does_not_return_other_items(self) -> None:
        self._mgr.save_version("item_X", "X text", source="manual")
        self._mgr.save_version("item_Y", "Y text", source="manual")
        versions_x = self._mgr.get_versions("item_X")
        self.assertEqual(len(versions_x), 1)
        self.assertTrue(all(v["item_id"] == "item_X" for v in versions_x))

    # ------------------------------------------------------------------
    # get_version
    # ------------------------------------------------------------------

    def test_get_version_by_num_returns_correct_record(self) -> None:
        self._mgr.save_version("item_006", "First version", source="stt_raw")
        self._mgr.save_version("item_006", "Second version", source="llm_rewrite")
        v1 = self._mgr.get_version("item_006", 1)
        v2 = self._mgr.get_version("item_006", 2)
        self.assertEqual(v1["text"], "First version")
        self.assertEqual(v2["text"], "Second version")

    def test_get_version_not_found_raises_key_error(self) -> None:
        self._mgr.save_version("item_007", "Some text", source="manual")
        with self.assertRaises(KeyError):
            self._mgr.get_version("item_007", 99)

    def test_get_version_unknown_item_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.get_version("nonexistent", 1)

    # ------------------------------------------------------------------
    # revert_to_version
    # ------------------------------------------------------------------

    def test_revert_creates_new_version(self) -> None:
        self._mgr.save_version("item_008", "Original", source="stt_raw")
        self._mgr.save_version("item_008", "Edited", source="manual")
        revert_result = self._mgr.revert_to_version("item_008", 1)
        self.assertEqual(revert_result["version_num"], 3)
        self.assertEqual(revert_result["text"], "Original")
        self.assertEqual(revert_result["reverted_from"], 1)

    def test_revert_preserves_all_previous_versions(self) -> None:
        self._mgr.save_version("item_009", "v1", source="stt_raw")
        self._mgr.save_version("item_009", "v2", source="manual")
        self._mgr.revert_to_version("item_009", 1)
        versions = self._mgr.get_versions("item_009")
        self.assertEqual(len(versions), 3)

    def test_revert_nonexistent_version_raises_key_error(self) -> None:
        self._mgr.save_version("item_010", "Only version", source="manual")
        with self.assertRaises(KeyError):
            self._mgr.revert_to_version("item_010", 999)

    # ------------------------------------------------------------------
    # diff_versions
    # ------------------------------------------------------------------

    def test_diff_versions_returns_expected_structure(self) -> None:
        self._mgr.save_version("item_011", "Hello world", source="stt_raw")
        self._mgr.save_version("item_011", "Hello earth", source="manual")
        diff = self._mgr.diff_versions("item_011", 1, 2)
        self.assertEqual(diff["item_id"], "item_011")
        self.assertEqual(diff["v1"], 1)
        self.assertEqual(diff["v2"], 2)
        self.assertEqual(diff["text_v1"], "Hello world")
        self.assertEqual(diff["text_v2"], "Hello earth")
        self.assertIn("unified_diff", diff)
        self.assertIn("added_lines", diff)
        self.assertIn("removed_lines", diff)

    def test_diff_identical_texts_no_changes(self) -> None:
        self._mgr.save_version("item_012", "Same text", source="stt_raw")
        self._mgr.save_version("item_012", "Same text", source="manual")
        diff = self._mgr.diff_versions("item_012", 1, 2)
        self.assertEqual(diff["added_lines"], 0)
        self.assertEqual(diff["removed_lines"], 0)

    def test_diff_versions_nonexistent_raises_key_error(self) -> None:
        self._mgr.save_version("item_013", "Some text", source="manual")
        with self.assertRaises(KeyError):
            self._mgr.diff_versions("item_013", 1, 99)

    def test_diff_counts_changes_correctly(self) -> None:
        self._mgr.save_version("item_014", "line one\nline two\nline three", source="stt_raw")
        self._mgr.save_version("item_014", "line one\nline two modified\nline three\nline four", source="manual")
        diff = self._mgr.diff_versions("item_014", 1, 2)
        self.assertGreater(diff["added_lines"], 0)
        self.assertGreater(diff["removed_lines"], 0)

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def test_versions_persist_across_manager_instances(self) -> None:
        self._mgr.save_version("item_015", "Persistent text", source="import")
        # Создаём новый менеджер с тем же data_dir
        new_mgr = TranscriptVersionManager(data_dir=self._tmpdir)
        versions = new_mgr.get_versions("item_015")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["text"], "Persistent text")
        self.assertEqual(versions[0]["source"], "import")

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def test_ipc_save_transcript_version_basic(self) -> None:
        result = self._mgr.handle_save_transcript_version({
            "item_id": "item_ipc_1",
            "text": "IPC saved text",
            "source": "manual",
        })
        self.assertEqual(result["item_id"], "item_ipc_1")
        self.assertEqual(result["text"], "IPC saved text")
        self.assertEqual(result["version_num"], 1)

    def test_ipc_save_transcript_version_default_source(self) -> None:
        result = self._mgr.handle_save_transcript_version({
            "item_id": "item_ipc_2",
            "text": "Default source",
        })
        self.assertEqual(result["source"], "manual")

    def test_ipc_save_transcript_version_missing_item_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.handle_save_transcript_version({"text": "Some text"})

    def test_ipc_save_transcript_version_missing_text_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.handle_save_transcript_version({"item_id": "item_ipc_3"})

    def test_ipc_get_transcript_versions_basic(self) -> None:
        self._mgr.save_version("item_ipc_4", "v1", source="stt_raw")
        self._mgr.save_version("item_ipc_4", "v2", source="manual")
        result = self._mgr.handle_get_transcript_versions({"item_id": "item_ipc_4"})
        self.assertEqual(result["item_id"], "item_ipc_4")
        self.assertEqual(result["total"], 2)
        self.assertIsInstance(result["versions"], list)
        self.assertEqual(len(result["versions"]), 2)

    def test_ipc_get_transcript_versions_missing_item_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.handle_get_transcript_versions({})

    def test_ipc_revert_transcript_version_basic(self) -> None:
        self._mgr.save_version("item_ipc_5", "Original v1", source="stt_raw")
        self._mgr.save_version("item_ipc_5", "Edited v2", source="manual")
        result = self._mgr.handle_revert_transcript_version({
            "item_id": "item_ipc_5",
            "version_num": 1,
        })
        self.assertEqual(result["text"], "Original v1")
        self.assertEqual(result["version_num"], 3)
        self.assertEqual(result["reverted_from"], 1)

    def test_ipc_revert_missing_version_num_raises(self) -> None:
        self._mgr.save_version("item_ipc_6", "Text", source="manual")
        with self.assertRaises(ValueError):
            self._mgr.handle_revert_transcript_version({"item_id": "item_ipc_6"})

    def test_ipc_revert_missing_item_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.handle_revert_transcript_version({"version_num": 1})


class TranscriptVersioningEdgeCasesTestCase(unittest.TestCase):
    """Граничные случаи."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._mgr = TranscriptVersionManager(data_dir=self._tmpdir)

    def test_multiline_text_preserved(self) -> None:
        text = "Line 1\nLine 2\nLine 3"
        _result = self._mgr.save_version("item_ml", text, source="import")  # noqa: F841
        retrieved = self._mgr.get_version("item_ml", 1)
        self.assertEqual(retrieved["text"], text)

    def test_unicode_text_preserved(self) -> None:
        text = "Привет мир! Это транскрипция на русском языке."
        _result = self._mgr.save_version("item_uni", text, source="stt_cleaned")  # noqa: F841
        retrieved = self._mgr.get_version("item_uni", 1)
        self.assertEqual(retrieved["text"], text)

    def test_created_at_is_iso8601(self) -> None:
        result = self._mgr.save_version("item_ts", "Text", source="manual")
        created_at = result["created_at"]
        # ISO8601 должен содержать T и +
        self.assertIn("T", created_at)

    def test_many_versions_ordering(self) -> None:
        for i in range(10):
            self._mgr.save_version("item_many", f"version {i + 1}", source="manual")
        versions = self._mgr.get_versions("item_many")
        nums = [v["version_num"] for v in versions]
        self.assertEqual(nums, list(range(10, 0, -1)))

    def test_diff_reverse_order_works(self) -> None:
        self._mgr.save_version("item_rev", "First", source="stt_raw")
        self._mgr.save_version("item_rev", "Second", source="manual")
        # v2 как база, v1 как новая — допустимо
        diff = self._mgr.diff_versions("item_rev", 2, 1)
        self.assertEqual(diff["text_v1"], "Second")
        self.assertEqual(diff["text_v2"], "First")


if __name__ == "__main__":
    unittest.main()
