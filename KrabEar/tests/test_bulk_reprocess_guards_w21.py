"""Tests for BulkReprocessor W21 security guards.

Finding 1 (MED-1): size cap — sf.info() gate rejects files whose frames*channels
  exceed MAX_AUDIO_FRAMES before any sf.read RAM allocation.
Finding 2 (MED-2): path containment — audio_path resolved through allowlist
  (home / /tmp / tempdir / data_dir) before sf.read; out-of-allowlist paths skipped.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bulk_reprocess import BulkReprocessor, MAX_AUDIO_FRAMES, _validate_audio_read_path  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_item_dict(
    item_id: str,
    audio_path: str,
    confidence: float = 0.5,
    ts: str | None = None,
) -> dict:
    from datetime import datetime, timezone, timedelta
    if ts is None:
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return {
        "id": item_id,
        "ts": ts,
        "text": "Привет мир",
        "paste_status": "ok",
        "source_text": "",
        "translated_text": "",
        "translation_mode": "off",
        "source_lang": "ru",
        "target_lang": "",
        "translation_status": "not_requested",
        "translation_engine": "",
        "chat_id": "",
        "message_id": "",
        "cleaned_text": "",
        "llm_applied": False,
        "llm_latency_ms": 0,
        "diarization": None,
        "audio_duration_sec": None,
        "confidence": confidence,
        "tags": [],
        "favorite": False,
        "emotion": None,
        "word_timestamps": None,
        "speaker_turns": None,
        "reasoning": None,
        "audio_path": audio_path,
        "is_protected": False,
    }


def _make_store_mock(items: list[dict], data_dir: str | None = None) -> MagicMock:
    from backend.models import HistoryItem
    store = MagicMock()
    store.data_dir = data_dir
    history_items = [HistoryItem.from_dict(d) for d in items]
    store._load_active_items_unlocked = MagicMock(return_value=history_items)
    store._lock = MagicMock(return_value=contextlib.nullcontext())
    store.update_history_item_text = MagicMock(return_value=True)
    return store


def _make_reprocessor(
    items: list[dict],
    data_dir: str | None = None,
) -> tuple[BulkReprocessor, MagicMock, MagicMock]:
    """Returns (reprocessor, store_mock, version_manager_mock)."""
    store = _make_store_mock(items, data_dir=data_dir)
    transcriber = MagicMock()
    transcriber.transcribe = MagicMock(return_value={"text": "Новый текст", "confidence": 0.9})
    version_manager = MagicMock()
    reprocessor = BulkReprocessor(store, transcriber, version_manager)
    return reprocessor, store, version_manager


# ---------------------------------------------------------------------------
# W21 MED-1: size cap tests
# ---------------------------------------------------------------------------

class TestSizeCap(unittest.TestCase):
    """sf.info() gate: oversized files are skipped without calling sf.read."""

    def test_oversized_file_is_skipped_no_sf_read(self):
        """Mock sf.info to report frames*channels > MAX_AUDIO_FRAMES; sf.read must NOT be called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_file = os.path.join(tmpdir, "big.wav")
            # Create a 0-byte placeholder so os.path.isfile passes in candidate filtering.
            open(audio_file, "wb").close()

            items = [_make_item_dict("item1", audio_path=audio_file)]
            reprocessor, store, _ = _make_reprocessor(items)

            # Build a fake sf.info result with giant frames*channels.
            fake_info = MagicMock()
            fake_info.frames = MAX_AUDIO_FRAMES + 1
            fake_info.channels = 1

            with patch("backend.bulk_reprocess.Path.home", return_value=Path(tmpdir)):
                with patch("soundfile.info", return_value=fake_info) as mock_info:
                    with patch("soundfile.read") as mock_read:
                        result = reprocessor.reprocess(
                            only_low_confidence=False,
                            dry_run=False,
                        )

            # sf.info was called, sf.read was NOT.
            mock_info.assert_called_once()
            mock_read.assert_not_called()
            # Item counted as error (RuntimeError from size gate).
            self.assertEqual(result["reprocessed"], 0)
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("item1", result["errors"][0])

    def test_oversized_file_stereo_skipped(self):
        """frames*channels check: mono 50M frames OK, stereo 50M frames*2 = 100M skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_file = os.path.join(tmpdir, "stereo.wav")
            open(audio_file, "wb").close()

            items = [_make_item_dict("item_stereo", audio_path=audio_file)]
            reprocessor, _, _ = _make_reprocessor(items)

            # 50_000_001 frames * 2 channels = 100_000_002 > MAX_AUDIO_FRAMES=100_000_000.
            fake_info = MagicMock()
            fake_info.frames = MAX_AUDIO_FRAMES // 2 + 1
            fake_info.channels = 2

            with patch("backend.bulk_reprocess.Path.home", return_value=Path(tmpdir)):
                with patch("soundfile.info", return_value=fake_info):
                    with patch("soundfile.read") as mock_read:
                        result = reprocessor.reprocess(
                            only_low_confidence=False,
                            dry_run=False,
                        )

            mock_read.assert_not_called()
            self.assertEqual(result["reprocessed"], 0)

    def test_exactly_at_limit_is_allowed(self):
        """frames*channels == MAX_AUDIO_FRAMES is within the cap and allowed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_file = os.path.join(tmpdir, "exact.wav")
            open(audio_file, "wb").close()

            items = [_make_item_dict("item_exact", audio_path=audio_file, confidence=0.3)]
            reprocessor, _, _ = _make_reprocessor(items)

            fake_info = MagicMock()
            fake_info.frames = MAX_AUDIO_FRAMES
            fake_info.channels = 1
            import numpy as np
            fake_audio = np.zeros(100, dtype="float32")

            with patch("backend.bulk_reprocess.Path.home", return_value=Path(tmpdir)):
                with patch("soundfile.info", return_value=fake_info):
                    with patch("soundfile.read", return_value=(fake_audio, 16000)) as mock_read:
                        with patch("core.mlx_lock.mlx_lock"):
                            with patch("core.mlx_inter_lock.mlx_inter_process_lock"):
                                reprocessor.reprocess(
                                    only_low_confidence=False,
                                    dry_run=False,
                                )

            # sf.read WAS called because frames*channels == limit (not exceeding).
            mock_read.assert_called_once()

    def test_small_file_proceeds(self):
        """Small file (frames < MAX_AUDIO_FRAMES) calls sf.read normally."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_file = os.path.join(tmpdir, "small.wav")
            open(audio_file, "wb").close()

            items = [_make_item_dict("item_small", audio_path=audio_file, confidence=0.3)]
            reprocessor, _, _ = _make_reprocessor(items)

            fake_info = MagicMock()
            fake_info.frames = 1000
            fake_info.channels = 1
            import numpy as np
            fake_audio = np.zeros(1000, dtype="float32")

            with patch("backend.bulk_reprocess.Path.home", return_value=Path(tmpdir)):
                with patch("soundfile.info", return_value=fake_info):
                    with patch("soundfile.read", return_value=(fake_audio, 16000)) as mock_read:
                        with patch("core.mlx_lock.mlx_lock"):
                            with patch("core.mlx_inter_lock.mlx_inter_process_lock"):
                                result = reprocessor.reprocess(
                                    only_low_confidence=False,
                                    dry_run=False,
                                )

            mock_read.assert_called_once()
            self.assertEqual(result["reprocessed"], 1)
            self.assertEqual(result["errors"], [])


# ---------------------------------------------------------------------------
# W21 MED-2: path containment tests
# ---------------------------------------------------------------------------

class TestPathContainment(unittest.TestCase):
    """audio_path outside allowlist is rejected before sf.read or sf.info."""

    def test_etc_passwd_is_rejected(self):
        """/etc/passwd must be rejected without reading."""
        # Fake that the file exists so candidate filtering passes.
        items = [_make_item_dict("item_etc", audio_path="/etc/passwd")]
        # data_dir=None → allowed roots are home/tmp/tempdir only.
        reprocessor, _, _ = _make_reprocessor(items, data_dir=None)

        with patch("os.path.isfile", return_value=True):
            with patch("soundfile.info") as mock_info:
                with patch("soundfile.read") as mock_read:
                    result = reprocessor.reprocess(
                        only_low_confidence=False,
                        dry_run=False,
                    )

        mock_info.assert_not_called()
        mock_read.assert_not_called()
        self.assertEqual(result["reprocessed"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("item_etc", result["errors"][0])

    def test_path_traversal_rejected(self):
        """/var/root/secret.wav is outside allowlist and must be rejected."""
        malicious = "/var/root/secret.wav"
        items = [_make_item_dict("item_traverse", audio_path=malicious)]
        reprocessor, _, _ = _make_reprocessor(items, data_dir=None)

        with patch("os.path.isfile", return_value=True):
            with patch("soundfile.info") as mock_info:
                with patch("soundfile.read") as mock_read:
                    result = reprocessor.reprocess(
                        only_low_confidence=False,
                        dry_run=False,
                    )

        mock_info.assert_not_called()
        mock_read.assert_not_called()
        self.assertEqual(result["reprocessed"], 0)

    def test_tmp_path_allowed(self):
        """A path inside /tmp is within the allowlist and proceeds normally."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            items = [_make_item_dict("item_tmp", audio_path=tmp_path, confidence=0.3)]
            reprocessor, _, _ = _make_reprocessor(items, data_dir=None)

            fake_info = MagicMock()
            fake_info.frames = 500
            fake_info.channels = 1
            import numpy as np
            fake_audio = np.zeros(500, dtype="float32")

            with patch("soundfile.info", return_value=fake_info):
                with patch("soundfile.read", return_value=(fake_audio, 16000)) as mock_read:
                    with patch("core.mlx_lock.mlx_lock"):
                        with patch("core.mlx_inter_lock.mlx_inter_process_lock"):
                            result = reprocessor.reprocess(
                                only_low_confidence=False,
                                dry_run=False,
                            )

            mock_read.assert_called_once()
            self.assertEqual(result["reprocessed"], 1)
        finally:
            os.unlink(tmp_path)

    def test_home_subpath_allowed(self):
        """A path inside the user home dir is within the allowlist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_file = os.path.join(tmpdir, "recording.wav")
            open(audio_file, "wb").close()

            items = [_make_item_dict("item_home", audio_path=audio_file, confidence=0.3)]
            reprocessor, _, _ = _make_reprocessor(items, data_dir=None)

            fake_info = MagicMock()
            fake_info.frames = 200
            fake_info.channels = 1
            import numpy as np
            fake_audio = np.zeros(200, dtype="float32")

            # Patch Path.home() to return our tmpdir so test is hermetic.
            with patch("backend.bulk_reprocess.Path.home", return_value=Path(tmpdir)):
                with patch("soundfile.info", return_value=fake_info):
                    with patch("soundfile.read", return_value=(fake_audio, 16000)) as mock_read:
                        with patch("core.mlx_lock.mlx_lock"):
                            with patch("core.mlx_inter_lock.mlx_inter_process_lock"):
                                result = reprocessor.reprocess(
                                    only_low_confidence=False,
                                    dry_run=False,
                                )

            mock_read.assert_called_once()
            self.assertEqual(result["reprocessed"], 1)

    def test_data_dir_subpath_allowed(self):
        """A path inside data_dir is within the allowlist when data_dir is provided."""
        with tempfile.TemporaryDirectory() as data_dir:
            audio_file = os.path.join(data_dir, "audio", "rec.wav")
            os.makedirs(os.path.dirname(audio_file), exist_ok=True)
            open(audio_file, "wb").close()

            items = [_make_item_dict("item_datadir", audio_path=audio_file, confidence=0.3)]
            store = _make_store_mock(items, data_dir=data_dir)
            transcriber = MagicMock()
            transcriber.transcribe = MagicMock(
                return_value={"text": "Новый текст", "confidence": 0.9}
            )
            version_manager = MagicMock()
            reprocessor = BulkReprocessor(store, transcriber, version_manager)

            fake_info = MagicMock()
            fake_info.frames = 300
            fake_info.channels = 1
            import numpy as np
            fake_audio = np.zeros(300, dtype="float32")

            with patch("soundfile.info", return_value=fake_info):
                with patch("soundfile.read", return_value=(fake_audio, 16000)) as mock_read:
                    with patch("core.mlx_lock.mlx_lock"):
                        with patch("core.mlx_inter_lock.mlx_inter_process_lock"):
                            result = reprocessor.reprocess(
                                only_low_confidence=False,
                                dry_run=False,
                            )

            mock_read.assert_called_once()
            self.assertEqual(result["reprocessed"], 1)

    def test_out_of_allowlist_path_skipped_batch_continues(self):
        """Multiple items: bad-path item is skipped+logged; subsequent items proceed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            good_file = os.path.join(tmpdir, "good.wav")
            open(good_file, "wb").close()

            items = [
                _make_item_dict("item_bad", audio_path="/etc/shadow", confidence=0.3),
                _make_item_dict("item_good", audio_path=good_file, confidence=0.3),
            ]
            reprocessor, _, _ = _make_reprocessor(items, data_dir=None)

            fake_info = MagicMock()
            fake_info.frames = 100
            fake_info.channels = 1
            import numpy as np
            fake_audio = np.zeros(100, dtype="float32")

            with patch("os.path.isfile", return_value=True):
                with patch("backend.bulk_reprocess.Path.home", return_value=Path(tmpdir)):
                    with patch("soundfile.info", return_value=fake_info):
                        with patch("soundfile.read", return_value=(fake_audio, 16000)):
                            with patch("core.mlx_lock.mlx_lock"):
                                with patch("core.mlx_inter_lock.mlx_inter_process_lock"):
                                    result = reprocessor.reprocess(
                                        only_low_confidence=False,
                                        dry_run=False,
                                    )

            # One error (bad path), one success (good file).
            self.assertEqual(result["reprocessed"], 1)
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("item_bad", result["errors"][0])


