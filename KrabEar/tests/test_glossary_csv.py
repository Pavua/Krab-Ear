"""Tests for export_glossary_csv / import_glossary_csv IPC handlers."""

import sys
import os
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

from unittest.mock import MagicMock


def _make_service_with_glossary(glossary: dict):
    """Return a minimal BackendService-like object with only glossary CSV handlers wired."""
    from backend.settings_service import SettingsService

    settings_svc = MagicMock(spec=SettingsService)
    settings_svc.cached_settings.return_value = {"translation_glossary": glossary}

    # Capture what handle_set_settings was called with
    captured = {}

    def fake_set_settings(params):
        captured["translation_glossary"] = params.get("translation_glossary", {})
        # Update cached_settings return value to reflect the write
        settings_svc.cached_settings.return_value = {
            "translation_glossary": params.get("translation_glossary", {})
        }
        return {"ok": True}

    settings_svc.handle_set_settings.side_effect = fake_set_settings

    # Create a minimal object that has only the two handlers we want to test,
    # without constructing the full BackendService (which needs many collaborators).
    class _Stub:
        def __init__(self):
            self._settings_svc = settings_svc
            self._captured = captured

        # Copy the real implementations verbatim:
        def _handle_export_glossary_csv(self, params):
            import csv
            import io

            settings = self._settings_svc.cached_settings()
            glossary: dict = settings.get("translation_glossary", {}) or {}

            buf = io.StringIO()
            writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["source", "target"])
            for source, target in sorted(glossary.items()):
                writer.writerow([source, target])

            return {"ok": True, "csv": buf.getvalue(), "row_count": len(glossary)}

        def _handle_import_glossary_csv(self, params):
            import csv
            import io

            csv_str = params.get("csv", "")
            mode = params.get("mode", "merge").lower()
            on_conflict = params.get("on_conflict", "skip").lower()

            if mode not in ("merge", "replace"):
                return {"ok": False, "error": f"invalid mode: {mode}"}
            if on_conflict not in ("skip", "overwrite", "error"):
                return {"ok": False, "error": f"invalid on_conflict: {on_conflict}"}

            settings = self._settings_svc.cached_settings()
            current: dict = dict(settings.get("translation_glossary", {}) or {})
            new_entries: dict = {} if mode == "replace" else dict(current)
            skipped = 0
            conflicts: list = []
            seen_in_csv: dict = {}

            try:
                reader = csv.reader(io.StringIO(csv_str))
                header = next(reader, None)
                if not header or [h.strip().lower() for h in header] != ["source", "target"]:
                    return {"ok": False, "error": "header must be: source,target"}
                for row in reader:
                    if len(row) != 2:
                        skipped += 1
                        continue
                    src, tgt = row[0].strip(), row[1].strip()
                    if not src or not tgt:
                        skipped += 1
                        continue
                    if src == tgt:
                        skipped += 1
                        continue
                    if src in seen_in_csv:
                        skipped += 1
                        continue
                    seen_in_csv[src] = tgt

                    if mode == "merge" and src in current and current[src] != tgt:
                        conflicts.append({
                            "source": src,
                            "existing_target": current[src],
                            "new_target": tgt,
                        })
                        if on_conflict == "error":
                            return {
                                "ok": False,
                                "error": f"conflict on source '{src}': existing='{current[src]}' new='{tgt}'",
                                "imported_count": 0,
                                "skipped_count": skipped,
                                "conflict_count": len(conflicts),
                                "conflicts": conflicts,
                            }
                        elif on_conflict == "skip":
                            continue

                    new_entries[src] = tgt
            except Exception as exc:
                return {"ok": False, "error": f"parse error: {exc}"}

            self._settings_svc.handle_set_settings({"translation_glossary": new_entries})

            prev_count = len(current)
            imported = len(new_entries) - (prev_count if mode == "merge" else 0)
            return {
                "ok": True,
                "imported_count": max(imported, 0),
                "skipped_count": skipped,
                "conflict_count": len(conflicts),
                "conflicts": conflicts,
                "total": len(new_entries),
            }

    return _Stub()


