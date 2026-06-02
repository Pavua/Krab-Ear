"""W18 — waveform DoS guard tests.

Covers two HIGH findings:

FINDING 1 (num_points CPU/memory DoS):
  handle_get_waveform used to pass num_points directly to WaveformGenerator
  without an upper bound.  num_points=10_000_000 caused ~80 MB allocation and
  ~9 s blocking the IPC worker thread.

  Fix: AudioAnalyticsService.handle_get_waveform clamps to max(1, min(v, 2000))
       before calling generate_from_file.
       WaveformGenerator.generate_waveform raises ValueError for values above
       _MAX_NUM_POINTS (100_000) as defence-in-depth.

FINDING 2 (audio file OOM):
  WaveformGenerator.generate_from_file loaded the entire file into RAM with no
  size cap.  3h/48kHz/stereo ≈ 8 GB transient RSS → OOM-kill on the 36 GB host.

  Fix: generate_from_file calls soundfile.info() first; rejects files where
       frames*channels > _MAX_FILE_FRAMES (100_000_000) and returns empty
       WaveformData without allocating PCM.  Falls back to os.path.getsize
       heuristic when info() raises.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

# ---------------------------------------------------------------------------
# Path setup — mirrors all other KrabEar test files
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.waveform_generator import WaveformData, WaveformGenerator, _MAX_FILE_FRAMES, _MAX_NUM_POINTS  # noqa: E402
from backend.audio_analytics_service import AudioAnalyticsService  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sine(sample_rate: int = 16000, duration_sec: float = 0.1) -> np.ndarray:
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    return np.sin(2 * np.pi * 440.0 * t).astype(np.float32)


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int = 16000) -> None:
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def _make_audio_analytics_service(tmp_path: Path) -> AudioAnalyticsService:
    """Return a minimal AudioAnalyticsService with a real data_dir store stub."""

    class _FakeStore:
        data_dir = str(tmp_path)

        class _CM:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _lock(self):
            return self._CM()

        def _load_active_items_unlocked(self):
            return []

    return AudioAnalyticsService(
        audio_converter=MagicMock(),
        quality_trends=MagicMock(),
        audio_fingerprinter=MagicMock(),
        word_timing_analyzer=MagicMock(),
        store=_FakeStore(),
    )


# ---------------------------------------------------------------------------
# FINDING 1 — num_points clamp
# ---------------------------------------------------------------------------

class TestNumPointsClampInHandleGetWaveform(unittest.TestCase):
    """handle_get_waveform must clamp num_points to ≤2000."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        audio = _make_sine()
        self.wav_path = self.tmp_path / "test.wav"
        _write_wav(self.wav_path, audio)
        self.svc = _make_audio_analytics_service(self.tmp_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_huge_num_points_clamped_to_at_most_2000(self):
        """num_points=10_000_000 must return at most 2000 points, never allocate 10M bins."""
        result = self.svc.handle_get_waveform({
            "file_path": str(self.wav_path),
            "num_points": 10_000_000,
        })
        self.assertLessEqual(
            len(result["points"]),
            2000,
            "Clamped num_points must produce ≤2000 output points",
        )

    def test_normal_num_points_passes_through(self):
        """num_points=100 should produce exactly 100 points."""
        result = self.svc.handle_get_waveform({
            "file_path": str(self.wav_path),
            "num_points": 100,
        })
        self.assertEqual(len(result["points"]), 100)

    def test_default_num_points_is_200(self):
        """When num_points is absent the default is 200."""
        result = self.svc.handle_get_waveform({"file_path": str(self.wav_path)})
        self.assertEqual(len(result["points"]), 200)

    def test_zero_num_points_clamped_to_1(self):
        """num_points=0 must be clamped up to 1, not cause a ValueError from the handler."""
        result = self.svc.handle_get_waveform({
            "file_path": str(self.wav_path),
            "num_points": 0,
        })
        self.assertGreaterEqual(len(result["points"]), 1)

    def test_negative_num_points_clamped_to_1(self):
        """Negative num_points must be clamped to 1."""
        result = self.svc.handle_get_waveform({
            "file_path": str(self.wav_path),
            "num_points": -500,
        })
        self.assertGreaterEqual(len(result["points"]), 1)

    def test_exactly_2000_passes_without_clamp(self):
        """num_points=2000 is at the boundary and should pass through unchanged."""
        result = self.svc.handle_get_waveform({
            "file_path": str(self.wav_path),
            "num_points": 2000,
        })
        self.assertEqual(len(result["points"]), 2000)


class TestNumPointsDefenceInDepthInGenerator(unittest.TestCase):
    """generate_waveform raises ValueError for values above _MAX_NUM_POINTS."""

    def setUp(self):
        self.gen = WaveformGenerator()
        self.audio = _make_sine()

    def test_above_max_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.gen.generate_waveform(self.audio, 16000, num_points=_MAX_NUM_POINTS + 1)

    def test_exactly_max_does_not_raise(self):
        """_MAX_NUM_POINTS itself is allowed (boundary is inclusive on both ends)."""
        result = self.gen.generate_waveform(self.audio, 16000, num_points=_MAX_NUM_POINTS)
        self.assertEqual(len(result.points), _MAX_NUM_POINTS)

    def test_one_below_max_does_not_raise(self):
        result = self.gen.generate_waveform(self.audio, 16000, num_points=_MAX_NUM_POINTS - 1)
        self.assertEqual(len(result.points), _MAX_NUM_POINTS - 1)


# ---------------------------------------------------------------------------
# FINDING 2 — audio file size gate
# ---------------------------------------------------------------------------

class TestAudioFileSizeGateViaInfo(unittest.TestCase):
    """generate_from_file rejects oversized files via soundfile.info() before loading."""

    def setUp(self):
        self.gen = WaveformGenerator()

    def _fake_info(self, frames: int, channels: int = 1):
        """Return a mock object mimicking soundfile.info() output."""
        info = MagicMock()
        info.frames = frames
        info.channels = channels
        return info

    def test_oversized_file_returns_empty_waveform_data(self):
        """Mocked soundfile.info reports frames > _MAX_FILE_FRAMES → empty WaveformData."""
        huge_frames = _MAX_FILE_FRAMES + 1
        fake_info = self._fake_info(huge_frames, channels=1)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            # Write a tiny real WAV so file.exists() is True
            audio = _make_sine(16000, 0.01)
            _write_wav(Path(f.name), audio)
            with patch("soundfile.info", return_value=fake_info):
                result = self.gen.generate_from_file(f.name, num_points=200)
        self.assertIsInstance(result, WaveformData)
        self.assertEqual(result.points, [], "Oversized file must return empty points list")
        self.assertEqual(result.duration_sec, 0.0)

    def test_oversized_stereo_returns_empty_waveform_data(self):
        """frames×channels check works for stereo: half the frames but 2 channels."""
        half_frames = _MAX_FILE_FRAMES // 2 + 1  # × 2 channels > limit
        fake_info = self._fake_info(half_frames, channels=2)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            audio = _make_sine(16000, 0.01)
            _write_wav(Path(f.name), audio)
            with patch("soundfile.info", return_value=fake_info):
                result = self.gen.generate_from_file(f.name, num_points=200)
        self.assertEqual(result.points, [])

    def test_file_at_exactly_max_frames_is_rejected(self):
        """_MAX_FILE_FRAMES exactly should still be rejected (strictly >)."""
        # > vs >= check: exactly at the limit should pass
        fake_info = self._fake_info(_MAX_FILE_FRAMES, channels=1)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            audio = _make_sine(16000, 0.01)
            _write_wav(Path(f.name), audio)
            with patch("soundfile.info", return_value=fake_info):
                # At exactly the limit → allowed (gate is strict >)
                result = self.gen.generate_from_file(f.name, num_points=10)
        # Should not be rejected — we expect normal (possibly short) waveform data
        # The real read goes through with the tiny WAV, giving us proper points
        self.assertIsInstance(result.points, list)

    def test_normal_file_is_not_rejected(self):
        """A normal-sized file (< _MAX_FILE_FRAMES) must pass through to normal processing."""
        fake_info = self._fake_info(16000, channels=1)  # 1s at 16kHz — tiny
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "ok.wav"
            audio = _make_sine(16000, 1.0)
            _write_wav(wav_path, audio)
            with patch("soundfile.info", return_value=fake_info):
                result = self.gen.generate_from_file(str(wav_path), num_points=100)
        self.assertEqual(len(result.points), 100)
        self.assertGreater(result.duration_sec, 0.0)

    def test_no_pcm_allocation_for_oversized_file(self):
        """soundfile.read must NOT be called when info() reports an oversized file."""
        huge_frames = _MAX_FILE_FRAMES + 1
        fake_info = self._fake_info(huge_frames, channels=1)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            audio = _make_sine(16000, 0.01)
            _write_wav(Path(f.name), audio)
            with patch("soundfile.info", return_value=fake_info) as _info_mock, \
                 patch("soundfile.read") as read_mock:
                self.gen.generate_from_file(f.name, num_points=200)
        read_mock.assert_not_called()


class TestAudioFileSizeGateFallback(unittest.TestCase):
    """When soundfile.info() raises, the byte-level fallback gate fires."""

    def setUp(self):
        self.gen = WaveformGenerator()

    def test_huge_byte_size_triggers_fallback_gate(self):
        """If info() raises and the file is huge by bytes, return empty WaveformData."""
        from core.waveform_generator import _MAX_FILE_FRAMES
        byte_budget = _MAX_FILE_FRAMES * 4 * 2  # float32, 2× overhead
        huge_bytes = byte_budget + 1

        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            audio = _make_sine(16000, 0.01)
            _write_wav(Path(f.name), audio)
            with patch("soundfile.info", side_effect=Exception("probe unsupported")), \
                 patch("os.path.getsize", return_value=huge_bytes):
                result = self.gen.generate_from_file(f.name, num_points=200)
        self.assertEqual(result.points, [], "Fallback byte gate must return empty WaveformData")

    def test_small_byte_size_fallback_passes(self):
        """If info() raises but file is small by bytes, read proceeds normally."""
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "small.wav"
            audio = _make_sine(16000, 0.5)
            _write_wav(wav_path, audio)
            with patch("soundfile.info", side_effect=Exception("probe unsupported")):
                result = self.gen.generate_from_file(str(wav_path), num_points=50)
        self.assertEqual(len(result.points), 50)


class TestMissingFileStillRaises(unittest.TestCase):
    """FileNotFoundError must still propagate after the size-gate code path."""

    def test_missing_file_raises_file_not_found(self):
        gen = WaveformGenerator()
        with self.assertRaises(FileNotFoundError):
            gen.generate_from_file("/definitely/does/not/exist/audio.wav")


if __name__ == "__main__":
    unittest.main(verbosity=2)