# ---------------------------------------------------------------------------
# _validate_audio_read_path unit tests (module-level helper)
# ---------------------------------------------------------------------------

class TestValidateAudioReadPath(unittest.TestCase):
    """Direct unit tests of the module-level allowlist helper."""

    def test_tmp_always_allowed(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        try:
            result = _validate_audio_read_path(tmp_path, data_dir=None)
            self.assertEqual(result, Path(tmp_path).expanduser().resolve())
        finally:
            os.unlink(tmp_path)

    def test_home_subpath_allowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio = os.path.join(tmpdir, "audio.wav")
            with patch("backend.bulk_reprocess.Path.home", return_value=Path(tmpdir)):
                result = _validate_audio_read_path(audio, data_dir=None)
            self.assertEqual(result, Path(audio).expanduser().resolve())

    def test_root_etc_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _validate_audio_read_path("/etc/passwd", data_dir=None)
        self.assertIn("разрешённых директорий", str(ctx.exception))

    def test_data_dir_subpath_allowed(self):
        with tempfile.TemporaryDirectory() as data_dir:
            audio = os.path.join(data_dir, "a", "b.wav")
            result = _validate_audio_read_path(audio, data_dir=Path(data_dir))
            self.assertEqual(result, Path(audio).expanduser().resolve())

    def test_etc_rejected_even_with_data_dir(self):
        """Paths in /etc are rejected even when data_dir is provided."""
        with tempfile.TemporaryDirectory() as data_dir:
            with self.assertRaises(ValueError):
                _validate_audio_read_path("/etc/shadow", data_dir=Path(data_dir))


if __name__ == "__main__":
    unittest.main()