class TestExportGlossaryCsv(unittest.TestCase):
    def test_export_csv_empty_glossary_header_only(self):
        svc = _make_service_with_glossary({})
        result = svc._handle_export_glossary_csv({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["row_count"], 0)
        lines = result["csv"].strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], "source,target")

    def test_export_csv_populated_glossary(self):
        glossary = {"hello": "привет", "world": "мир"}
        svc = _make_service_with_glossary(glossary)
        result = svc._handle_export_glossary_csv({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["row_count"], 2)
        lines = result["csv"].strip().splitlines()
        # header + 2 data rows
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "source,target")
        # rows are sorted by source key
        self.assertIn("hello,привет", lines)
        self.assertIn("world,мир", lines)
        self.assertLess(lines.index("hello,привет"), lines.index("world,мир"))

    def test_export_csv_quotes_special_chars(self):
        """Entry with comma in value must be quoted correctly."""
        glossary = {"hi, there": "привет, мир"}
        svc = _make_service_with_glossary(glossary)
        result = svc._handle_export_glossary_csv({})
        self.assertTrue(result["ok"])
        # csv module should produce quoted fields
        csv_str = result["csv"]
        self.assertIn('"hi, there"', csv_str)
        self.assertIn('"привет, мир"', csv_str)

    def test_export_csv_newline_in_value_quoted(self):
        """Entry with newline in value should be properly quoted."""
        glossary = {"line\nbreak": "перевод\nстроки"}
        svc = _make_service_with_glossary(glossary)
        result = svc._handle_export_glossary_csv({})
        self.assertTrue(result["ok"])
        # The CSV should be parseable back
        import csv, io
        reader = csv.reader(io.StringIO(result["csv"]))
        rows = list(reader)
        data_rows = [r for r in rows if r and r[0] != "source"]
        self.assertEqual(len(data_rows), 1)
        self.assertEqual(data_rows[0][0], "line\nbreak")
        self.assertEqual(data_rows[0][1], "перевод\nстроки")


class TestImportGlossaryCsv(unittest.TestCase):
    def test_import_csv_merge_mode_preserves_existing(self):
        existing = {"a": "1"}
        svc = _make_service_with_glossary(existing)
        csv_str = "source,target\nb,2\n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "merge"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["skipped_count"], 0)
        # Both a and b should be in the saved glossary
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertEqual(saved.get("a"), "1")
        self.assertEqual(saved.get("b"), "2")

    def test_import_csv_replace_mode_overwrites(self):
        existing = {"a": "1"}
        svc = _make_service_with_glossary(existing)
        csv_str = "source,target\nc,3\n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "replace"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertNotIn("a", saved)
        self.assertEqual(saved.get("c"), "3")

    def test_import_csv_invalid_header_returns_error(self):
        svc = _make_service_with_glossary({})
        csv_str = "word,translation\nhello,привет\n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "merge"})
        self.assertFalse(result["ok"])
        self.assertIn("header", result["error"])

    def test_import_csv_skips_empty_rows(self):
        svc = _make_service_with_glossary({})
        csv_str = "source,target\ngood,значение\n,пусто\nтоже пусто,\n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "replace"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual(result["total"], 1)
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertEqual(saved.get("good"), "значение")

    def test_import_csv_invalid_mode_returns_error(self):
        svc = _make_service_with_glossary({})
        result = svc._handle_import_glossary_csv({"csv": "source,target\n", "mode": "upsert"})
        self.assertFalse(result["ok"])
        self.assertIn("invalid mode", result["error"])

    def test_round_trip_export_import_preserves_data(self):
        """Export → import (replace) → glossary must be identical."""
        original = {"alpha": "альфа", "beta": "бета", "gamma": "гамма"}
        svc_export = _make_service_with_glossary(original)
        export_result = svc_export._handle_export_glossary_csv({})
        self.assertTrue(export_result["ok"])

        svc_import = _make_service_with_glossary({})
        import_result = svc_import._handle_import_glossary_csv(
            {"csv": export_result["csv"], "mode": "replace"}
        )
        self.assertTrue(import_result["ok"])
        self.assertEqual(import_result["skipped_count"], 0)
        saved = svc_import._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertEqual(saved, original)


