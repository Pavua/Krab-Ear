"""Wave-23 MED — read-path privacy redaction in EventReplayManager.

FINDING (MED, privacy read-leak): redaction was WRITE-time only (record_event).
get_events / replay_events / get_event_stats did NO redaction and returned the
pre-privacy cleartext transcripts (STT_FINAL/PARTIAL/LIVE_SUBS payloads).
Enabling privacy mode at runtime did NOT scrub the existing in-memory ring + file,
so the read path leaked cleartext recorded before privacy was switched on.

FIX (a): get_events / replay_events / get_event_stats now redact every payload
whenever privacy_mode_enabled is active AT READ TIME (a mutable settings flag,
not the write-time value). This makes the service.py comment ("privacy-mode
redaction in get_event_log / replay_events is honoured at runtime") TRUE.

FIX (b, defense-in-depth, lives in service.py): a privacy OFF->ON after-save hook
calls EventReplayManager.clear() so the cleartext ring + event_replay.ndjson are
wiped the moment privacy is enabled. Covered by an inline reimplementation here so
the test does not require constructing the whole BackendService.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Standalone path setup (same pattern as the sibling event_replay tests).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRABEAR_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PROJECT_ROOT), str(KRABEAR_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.event_replay import EventReplayManager  # noqa: E402


class _MutablePrivacy:
    """Settings provider whose privacy flag can be flipped at runtime.

    Mirrors the production provider (``SettingsService.cached_settings``): the
    same callable is read on every record/get, so flipping ``enabled`` changes
    what subsequent reads observe — exactly the runtime-toggle scenario.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def __call__(self) -> dict:
        return {"privacy_mode_enabled": self.enabled}


def _has_cleartext(events: list[dict], needles: list[str]) -> bool:
    """True if any transcript needle appears anywhere in the serialized events."""
    blob = json.dumps(events, ensure_ascii=False)
    return any(n in blob for n in needles)


class TestReadPathRedactionOnRuntimePrivacyFlip(unittest.TestCase):
    """FIX (a): record under privacy-OFF, flip ON, assert reads return no text."""

    SECRETS = ["секретный текст звонка", "confidential transcript", "número 555-1234"]

    def _record_cleartext(self, mgr: EventReplayManager) -> None:
        mgr.record_event("stt.final", {"text": self.SECRETS[0], "confidence": 0.98})
        mgr.record_event("stt.partial", {"text": self.SECRETS[1]})
        mgr.record_event("live_subs.result", {"text": self.SECRETS[2], "lang": "es"})

    def test_get_events_redacts_after_privacy_flip(self):
        privacy = _MutablePrivacy(enabled=False)
        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy)
        try:
            self._record_cleartext(mgr)
            # Sanity: while privacy is OFF, text is visible (baseline).
            self.assertTrue(_has_cleartext(mgr.get_events(), self.SECRETS))

            # Flip privacy ON at runtime — the existing cleartext ring is unchanged
            # on disk, but the read path must now redact it.
            privacy.enabled = True

            events = mgr.get_events(limit=1000)
            self.assertEqual(len(events), 3, "metadata entries are still listed")
            self.assertFalse(
                _has_cleartext(events, self.SECRETS),
                "read path leaked pre-privacy cleartext after privacy flip",
            )
            for ev in events:
                self.assertTrue(ev["data"].get("redacted"))
                self.assertEqual(ev["data"].get("reason"), "privacy_mode")
                self.assertNotIn("text", ev["data"])
                self.assertNotIn("confidence", ev["data"])
                # Metadata preserved for debugging.
                self.assertIn("type", ev)
                self.assertIn("ts", ev)
                self.assertIn("seq", ev)
        finally:
            mgr.close()

    def test_replay_events_redacts_after_privacy_flip(self):
        privacy = _MutablePrivacy(enabled=False)
        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy)
        try:
            self._record_cleartext(mgr)
            privacy.enabled = True

            events = mgr.replay_events("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")
            self.assertEqual(len(events), 3)
            self.assertFalse(
                _has_cleartext(events, self.SECRETS),
                "replay_events leaked pre-privacy cleartext after privacy flip",
            )
            for ev in events:
                self.assertTrue(ev["data"].get("redacted"))
                self.assertNotIn("text", ev["data"])
            # Order is still by seq (redaction preserves seq metadata).
            seqs = [e["seq"] for e in events]
            self.assertEqual(seqs, sorted(seqs))
        finally:
            mgr.close()

    def test_handle_get_event_log_redacts_after_privacy_flip(self):
        privacy = _MutablePrivacy(enabled=False)
        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy)
        try:
            self._record_cleartext(mgr)
            privacy.enabled = True

            result = mgr.handle_get_event_log({"limit": 100})
            self.assertEqual(result["count"], 3)
            self.assertFalse(
                _has_cleartext(result["events"], self.SECRETS),
                "handle_get_event_log leaked pre-privacy cleartext after privacy flip",
            )
        finally:
            mgr.close()

    def test_handle_replay_events_redacts_after_privacy_flip(self):
        import time as _time
        privacy = _MutablePrivacy(enabled=False)
        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy)
        try:
            self._record_cleartext(mgr)
            privacy.enabled = True

            # Use numeric Unix epoch timestamps within the 7-day window cap.
            result = mgr.handle_replay_events(
                {"from_ts": _time.time() - 86400, "to_ts": _time.time() + 86400}
            )
            self.assertEqual(result["count"], 3)
            self.assertFalse(_has_cleartext(result["events"], self.SECRETS))
        finally:
            mgr.close()

    def test_event_type_filter_still_works_under_privacy(self):
        """Read-path filtering relies on metadata, which redaction preserves."""
        privacy = _MutablePrivacy(enabled=False)
        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy)
        try:
            self._record_cleartext(mgr)
            privacy.enabled = True
            events = mgr.get_events(event_type="stt.final")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "stt.final")
            self.assertTrue(events[0]["data"].get("redacted"))
        finally:
            mgr.close()

    def test_stats_safe_under_privacy(self):
        """get_event_stats never carries transcript text; counts stay correct."""
        privacy = _MutablePrivacy(enabled=False)
        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy)
        try:
            self._record_cleartext(mgr)
            privacy.enabled = True
            stats = mgr.get_event_stats()
            self.assertEqual(stats["total_events"], 3)
            self.assertEqual(stats["counts_by_type"]["stt.final"], 1)
            self.assertFalse(_has_cleartext([stats], self.SECRETS))
        finally:
            mgr.close()

    def test_read_returns_cleartext_again_when_privacy_flips_off(self):
        """Redaction tracks the CURRENT flag, not the write-time value.

        record_event under privacy-OFF stored cleartext; reads honour whatever
        privacy is at read time. Flipping back OFF re-exposes the (still-stored)
        cleartext — this documents that fix (a) alone does not destroy data
        (fix (b) in service.py does, on the OFF->ON edge).
        """
        privacy = _MutablePrivacy(enabled=False)
        mgr = EventReplayManager(max_buffer=100, settings_provider=privacy)
        try:
            self._record_cleartext(mgr)
            privacy.enabled = True
            self.assertFalse(_has_cleartext(mgr.get_events(), self.SECRETS))
            privacy.enabled = False
            self.assertTrue(_has_cleartext(mgr.get_events(), self.SECRETS))
        finally:
            mgr.close()


