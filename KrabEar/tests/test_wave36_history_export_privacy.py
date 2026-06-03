"""Tests for wave-36: history export privacy gates + transcript_versions gate +
calendar_links purge.

Covers three fixes:

B1 (HIGH) — six history-export handlers must withhold the transcript corpus while
            privacy mode is active (they write the full corpus to the IPC response,
            clipboard, or local files):
              handle_export_history, handle_export_history_markdown,
              handle_export_history_json, handle_export_history_csv,
              handle_batch_export, handle_export_html_report.

B2 (HIGH) — TranscriptVersionManager.handle_get_transcript_versions must withhold a
            transcript's full edit history (every version is cleartext PII) while
            privacy mode is active.  Privacy is read via an injected settings_fn.

B3 (MED)  — handle_purge_all_data must physically delete
            history_calendar_links.ndjson (CalendarLinker sidecar journal — event
            titles are user PII).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402
from backend.transcript_versioning import TranscriptVersionManager  # noqa: E402


# ---------------------------------------------------------------------------
# B1 — export handlers honour privacy mode
# ---------------------------------------------------------------------------
class ExportPrivacyGateTestCase(unittest.TestCase):
    """Каждый экспорт-хендлер не отдаёт корпус транскрипций при privacy_mode."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        # privacy flag is read via the injected cached_settings provider.
        self._privacy = {"privacy_mode_enabled": False}
        self.svc = HistoryService(
            store=self.store,
            cached_settings=lambda: dict(self._privacy),
        )
        # Seed some real transcript content so a leak would be observable.
        for i in range(3):
            self.store.add_history_item(
                text=f"секретная запись {i}", paste_status="ok", source_lang="ru"
            )

    def _set_privacy(self, on: bool) -> None:
        self._privacy["privacy_mode_enabled"] = on

    # --- privacy ON: every export refuses ---------------------------------
    def test_export_history_blocked_in_privacy(self) -> None:
        self._set_privacy(True)
        res = self.svc.handle_export_history({})
        self.assertEqual(res.get("content"), "")
        self.assertEqual(res.get("total_items"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_export_markdown_blocked_in_privacy(self) -> None:
        self._set_privacy(True)
        res = self.svc.handle_export_history_markdown({})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("entries"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_export_json_blocked_in_privacy(self) -> None:
        self._set_privacy(True)
        res = self.svc.handle_export_history_json({})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("entries"), 0)
        self.assertIsNone(res.get("path"))
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_export_csv_blocked_in_privacy(self) -> None:
        self._set_privacy(True)
        # copy_to_clipboard defaults True for CSV — the gate must short-circuit
        # before any pbcopy/file write happens.
        res = self.svc.handle_export_history_csv({"copy_to_clipboard": False})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("entries"), 0)
        self.assertIsNone(res.get("file"))
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_batch_export_blocked_in_privacy(self) -> None:
        self._set_privacy(True)
        res = self.svc.handle_batch_export({"formats": ["markdown"]})
        self.assertEqual(res.get("files"), {})
        self.assertEqual(res.get("total_entries"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        # No export bundle directory may be created while privacy is on.
        exports_dir = Path(self.store.data_dir) / "exports"
        if exports_dir.exists():
            self.assertEqual(list(exports_dir.iterdir()), [])

    def test_html_report_blocked_in_privacy(self) -> None:
        self._set_privacy(True)
        res = self.svc.handle_export_html_report({})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("html"), "")
        self.assertEqual(res.get("entries"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_no_transcript_file_written_when_blocked(self) -> None:
        """save_to_file=True must NOT write any artefact while privacy is on."""
        self._set_privacy(True)
        self.svc.handle_export_history({"save_to_file": True})
        self.svc.handle_export_history_json({"save_to_file": True})
        self.svc.handle_export_html_report({"save_to_file": True})
        transcripts_dir = Path(self.store.data_dir) / "transcripts"
        files = list(transcripts_dir.glob("*")) if transcripts_dir.exists() else []
        self.assertEqual(files, [], f"export artefacts leaked under privacy: {files}")

    # --- privacy OFF: exports still work (no regression) ------------------
    def test_exports_work_when_privacy_off(self) -> None:
        self._set_privacy(False)
        self.assertEqual(self.svc.handle_export_history({}).get("total_items"), 3)

        md = self.svc.handle_export_history_markdown({})
        self.assertTrue(md.get("ok"))
        self.assertEqual(md.get("entries"), 3)

        js = self.svc.handle_export_history_json({})
        self.assertTrue(js.get("ok"))
        self.assertEqual(js.get("entries"), 3)

        csv_res = self.svc.handle_export_history_csv({"copy_to_clipboard": False})
        self.assertTrue(csv_res.get("ok"))
        self.assertEqual(csv_res.get("entries"), 3)

        html = self.svc.handle_export_html_report({})
        self.assertTrue(html.get("ok"))
        self.assertEqual(html.get("entries"), 3)


# ---------------------------------------------------------------------------
# B2 — transcript versions honour privacy mode
# ---------------------------------------------------------------------------
class TranscriptVersionsPrivacyGateTestCase(unittest.TestCase):
    """handle_get_transcript_versions не отдаёт историю версий при privacy_mode."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._privacy = {"privacy_mode_enabled": False}
        self.mgr = TranscriptVersionManager(
            data_dir=Path(self.tmp.name),
            settings_fn=lambda: dict(self._privacy),
        )
        self.mgr.save_version(item_id="item-1", text="первая версия", source="stt_raw")
        self.mgr.save_version(item_id="item-1", text="вторая версия", source="manual")

    def test_get_versions_blocked_in_privacy(self) -> None:
        self._privacy["privacy_mode_enabled"] = True
        res = self.mgr.handle_get_transcript_versions({"item_id": "item-1"})
        self.assertEqual(res.get("versions"), [])
        self.assertEqual(res.get("total"), 0)
        self.assertEqual(res.get("reason"), "privacy_mode_active")

    def test_get_versions_work_when_privacy_off(self) -> None:
        self._privacy["privacy_mode_enabled"] = False
        res = self.mgr.handle_get_transcript_versions({"item_id": "item-1"})
        self.assertEqual(res.get("total"), 2)
        self.assertNotIn("reason", res)

    def test_no_settings_fn_means_no_gate(self) -> None:
        """Backward-compat: data-dir-only constructor never gates (settings_fn=None)."""
        mgr = TranscriptVersionManager(data_dir=Path(self.tmp.name))
        res = mgr.handle_get_transcript_versions({"item_id": "item-1"})
        self.assertEqual(res.get("total"), 2)


# ---------------------------------------------------------------------------
# B3 — calendar_links journal is deleted by purge
# ---------------------------------------------------------------------------
class CalendarLinksPurgeTestCase(unittest.TestCase):
    """handle_purge_all_data физически удаляет history_calendar_links.ndjson."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_calendar_links_deleted_after_purge(self) -> None:
        # Seed a calendar link with a PII event title directly into the sidecar
        # journal (production writes it via service.py link_to_calendar_event).
        item = self.store.add_history_item(text="звонок", paste_status="ok")
        item_id = item.id
        link_path = self.store.calendar_links_path
        link_path.write_text(
            '{"id": "%s", "event_title": "Standup with Alice", "event_id": "evt-1"}\n'
            % item_id,
            encoding="utf-8",
        )
        self.assertTrue(link_path.exists())
        self.assertIn("Alice", link_path.read_text(encoding="utf-8"))

        res = self.svc.handle_purge_all_data({"confirm": True})
        self.assertTrue(res.get("ok"))

        # File must be gone (physically unlinked), not merely truncated.
        self.assertFalse(
            link_path.exists(),
            "history_calendar_links.ndjson survived purge",
        )
        # calendar_links must not show up as a failed purge step.
        self.assertNotIn("calendar_links", res.get("errors", []))


if __name__ == "__main__":
    unittest.main()