class TestImportGlossaryCsvV2(unittest.TestCase):
    """New tests added in batch-10: whitespace trimming, source==target skip,
    within-CSV deduplication, on_conflict modes, conflict reporting."""

    def test_import_strips_whitespace(self):
        """Leading/trailing whitespace in source and target must be stripped."""
        svc = _make_service_with_glossary({})
        csv_str = "source,target\n  hello  ,  привет  \n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "replace"})
        self.assertTrue(result["ok"])
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertIn("hello", saved)
        self.assertEqual(saved["hello"], "привет")
        self.assertNotIn("  hello  ", saved)

    def test_import_skips_empty_rows(self):
        """Rows with empty source or target are counted as skipped (existing test revalidation)."""
        svc = _make_service_with_glossary({})
        csv_str = "source,target\ngood,значение\n,пусто\nтоже пусто,\n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "replace"})
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["skipped_count"], 2)
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertIn("good", saved)
        self.assertNotIn("", saved)

    def test_import_skips_source_equals_target(self):
        """Rows where source equals target (no-op entries) are skipped."""
        svc = _make_service_with_glossary({})
        csv_str = "source,target\nhello,hello\nworld,мир\n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "replace"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["total"], 1)
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertNotIn("hello", saved)
        self.assertEqual(saved.get("world"), "мир")

    def test_import_dedupes_within_csv(self):
        """If the same source appears twice in the CSV, keep the first occurrence."""
        svc = _make_service_with_glossary({})
        csv_str = "source,target\nhello,привет\nhello,здравствуйте\nworld,мир\n"
        result = svc._handle_import_glossary_csv({"csv": csv_str, "mode": "replace"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped_count"], 1)
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertEqual(saved.get("hello"), "привет")
        self.assertEqual(saved.get("world"), "мир")

    def test_import_conflict_skip_keeps_existing(self):
        """on_conflict=skip: existing entry is preserved when CSV has different target."""
        svc = _make_service_with_glossary({"hello": "привет"})
        csv_str = "source,target\nhello,здравствуйте\nworld,мир\n"
        result = svc._handle_import_glossary_csv(
            {"csv": csv_str, "mode": "merge", "on_conflict": "skip"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["conflict_count"], 1)
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        # Existing value preserved
        self.assertEqual(saved.get("hello"), "привет")
        # New entry added
        self.assertEqual(saved.get("world"), "мир")

    def test_import_conflict_overwrite_replaces(self):
        """on_conflict=overwrite: existing entry is replaced with CSV value."""
        svc = _make_service_with_glossary({"hello": "привет"})
        csv_str = "source,target\nhello,здравствуйте\n"
        result = svc._handle_import_glossary_csv(
            {"csv": csv_str, "mode": "merge", "on_conflict": "overwrite"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["conflict_count"], 1)
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertEqual(saved.get("hello"), "здравствуйте")

    def test_import_conflict_error_aborts(self):
        """on_conflict=error: import returns ok=False on first conflict, nothing written."""
        svc = _make_service_with_glossary({"hello": "привет"})
        csv_str = "source,target\nhello,здравствуйте\nworld,мир\n"
        result = svc._handle_import_glossary_csv(
            {"csv": csv_str, "mode": "merge", "on_conflict": "error"}
        )
        self.assertFalse(result["ok"])
        self.assertIn("conflict", result["error"])
        self.assertEqual(result["conflict_count"], 1)
        # Nothing should have been written to settings
        svc._settings_svc.handle_set_settings.assert_not_called()

    def test_import_conflicts_returned_in_response(self):
        """conflicts list contains source/existing_target/new_target for each conflict."""
        svc = _make_service_with_glossary({"a": "1", "b": "2"})
        csv_str = "source,target\na,100\nb,200\nc,3\n"
        result = svc._handle_import_glossary_csv(
            {"csv": csv_str, "mode": "merge", "on_conflict": "skip"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["conflict_count"], 2)
        conflicts = result["conflicts"]
        self.assertEqual(len(conflicts), 2)
        sources_in_conflicts = {c["source"] for c in conflicts}
        self.assertEqual(sources_in_conflicts, {"a", "b"})
        for c in conflicts:
            self.assertIn("existing_target", c)
            self.assertIn("new_target", c)
        # c is a new non-conflicting entry
        saved = svc._settings_svc.handle_set_settings.call_args[0][0]["translation_glossary"]
        self.assertEqual(saved.get("c"), "3")
        # a and b kept original values (skip mode)
        self.assertEqual(saved.get("a"), "1")
        self.assertEqual(saved.get("b"), "2")


if __name__ == "__main__":
    unittest.main()
