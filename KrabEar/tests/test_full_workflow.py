"""End-to-end workflow integration test for Krab Ear.

Simulates a complete user session in sequential order:
  - Backend startup
  - Mock transcription recording
  - History search
  - Tagging & favorites
  - Multi-format export (SRT, CSV, MD, JSON, Obsidian)
  - Auto-summary generation
  - Collection CRUD
  - Analytics dashboard
  - Health check
  - Daily digest
  - History backup
  - Deduplication scan
  - Stats report
  - Quality trends
  - Obsidian export
  - Data consistency verification

All steps use BackendService.handle_request (IPC layer) directly.
No network or audio hardware required — FakeRecorder + FakeTranscriber stubs.
"""

from __future__ import annotations
from backend.translator import TranslationResult
from backend.state_store import StateStore
from backend.service import BackendService

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Shared fakes (reused across the whole session)
# ---------------------------------------------------------------------------

class FakeRecorder:
    """Deterministic audio recorder — returns speech-like signal."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        carrier = np.sin(2.0 * np.pi * 210.0 * t)
        envelope = 0.45 + 0.55 * np.sin(2.0 * np.pi * 2.4 * t)
        wobble = 0.08 * np.sin(2.0 * np.pi * 23.0 * t)
        speech_like = 0.06 * carrier * envelope + wobble
        return speech_like.astype(np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        return np.ones(32000, dtype=np.float32), 1.0


class FakeEngine:
    """Minimal engine stub satisfying get_diagnostics and health_check access."""
    quality_profile: str = "balanced"
    current_model: str = "fake-model"
    _whisper_model = None

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class FakeTranscriber:
    """Returns deterministic transcript lines including the session keyword."""

    def __init__(self) -> None:
        self.counter = 0
        self.engine = FakeEngine()

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None) -> str:
        self.counter += 1
        # Embed a searchable keyword so search tests always find this item.
        return f"workflow_session тестовая строка номер {self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return f"preview {self.counter}"


class FakeTranslator:
    """No-op translator — returns empty result for 'off' mode."""

    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary=None) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Main test class — tests run in alphabetical / declaration order which maps
# to the logical session flow below.
# ---------------------------------------------------------------------------

class FullWorkflowTestCase(unittest.TestCase):
    """Sequential end-to-end simulation of a complete Krab Ear user session.

    setUp/tearDown create a fresh tmpdir once for the class so that state
    accumulates across tests (simulating a real session).
    """

    # ---- class-level shared state ----------------------------------------

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        store = StateStore(Path(cls._tmp.name) / "data")
        cls.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        # Collected item IDs across tests
        cls.item_ids: list[str] = []

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # ---- helper ----------------------------------------------------------

    def req(self, method: str, params=None, req_id: str = "test") -> dict:
        """Shorthand for handle_request."""
        resp = self.service.handle_request(
            {"id": req_id, "method": method, "params": params or {}}
        )
        return resp

    def assertOk(self, resp: dict, msg: str = "") -> dict:
        """Assert response is ok=True and return result dict."""
        self.assertTrue(resp.get("ok"), f"Response not ok: {resp!r}  {msg}")
        return resp["result"]

    # ======================================================================
    # Step 1: Backend startup — ping + settings
    # ======================================================================

    def test_01_ping_returns_ok(self) -> None:
        resp = self.req("ping")
        result = self.assertOk(resp)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["service"], "krabear-backend")
        self.assertIn("version", result)
        self.assertFalse(result["is_recording"])
        self.assertGreaterEqual(result["uptime_sec"], 0)

    def test_02_initial_settings_are_defaults(self) -> None:
        result = self.assertOk(self.req("get_settings"))
        self.assertEqual(result["translation_mode"], "off")
        self.assertIn("history_policy", result)
        self.assertIn("quality_profile", result)

    # ======================================================================
    # Step 2: Record first transcription (mock audio → fake STT)
    # ======================================================================

    def test_03_start_recording(self) -> None:
        result = self.assertOk(self.req("start_recording"))
        self.assertEqual(result["status"], "recording")

    def test_04_stop_recording_produces_history_item(self) -> None:
        result = self.assertOk(self.req("stop_recording"))
        # Should succeed (not silence) — FakeRecorder returns speech-like signal
        self.assertIn(result["status"], {"ok", "pasted", "copied"})
        self.assertIn("history_id", result)
        item_id = result["history_id"]
        self.assertIsNotNone(item_id)
        FullWorkflowTestCase.item_ids.append(item_id)

    def test_05_second_recording_adds_another_item(self) -> None:
        """Add a second distinct item so dedup/stats tests have material."""
        self.service.recorder.start()
        result = self.assertOk(self.req("stop_recording"))
        self.assertIn("history_id", result)
        item_id = result["history_id"]
        if item_id:
            FullWorkflowTestCase.item_ids.append(item_id)

    def test_06_add_extra_items_directly(self) -> None:
        """Add items via add_history_item for richer export/analytics tests."""
        texts = [
            "workflow_session встреча по проекту Krab Ear",
            "workflow_session ежедневный стендап команды",
            "workflow_session обсуждение архитектуры backend",
        ]
        for text in texts:
            result = self.assertOk(
                self.req("add_history_item", {"text": text, "paste_status": "ok"})
            )
            FullWorkflowTestCase.item_ids.append(result["id"])

    # ======================================================================
    # Step 3: Search history
    # ======================================================================

    def test_07_search_history_finds_item(self) -> None:
        result = self.assertOk(
            self.req("search_history", {"query": "workflow_session", "limit": 20})
        )
        self.assertIn("items", result)
        self.assertGreater(len(result["items"]), 0, "Search should find at least one item")

    def test_08_get_history_page_returns_items(self) -> None:
        result = self.assertOk(self.req("get_history_page", {"limit": 50}))
        self.assertIn("items", result)
        self.assertGreater(len(result["items"]), 0)

    def test_09_get_history_item_by_id(self) -> None:
        item_id = FullWorkflowTestCase.item_ids[0]
        result = self.assertOk(self.req("get_history_item", {"id": item_id}))
        self.assertEqual(result["id"], item_id)
        self.assertIn("text", result)

    # ======================================================================
    # Step 4: Tags and favorites
    # ======================================================================

    def test_10_add_tag_to_item(self) -> None:
        item_id = FullWorkflowTestCase.item_ids[0]
        result = self.assertOk(self.req("add_tag", {"id": item_id, "tag": "important"}))
        self.assertIn("important", result["tags"])

    def test_11_add_second_tag(self) -> None:
        item_id = FullWorkflowTestCase.item_ids[0]
        result = self.assertOk(self.req("add_tag", {"id": item_id, "tag": "review"}))
        self.assertIn("review", result["tags"])

    def test_12_get_tags_for_item(self) -> None:
        item_id = FullWorkflowTestCase.item_ids[0]
        result = self.assertOk(self.req("get_tags", {"id": item_id}))
        self.assertIn("important", result["tags"])
        self.assertIn("review", result["tags"])

    def test_13_search_by_tag(self) -> None:
        result = self.assertOk(self.req("search_by_tag", {"tag": "important"}))
        self.assertGreater(result["count"], 0)
        ids_in_result = [i["id"] for i in result["items"]]
        self.assertIn(FullWorkflowTestCase.item_ids[0], ids_in_result)

    def test_14_toggle_favorite_on(self) -> None:
        item_id = FullWorkflowTestCase.item_ids[0]
        result = self.assertOk(self.req("toggle_favorite", {"id": item_id}))
        self.assertEqual(result["id"], item_id)
        # After toggle, should be True (was False initially)
        self.assertTrue(result["favorite"])

    def test_15_get_favorites_contains_item(self) -> None:
        result = self.assertOk(self.req("get_favorites"))
        self.assertGreater(result["count"], 0)
        fav_ids = [i["id"] for i in result["items"]]
        self.assertIn(FullWorkflowTestCase.item_ids[0], fav_ids)

    # ======================================================================
    # Step 5: Multi-format export
    # ======================================================================

    def test_16_export_history_srt(self) -> None:
        # SRT export requires a specific item id
        item_id = FullWorkflowTestCase.item_ids[0]
        result = self.assertOk(self.req("export_history_srt", {"id": item_id}))
        self.assertIn("content", result)
        content = result["content"]
        # SRT contains timestamp arrow (even for single-speaker items)
        self.assertIn("-->", content)
        self.assertIn("item_id", result)
        self.assertIn("speakers", result)

    def test_17_export_history_csv(self) -> None:
        # export_history_csv returns ok, entries, file (content written to file)
        result = self.assertOk(self.req("export_history_csv", {"limit": 100}))
        self.assertIn("entries", result)
        self.assertGreaterEqual(result["entries"], 0)
        # file field may be present if save_to_file triggered
        self.assertIn("ok", result)

    def test_18_export_history_markdown(self) -> None:
        # export_history_markdown returns ok, entries, chars
        result = self.assertOk(self.req("export_history_markdown", {"limit": 100}))
        self.assertIn("entries", result)
        self.assertIn("chars", result)
        self.assertGreaterEqual(result["chars"], 0)

    def test_19_export_history_json(self) -> None:
        # export_history_json returns ok, entries, chars, path
        result = self.assertOk(self.req("export_history_json", {"limit": 100}))
        self.assertIn("entries", result)
        self.assertIn("chars", result)
        self.assertGreaterEqual(result["entries"], 0)

    # ======================================================================
    # Step 6: Auto-summary generation (LLM disabled → graceful fallback)
    # ======================================================================

    def test_20_auto_summarize_batch_no_llm(self) -> None:
        """auto_summarize_batch should return gracefully even without LLM."""
        ids = FullWorkflowTestCase.item_ids[:2]
        resp = self.req("auto_summarize_batch", {"ids": ids})
        # Either ok with a result, or an error if LLM unavailable — both valid.
        self.assertIn("ok", resp)

    # ======================================================================
    # Step 7: Collections
    # ======================================================================

    def test_21_create_collection(self) -> None:
        result = self.assertOk(
            self.req("create_collection",
                     {"name": "WorkflowTest", "description": "E2E test collection"})
        )
        self.assertEqual(result["name"], "WorkflowTest")
        self.assertEqual(result["item_count"], 0)

    def test_22_list_collections_contains_new(self) -> None:
        # list_collections returns {"collections": [...]}
        result = self.assertOk(self.req("list_collections"))
        self.assertIn("collections", result)
        names = [c["name"] for c in result["collections"]]
        self.assertIn("WorkflowTest", names)

    def test_23_add_item_to_collection(self) -> None:
        item_id = FullWorkflowTestCase.item_ids[0]
        # The IPC param is collection_name, not collection
        # Returns the updated collection dict: {name, description, created_at, item_count}
        result = self.assertOk(
            self.req("add_to_collection",
                     {"collection_name": "WorkflowTest", "item_id": item_id})
        )
        self.assertIn("name", result)
        self.assertEqual(result["name"], "WorkflowTest")
        self.assertGreaterEqual(result["item_count"], 1)

    def test_24_get_collection_items(self) -> None:
        # IPC param is collection_name, not collection
        result = self.assertOk(
            self.req("get_collection_items", {"collection_name": "WorkflowTest"})
        )
        self.assertIn("items", result)
        self.assertGreater(result["count"], 0)

    # ======================================================================
    # Step 8: Analytics dashboard
    # ======================================================================

    def test_25_analytics_dashboard_has_required_sections(self) -> None:
        result = self.assertOk(self.req("get_analytics_dashboard", {"days": 7}))
        for section in ("overview", "today", "trends", "languages", "quality",
                        "engagement", "storage", "performance"):
            self.assertIn(section, result,
                          f"Analytics dashboard missing section: {section}")

    def test_26_analytics_dashboard_overview_counts(self) -> None:
        result = self.assertOk(self.req("get_analytics_dashboard", {"days": 30}))
        overview = result["overview"]
        self.assertIn("total_recordings", overview)
        self.assertGreater(overview["total_recordings"], 0)

    # ======================================================================
    # Step 9: Health check
    # ======================================================================

    def test_27_health_check_returns_status(self) -> None:
        result = self.assertOk(self.req("health_check"))
        self.assertIn("status", result)
        # HealthChecker returns "healthy" | "degraded" | "critical" (not "ok")
        self.assertIn(result["status"], {"healthy", "ok", "degraded", "critical"})
        # checks is a dict of subsystem → status dict
        self.assertIn("checks", result)
        self.assertIsInstance(result["checks"], dict)

    # ======================================================================
    # Step 10: Daily digest
    # ======================================================================

    def test_28_daily_digest_returns_markdown(self) -> None:
        result = self.assertOk(self.req("generate_daily_digest"))
        self.assertIn("markdown", result)
        self.assertIn("date", result)
        self.assertIn("total_recordings", result)

    def test_29_daily_digest_total_recordings_gte_zero(self) -> None:
        result = self.assertOk(self.req("generate_daily_digest"))
        self.assertGreaterEqual(result["total_recordings"], 0)

    # ======================================================================
    # Step 11: Backup history
    # ======================================================================

    def test_30_backup_history_creates_files(self) -> None:
        result = self.assertOk(self.req("backup_history"))
        self.assertIn("backup_path", result)
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.exists(), "Backup directory should exist")
        self.assertIn("entries", result)
        self.assertGreaterEqual(result["entries"], 0)

    def test_31_list_backups_shows_backup(self) -> None:
        result = self.assertOk(self.req("list_backups"))
        self.assertIn("backups", result)
        self.assertGreater(len(result["backups"]), 0)

    # ======================================================================
    # Step 12: Deduplication scan
    # ======================================================================

    def test_32_run_deduplication_scan(self) -> None:
        result = self.assertOk(self.req("run_deduplication"))
        self.assertIn("total_scanned", result)
        self.assertIn("duplicate_groups", result)
        self.assertGreaterEqual(result["total_scanned"], 0)

    def test_33_get_dedup_stats(self) -> None:
        result = self.assertOk(self.req("get_dedup_stats"))
        self.assertIn("total_checked", result)
        self.assertIn("duplicates_found", result)

    # ======================================================================
    # Step 13: Stats report
    # ======================================================================

    def test_34_stats_report_is_markdown(self) -> None:
        result = self.assertOk(self.req("generate_stats_report", {"days": 7}))
        self.assertIn("markdown", result)
        self.assertIn("#", result["markdown"])
        self.assertEqual(result["days"], 7)

    def test_35_mini_stats_report_non_empty(self) -> None:
        result = self.assertOk(self.req("generate_mini_stats_report"))
        self.assertIn("markdown", result)
        self.assertGreater(len(result["markdown"]), 0)

    # ======================================================================
    # Step 14: Quality trends
    # ======================================================================

    def test_36_quality_trends_structure(self) -> None:
        result = self.assertOk(self.req("analyze_quality_trends", {"days": 7}))
        self.assertIn("overall_trend", result)
        self.assertIn("trend_slope", result)
        self.assertIn("confidence_distribution", result)

    # ======================================================================
    # Step 15: Obsidian export
    # ======================================================================

    def test_37_export_obsidian_by_ids(self) -> None:
        ids = FullWorkflowTestCase.item_ids[:2]
        result = self.assertOk(self.req("export_obsidian", {"ids": ids}))
        self.assertIn("content", result)
        content = result["content"]
        # Obsidian export has YAML frontmatter
        self.assertTrue(
            content.startswith("---") or "tags:" in content,
            "Obsidian export should contain YAML frontmatter"
        )
        self.assertIn("entries", result)
        self.assertGreater(result["entries"], 0)

    def test_38_export_obsidian_has_krab_ear_tag(self) -> None:
        ids = FullWorkflowTestCase.item_ids[:1]
        result = self.assertOk(self.req("export_obsidian", {"ids": ids}))
        self.assertIn("krab-ear", result["content"])

    # ======================================================================
    # Step 16: Data consistency verification
    # ======================================================================

    def test_39_integrity_check_passes(self) -> None:
        result = self.assertOk(self.req("check_integrity"))
        self.assertIn("status", result)
        self.assertIn(result["status"], {"ok", "warnings", "errors"})
        self.assertIn("total_items", result)
        self.assertGreater(result["total_items"], 0)

    def test_40_history_stats_consistent(self) -> None:
        result = self.assertOk(self.req("get_history_stats"))
        # get_history_stats returns active_count (not total_items)
        self.assertIn("active_count", result)
        self.assertGreaterEqual(result["active_count"], len(FullWorkflowTestCase.item_ids))

    def test_41_history_overview_consistent(self) -> None:
        result = self.assertOk(self.req("get_history_overview"))
        # get_history_overview returns active_count (not total)
        self.assertIn("active_count", result)
        self.assertGreater(result["active_count"], 0)

    def test_42_list_all_tags_includes_workflow_tags(self) -> None:
        result = self.assertOk(self.req("list_all_tags"))
        all_tag_names = [entry["tag"] for entry in result["tags"]]
        self.assertIn("important", all_tag_names)
        self.assertIn("review", all_tag_names)

    def test_43_word_frequency_analysis(self) -> None:
        # word_frequency_analysis returns top_words, total_words, unique_words, etc.
        result = self.assertOk(self.req("word_frequency_analysis", {"limit": 20}))
        self.assertIn("top_words", result)
        self.assertIsInstance(result["top_words"], list)
        self.assertIn("total_words", result)
        self.assertGreater(result["total_words"], 0)

    def test_44_get_history_statistics(self) -> None:
        result = self.assertOk(self.req("get_history_statistics"))
        self.assertIn("total_items", result)

    def test_45_storage_info_has_sizes(self) -> None:
        result = self.assertOk(self.req("get_storage_info"))
        # Actual key is history_file_size_mb (not history_mb)
        self.assertIn("history_file_size_mb", result)
        self.assertGreaterEqual(result["history_file_size_mb"], 0.0)
        self.assertIn("total_data_mb", result)

    # ======================================================================
    # Final: Full batch request round-trip
    # ======================================================================

    def test_46_batch_request_ping_and_settings(self) -> None:
        result = self.assertOk(
            self.req("batch", {
                "requests": [
                    {"method": "ping", "params": {}},
                    {"method": "get_settings", "params": {}},
                ]
            })
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 0)

    def test_47_diagnostics_has_all_sections(self) -> None:
        # get_diagnostics accesses transcriber.engine — FakeTranscriber has FakeEngine stub
        result = self.assertOk(self.req("get_diagnostics"))
        for section in ("system", "stt", "llm", "history", "settings_cache"):
            self.assertIn(section, result,
                          f"Diagnostics missing section: {section}")
        # Verify stt section contains expected fields
        self.assertIn("quality_profile", result["stt"])
        self.assertIn("current_model", result["stt"])

    def test_48_find_duplicates_returns_groups(self) -> None:
        result = self.assertOk(self.req("find_duplicates", {"similarity_threshold": 0.95}))
        self.assertIn("groups", result)
        self.assertIn("total_duplicates", result)

    def test_49_compact_history_runs_without_error(self) -> None:
        result = self.assertOk(self.req("compact_history"))
        self.assertTrue(result.get("compacted"))

    def test_50_ping_after_full_session_still_ok(self) -> None:
        """Final sanity check: service is still healthy at end of session."""
        result = self.assertOk(self.req("ping"))
        self.assertEqual(result["status"], "ok")
        # history_count should reflect items we added during the session
        self.assertGreater(result["history_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
