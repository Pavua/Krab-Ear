"""W1353: Tests for extended _SENSITIVE_METHODS coverage in audit_logger.py.

Verifies:
- sensitive methods produce {redacted: True, param_count: N} — no param keys leak
- non-sensitive methods log params_keys normally
- _SENSITIVE_METHODS has ≥ 30 entries (W1351 F2 MED threshold)
- representative methods from each category (credentials, text, paths, PII) are covered
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.audit_logger import AuditLogger, _SENSITIVE_METHODS  # noqa: E402


class TestSensitiveMethodRedactsParams(unittest.TestCase):
    """Sensitive methods must not leak param names into audit log."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.al = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.al.close()

    def _read_entries(self) -> list[dict]:
        files = sorted(Path(self.tmpdir).glob("audit_*.ndjson"))
        entries = []
        for f in files:
            for line in f.read_text().splitlines():
                if line.strip():
                    entries.append(json.loads(line))
        return entries

    def test_sensitive_method_redacts_params(self):
        """W1696: sensitive method → params_keys=[] (not redacted=True/param_count).
        Values must not appear in the audit log."""
        self.al.log_request(
            "set_settings",
            {"voice_gateway_api_key": "sk-secret", "hf_token": "hf-abc123"},
            {"ok": True, "result": {}},
            1.0,
        )
        entries = self._read_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [], "set_settings должен иметь params_keys=[]")
        # No secret values in any value
        entry_str = json.dumps(entry)
        self.assertNotIn("sk-secret", entry_str)
        self.assertNotIn("hf-abc123", entry_str)

    def test_sensitive_method_translate_text_redacts_text(self):
        """W1696: translate_text → params_keys=[] (text not logged)."""
        self.al.log_request(
            "translate_text",
            {"text": "personal medical info", "source_lang": "ru", "target_lang": "es"},
            {"ok": True, "result": {"translated": "..."}},
            5.0,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])
        self.assertNotIn("personal medical info", json.dumps(entry))

    def test_sensitive_method_send_imessage_redacts_recipient(self):
        """W1696: send_imessage → params_keys=[] (PII not logged)."""
        self.al.log_request(
            "send_imessage",
            {"recipient": "+1234567890", "text": "hello friend"},
            {"ok": True, "result": {}},
            2.0,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])
        self.assertNotIn("+1234567890", json.dumps(entry))

    def test_sensitive_method_transcribe_paths_redacts_paths(self):
        """W1696: transcribe_paths → params_keys=[] (file paths not logged)."""
        self.al.log_request(
            "transcribe_paths",
            {"paths": ["/Users/bob/private/audio.m4a"], "language": "ru"},
            {"ok": True, "result": {}},
            10.0,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])
        self.assertNotIn("/Users/bob", json.dumps(entry))

    def test_sensitive_method_create_calendar_event_redacts_notes(self):
        """W1696: create_calendar_event → params_keys=[] (personal data not logged)."""
        self.al.log_request(
            "create_calendar_event",
            {"title": "Doctor appointment", "notes": "confidential", "start_time": "2026-06-01T10:00:00"},
            {"ok": True, "result": {}},
            3.0,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])
        self.assertNotIn("Doctor appointment", json.dumps(entry))

    def test_sensitive_method_live_subs_ingest_redacts_audio(self):
        """W1696: live_subs_ingest → params_keys=[] (audio data not logged)."""
        self.al.log_request(
            "live_subs_ingest",
            {"audio_b64": "AAAA" * 1000, "is_final": False},
            {"ok": True, "result": {}},
            0.1,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])

    def test_sensitive_method_import_settings_redacts_file_path(self):
        """W1696: import_settings → params_keys=[] (file path not logged)."""
        self.al.log_request(
            "import_settings",
            {"file": "/home/user/settings_with_api_keys.json"},
            {"ok": True, "result": {}},
            1.5,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])

    def test_sensitive_method_register_webhook_redacts_url(self):
        """W1696: register_webhook → params_keys=[] (URL token not logged)."""
        self.al.log_request(
            "register_webhook",
            {"url": "https://hooks.example.com/api?token=mysecret", "event": "transcription.done"},
            {"ok": True, "result": {}},
            0.5,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])
        self.assertNotIn("mysecret", json.dumps(entry))

    def test_sensitive_method_call_session_create_redacts_phone(self):
        """W1696: call_session_create → params_keys=[] (phone PII not logged)."""
        self.al.log_request(
            "call_session_create",
            {"phone_number": "+79999999999", "provider": "telnyx"},
            {"ok": True, "result": {}},
            1.0,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])
        self.assertNotIn("+79999999999", json.dumps(entry))

    def test_param_count_zero_when_empty_params(self):
        """W1696: sensitive method with empty params → params_keys=[]."""
        self.al.log_request(
            "semantic_search",
            {},
            {"ok": True, "result": {}},
            0.5,
        )
        entries = self._read_entries()
        entry = entries[0]
        self.assertEqual(entry.get("params_keys"), [])

    def test_all_sensitive_methods_produce_redacted_true(self):
        """W1696: every method in _SENSITIVE_METHODS produces params_keys=[] in the log."""
        for method in _SENSITIVE_METHODS:
            with self.subTest(method=method):
                tmpdir2 = tempfile.mkdtemp()
                al2 = AuditLogger(data_dir=tmpdir2)
                al2.log_request(
                    method,
                    {"secret": "value", "key": "data"},
                    {"ok": True, "result": {}},
                    1.0,
                )
                al2.close()
                files = sorted(Path(tmpdir2).glob("audit_*.ndjson"))
                self.assertTrue(files, f"No audit file created for method {method}")
                entry = json.loads(files[0].read_text().strip().splitlines()[0])
                self.assertEqual(
                    entry.get("params_keys"),
                    [],
                    f"method '{method}' должен иметь params_keys=[] но: {entry}",
                )