class TestPrivacyOnTransitionWipe(unittest.TestCase):
    """FIX (b): the OFF->ON after-save hook wipes the cleartext ring + file.

    The production hook lives in service.py (registered via
    SettingsService.register_after_save_hook). It calls EventReplayManager.clear()
    on the OFF->ON edge. We reimplement the hook's exact body here to validate the
    contract without constructing the full BackendService.
    """

    @staticmethod
    def _on_privacy_mode_wipe(mgr: EventReplayManager, old: dict, new: dict) -> None:
        old_privacy = bool(old.get("privacy_mode_enabled", False))
        new_privacy = bool(new.get("privacy_mode_enabled", False))
        if not old_privacy and new_privacy:
            mgr.clear()

    def test_hook_wipes_ring_and_file_on_off_to_on(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "event_replay.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=100)
            try:
                mgr.record_event("stt.final", {"text": "cleartext-on-disk"})
                mgr.record_event("stt.partial", {"text": "more-cleartext"})
                # Cleartext is on disk before the transition.
                self.assertIn("cleartext-on-disk", path.read_text(encoding="utf-8"))
                self.assertEqual(mgr.get_event_stats()["total_events"], 2)

                # Simulate privacy_mode flipping OFF -> ON via the after-save hook.
                self._on_privacy_mode_wipe(
                    mgr,
                    {"privacy_mode_enabled": False},
                    {"privacy_mode_enabled": True},
                )

                # Ring + file are wiped (file kept, truncated to empty).
                self.assertEqual(mgr.get_event_stats()["total_events"], 0)
                self.assertTrue(path.exists())
                self.assertEqual(path.read_text(encoding="utf-8"), "")
            finally:
                mgr.close()

    def test_hook_noop_on_on_to_off(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "event_replay.ndjson"
            mgr = EventReplayManager(persist_path=path, max_buffer=100)
            try:
                mgr.record_event("stt.final", {"text": "keep-me"})
                # ON -> OFF must NOT wipe.
                self._on_privacy_mode_wipe(
                    mgr,
                    {"privacy_mode_enabled": True},
                    {"privacy_mode_enabled": False},
                )
                self.assertEqual(mgr.get_event_stats()["total_events"], 1)
            finally:
                mgr.close()

    def test_hook_noop_on_no_change(self):
        mgr = EventReplayManager(max_buffer=100)
        try:
            mgr.record_event("ping", {"x": 1})
            self._on_privacy_mode_wipe(
                mgr,
                {"privacy_mode_enabled": True},
                {"privacy_mode_enabled": True},
            )
            self.assertEqual(mgr.get_event_stats()["total_events"], 1)
        finally:
            mgr.close()


if __name__ == "__main__":
    unittest.main()
