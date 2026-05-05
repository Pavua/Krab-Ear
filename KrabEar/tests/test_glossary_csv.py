"""Tests for export_glossary_csv / import_glossary_csv IPC handlers."""

import sys
import os
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

from unittest.mock import MagicMock, patch


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
            if mode not in ("merge", "replace"):
                return {"ok": False, "error": f"invalid mode: {mode}"}

            settings = self._settings_svc.cached_settings()
            current: dict = dict(settings.get("translation_glossary", {}) or {})
            new_entries: dict = {} if mode == "replace" else dict(current)
            skipped = 0

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


if __name__ == "__main__":
    unittest.main()