class TestNonSensitiveMethodLogsParamsNormally(unittest.TestCase):
    """Non-sensitive methods must log params_keys (sorted)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.al = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.al.close()

    def _first_entry(self) -> dict:
        files = sorted(Path(self.tmpdir).glob("audit_*.ndjson"))
        return json.loads(files[0].read_text().strip())

    def test_ping_logs_params_keys(self):
        """ping (non-sensitive) should log params_keys normally."""
        self.al.log_request("ping", {"ts": 123}, {"ok": True, "result": {}}, 0.5)
        entry = self._first_entry()
        self.assertNotIn("redacted", entry)
        self.assertIn("params_keys", entry)
        self.assertEqual(entry["params_keys"], ["ts"])

    def test_get_history_page_logs_params_keys(self):
        """get_history_page should log its params (page, limit)."""
        self.al.log_request(
            "get_history_page",
            {"page": 1, "limit": 20},
            {"ok": True, "result": {}},
            2.0,
        )
        entry = self._first_entry()
        self.assertNotIn("redacted", entry)
        self.assertEqual(sorted(entry["params_keys"]), ["limit", "page"])

    def test_health_check_logs_params_keys(self):
        """health_check (non-sensitive) logs normally."""
        self.al.log_request("health_check", {}, {"ok": True, "result": {}}, 1.0)
        entry = self._first_entry()
        self.assertNotIn("redacted", entry)
        self.assertIn("params_keys", entry)
        self.assertEqual(entry["params_keys"], [])

    def test_list_stt_hotwords_logs_params_keys(self):
        """list_stt_hotwords is not sensitive — params logged."""
        self.al.log_request(
            "list_stt_hotwords", {}, {"ok": True, "result": {}}, 0.3
        )
        entry = self._first_entry()
        self.assertNotIn("redacted", entry)

    def test_get_settings_logs_params_keys(self):
        """get_settings (read-only, no secrets in params) logs normally."""
        self.al.log_request(
            "get_settings", {"keys": ["theme"]}, {"ok": True, "result": {}}, 1.0
        )
        entry = self._first_entry()
        self.assertNotIn("redacted", entry)
        self.assertEqual(entry["params_keys"], ["keys"])


class TestSensitiveMethodsCoverageAboveThreshold(unittest.TestCase):
    """_SENSITIVE_METHODS must have ≥ 30 entries (W1351 F2 MED requirement)."""

    def test_sensitive_methods_coverage_above_threshold(self):
        """Assert _SENSITIVE_METHODS has at least 30 entries."""
        count = len(_SENSITIVE_METHODS)
        self.assertGreaterEqual(
            count,
            30,
            f"_SENSITIVE_METHODS only has {count} entries — threshold is 30",
        )

    def test_credential_methods_covered(self):
        """Credential-bearing methods are in _SENSITIVE_METHODS."""
        required = {
            "set_settings",
            "import_settings",
            "restore_settings_backup",
        }
        missing = required - _SENSITIVE_METHODS
        self.assertFalse(missing, f"Missing credential methods: {missing}")

    def test_transcript_text_methods_covered(self):
        """Methods carrying full transcript text are in _SENSITIVE_METHODS."""
        required = {
            "translate_text",
            "translate_selection",
            "send_to_telegram",
            "send_imessage",
            "summarize_text",
            "live_subs_ingest",
        }
        missing = required - _SENSITIVE_METHODS
        self.assertFalse(missing, f"Missing transcript-text methods: {missing}")

    def test_file_path_methods_covered(self):
        """Methods carrying file paths are in _SENSITIVE_METHODS."""
        required = {
            "transcribe_paths",
            "transcribe_paths_async",
            "export_timeline_svg",
            "export_timeline_json",
            "export_timeline_ical",
            "configure_obsidian_sync",
        }
        missing = required - _SENSITIVE_METHODS
        self.assertFalse(missing, f"Missing file-path methods: {missing}")

    def test_pii_methods_covered(self):
        """Methods carrying PII (phone, personal data) are in _SENSITIVE_METHODS."""
        required = {
            "create_calendar_event",
            "create_apple_note",
            "create_apple_reminder",
            "call_session_create",
            "register_webhook",
        }
        missing = required - _SENSITIVE_METHODS
        self.assertFalse(missing, f"Missing PII methods: {missing}")

    def test_sensitive_methods_is_frozenset(self):
        """_SENSITIVE_METHODS is a frozenset (immutable guard)."""
        self.assertIsInstance(_SENSITIVE_METHODS, frozenset)

    def test_no_duplicates_in_sensitive_set(self):
        """frozenset guarantees no duplicates — converting to list and back gives same size."""
        as_list = list(_SENSITIVE_METHODS)
        self.assertEqual(len(as_list), len(set(as_list)))


if __name__ == "__main__":
    unittest.main()
