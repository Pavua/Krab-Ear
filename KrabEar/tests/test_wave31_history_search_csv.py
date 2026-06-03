"""Wave-31 regression tests: history_service fixes E1/E2/E3.

E1 (MED) — handle_search_history privacy gate
    privacy_mode_enabled=True → returns empty items list with reason=privacy_mode_active,
    consistent with handle_search_with_highlights and handle_fuzzy_search siblings.

E2 (MED) — CSV export formula injection in text/translation columns
    Text/translation cells starting with =, +, -, @, |, % are prefixed with ' to
    defuse spreadsheet formula execution (mirrors wave-27 fix for speaker column).

E3 (LOW) — CSV export lang/duration columns always empty
    to_dict() uses source_lang and audio_duration_sec keys; the old code read the
    non-existent "lang" and "duration" keys, always producing empty cells.
"""

from __future__ import annotations

import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from backend.history_service import HistoryService
    from backend.state_store import StateStore
    _SKIP = False
except ImportError:
    _SKIP = True


def _make_service(tmp_dir: str) -> tuple[HistoryService, StateStore]:
    store = StateStore(Path(tmp_dir) / "data")
    svc = HistoryService(store=store)
    return svc, store


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class SearchHistoryPrivacyGateTestCase(unittest.TestCase):
    """E1: handle_search_history must gate on privacy_mode_enabled."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.store = _make_service(self.tmp.name)
        # Seed one item so a non-privacy search would return results.
        self.svc.handle_add_history_item({"text": "секретный разговор", "paste_status": "ok"})

    # ------------------------------------------------------------------
    # E1-a: privacy mode ON → empty items
    # ------------------------------------------------------------------
    def test_search_history_returns_empty_in_privacy_mode(self) -> None:
        self.store.save_settings({"privacy_mode_enabled": True})
        result = self.svc.handle_search_history({"query": "секретный"})
        self.assertEqual(result.get("items"), [],
                         "items must be empty list in privacy mode")
        self.assertEqual(result.get("total"), 0,
                         "total must be 0 in privacy mode")
        self.assertEqual(result.get("reason"), "privacy_mode_active",
                         "reason must be privacy_mode_active")
        # ok=True — not an error
        self.assertTrue(result.get("ok", True))

    # ------------------------------------------------------------------
    # E1-b: privacy mode OFF → normal results returned
    # ------------------------------------------------------------------
    def test_search_history_works_when_privacy_off(self) -> None:
        self.store.save_settings({"privacy_mode_enabled": False})
        result = self.svc.handle_search_history({"query": "секретный"})
        # Should return items normally (not gated)
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")
        # items key present and is a list
        self.assertIn("items", result)
        self.assertIsInstance(result["items"], list)

    # ------------------------------------------------------------------
    # E1-c: privacy mode ON + empty query → still gated
    # ------------------------------------------------------------------
    def test_search_history_empty_query_gated_in_privacy_mode(self) -> None:
        self.store.save_settings({"privacy_mode_enabled": True})
        result = self.svc.handle_search_history({"query": ""})
        self.assertEqual(result.get("items"), [])
        self.assertEqual(result.get("reason"), "privacy_mode_active")


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class CSVFormulaInjectionTestCase(unittest.TestCase):
    """E2: text and translation columns must be formula-neutralized in CSV export."""

    # Formula-leading characters that must be prefixed with '
    _FORMULA_CHARS = ('=', '+', '-', '@', '|', '%')

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.store = _make_service(self.tmp.name)

    def _export_csv_rows(self, copy_to_clipboard: bool = False) -> list[dict[str, str]]:
        """Run export_history_csv with save_to_file=True and parse the saved CSV."""
        result = self.svc.handle_export_history_csv({
            "copy_to_clipboard": copy_to_clipboard,
            "include_header": True,
            "save_to_file": True,
        })
        file_path = result.get("file")
        if not file_path:
            return []
        csv_text = Path(file_path).read_text(encoding="utf-8")
        reader = csv.DictReader(io.StringIO(csv_text))
        return list(reader)

    # ------------------------------------------------------------------
    # E2-a: text starting with '=' must be prefixed with single-quote
    # ------------------------------------------------------------------
    def test_text_formula_eq_prefixed(self) -> None:
        self.svc.handle_add_history_item({"text": "=SUM(A1)", "paste_status": "ok"})
        rows = self._export_csv_rows()
        self.assertTrue(len(rows) >= 1)
        text_cell = rows[0]["text"]
        self.assertTrue(text_cell.startswith("'"),
                        f"Expected ' prefix, got: {text_cell!r}")
        self.assertIn("SUM(A1)", text_cell)

    # ------------------------------------------------------------------
    # E2-b: all formula-leading chars covered for text column
    # ------------------------------------------------------------------
    def test_text_all_formula_chars_prefixed(self) -> None:
        for char in self._FORMULA_CHARS:
            val = f"{char}INJECT"
            with self.subTest(char=char):
                with tempfile.TemporaryDirectory() as sub_tmp:
                    svc, _store = _make_service(sub_tmp)
                    svc.handle_add_history_item({"text": val, "paste_status": "ok"})
                    result = svc.handle_export_history_csv({
                        "copy_to_clipboard": False,
                        "save_to_file": True,
                    })
                    file_path = result.get("file")
                    self.assertIsNotNone(file_path, f"No file path for char={char!r}")
                    csv_text = Path(file_path).read_text(encoding="utf-8")
                    reader = csv.DictReader(io.StringIO(csv_text))
                    rows = list(reader)
                    self.assertTrue(len(rows) >= 1, f"No rows for char={char!r}")
                    cell = rows[0]["text"]
                    self.assertTrue(cell.startswith("'"),
                                    f"char={char!r}: expected ' prefix, got: {cell!r}")

    # ------------------------------------------------------------------
    # E2-c: normal text (no formula chars) unchanged
    # ------------------------------------------------------------------
    def test_normal_text_not_modified(self) -> None:
        self.svc.handle_add_history_item({"text": "обычный текст", "paste_status": "ok"})
        rows = self._export_csv_rows()
        self.assertTrue(len(rows) >= 1)
        text_cell = rows[0]["text"]
        self.assertFalse(text_cell.startswith("'"),
                         f"Normal text should not be prefixed: {text_cell!r}")
        self.assertEqual(text_cell, "обычный текст")

    # ------------------------------------------------------------------
    # E2-d: translation column also neutralized
    # ------------------------------------------------------------------
    def test_translation_formula_prefixed(self) -> None:
        self.svc.handle_add_history_item({
            "text": "hello",
            "paste_status": "ok",
            "translated_text": "=DANGEROUS()",
            "translation_status": "ok",
        })
        rows = self._export_csv_rows()
        self.assertTrue(len(rows) >= 1)
        trans_cell = rows[0]["translation"]
        self.assertTrue(trans_cell.startswith("'"),
                        f"Expected ' prefix in translation, got: {trans_cell!r}")

    # ------------------------------------------------------------------
    # _neutralize_csv unit test
    # ------------------------------------------------------------------
    def test_neutralize_csv_static_helper(self) -> None:
        neutralize = HistoryService._neutralize_csv
        # Formula-leading → prefixed
        self.assertEqual(neutralize("=A1+B1"), "'=A1+B1")
        self.assertEqual(neutralize("+CMD"), "'+CMD")
        self.assertEqual(neutralize("-1"), "'-1")
        self.assertEqual(neutralize("@user"), "'@user")
        self.assertEqual(neutralize("|pipe"), "'|pipe")
        self.assertEqual(neutralize("%discount"), "'%discount")
        # Normal values → unchanged
        self.assertEqual(neutralize("hello world"), "hello world")
        self.assertEqual(neutralize(""), "")
        self.assertEqual(neutralize("  spaces"), "  spaces")


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class CSVLangDurationColumnsTestCase(unittest.TestCase):
    """E3: lang and duration_sec columns must be populated in CSV export."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc, self.store = _make_service(self.tmp.name)

    def _export_csv_rows(self) -> list[dict[str, str]]:
        result = self.svc.handle_export_history_csv({
            "copy_to_clipboard": False,
            "save_to_file": True,
        })
        file_path = result.get("file")
        if not file_path:
            return []
        csv_text = Path(file_path).read_text(encoding="utf-8")
        reader = csv.DictReader(io.StringIO(csv_text))
        return list(reader)

    # ------------------------------------------------------------------
    # E3-a: source_lang populates the 'language' CSV column
    # ------------------------------------------------------------------
    def test_lang_column_populated(self) -> None:
        # Use store directly to set source_lang (handle_add_history_item doesn't
        # accept source_lang in the same way — this tests the CSV key fix).
        self.store.add_history_item(
            text="Привет мир",
            paste_status="ok",
            source_lang="ru",
        )
        rows = self._export_csv_rows()
        self.assertTrue(len(rows) >= 1)
        lang_cell = rows[0].get("language", "")
        self.assertEqual(lang_cell, "ru",
                         f"Expected 'ru' in language column, got: {lang_cell!r}")

    # ------------------------------------------------------------------
    # E3-b: audio_duration_sec populates the 'duration_sec' CSV column
    # ------------------------------------------------------------------
    def test_duration_column_populated(self) -> None:
        # Use store directly — handle_add_history_item does not thread audio_duration_sec.
        self.store.add_history_item(
            text="Тест длительности",
            paste_status="ok",
            audio_duration_sec=42.5,
        )
        rows = self._export_csv_rows()
        self.assertTrue(len(rows) >= 1)
        dur_cell = rows[0].get("duration_sec", "")
        self.assertNotEqual(dur_cell, "",
                            "duration_sec column must not be empty when audio_duration_sec is set")
        # Should be parseable as a float
        try:
            dur_val = float(dur_cell)
        except ValueError:
            self.fail(f"duration_sec not parseable as float: {dur_cell!r}")
        self.assertAlmostEqual(dur_val, 42.5, places=1)

    # ------------------------------------------------------------------
    # E3-c: both lang and duration populated together
    # ------------------------------------------------------------------
    def test_lang_and_duration_both_populated(self) -> None:
        self.store.add_history_item(
            text="Hola mundo",
            paste_status="ok",
            source_lang="es",
            audio_duration_sec=10.0,
        )
        rows = self._export_csv_rows()
        self.assertTrue(len(rows) >= 1)
        self.assertEqual(rows[0].get("language", ""), "es")
        self.assertNotEqual(rows[0].get("duration_sec", ""), "")

    # ------------------------------------------------------------------
    # E3-d: missing lang/duration → empty strings, not errors
    # ------------------------------------------------------------------
    def test_missing_lang_and_duration_graceful(self) -> None:
        # Add an item with neither source_lang nor audio_duration_sec
        self.store.add_history_item(text="plain text", paste_status="ok")
        rows = self._export_csv_rows()
        self.assertTrue(len(rows) >= 1)
        # Should be empty strings, not an exception
        self.assertIn("language", rows[0])
        self.assertIn("duration_sec", rows[0])


if __name__ == "__main__":
    unittest.main()
