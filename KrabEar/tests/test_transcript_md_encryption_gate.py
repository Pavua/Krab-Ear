"""crypto-audit (2026-06-20): plaintext .md transcript gate respects encryption.

`RecordingCoreService._should_write_plaintext_md` — статический helper, решающий,
можно ли писать plaintext .md-сайдкар транскрипта на диск. При включённом
шифровании истории (history_encryption_enabled) ИЛИ privacy_mode — НЕ пишем,
иначе открытый .md подрывал бы шифрование-at-rest. Live-путь дополнительно требует
auto_save_transcripts; import-путь — нет.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService  # noqa: E402

_gate = RecordingCoreService._should_write_plaintext_md


class TranscriptMdEncryptionGateTest(unittest.TestCase):
    # ── Live path (require_auto_save=True) ──────────────────────────────
    def test_live_all_clear_writes(self) -> None:
        self.assertTrue(_gate({"auto_save_transcripts": True}, False, require_auto_save=True))

    def test_live_encryption_on_skips(self) -> None:
        s = {"auto_save_transcripts": True, "history_encryption_enabled": True}
        self.assertFalse(_gate(s, False, require_auto_save=True))

    def test_live_privacy_mode_skips(self) -> None:
        self.assertFalse(_gate({"auto_save_transcripts": True}, True, require_auto_save=True))

    def test_live_no_autosave_skips(self) -> None:
        self.assertFalse(_gate({"auto_save_transcripts": False}, False, require_auto_save=True))

    # ── Import path (require_auto_save=False — пишет .md без флага) ──────
    def test_import_all_clear_writes(self) -> None:
        self.assertTrue(_gate({}, False, require_auto_save=False))

    def test_import_encryption_on_skips(self) -> None:
        self.assertFalse(_gate({"history_encryption_enabled": True}, False, require_auto_save=False))

    def test_import_privacy_skips(self) -> None:
        self.assertFalse(_gate({}, True, require_auto_save=False))

    # ── coercion (cached_settings может вернуть строку) ─────────────────
    def test_encryption_string_true_coerced(self) -> None:
        s = {"auto_save_transcripts": True, "history_encryption_enabled": "true"}
        self.assertFalse(_gate(s, False, require_auto_save=True))


if __name__ == "__main__":
    unittest.main()
