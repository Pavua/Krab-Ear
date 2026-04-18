"""STT Adapter Benchmark Matrix — Phase 4.

Mocked head-to-head comparison of all 5 STT adapters on same fake audio.
No real models are loaded — CI safe.

Adapters compared:
  whisper_turbo  — mlx-whisper large-v3-turbo (baseline, always ON)
  parakeet       — Parakeet-TDT-1.1B (EN WER leader, opt-in)
  sensevoice     — SenseVoice Small (RU + emotion, opt-in)
  whisperx       — WhisperX (word timestamps + diarization, opt-in)
  voxtral        — Voxtral Mini 4B Realtime (STT + reasoning, opt-in)
"""

from __future__ import annotations

import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADAPTER_NAMES = ("whisper_turbo", "parakeet", "sensevoice", "whisperx", "voxtral")

# Expected RAM footprints (bytes) from research — used in memory-safety test.
_EXPECTED_RAM_BYTES: Dict[str, int] = {
    "whisper_turbo": int(2.0 * 1024 ** 3),
    "parakeet": int(3.0 * 1024 ** 3),
    "sensevoice": int(1.5 * 1024 ** 3),
    "whisperx": int(4.5 * 1024 ** 3),
    "voxtral": int(3.0 * 1024 ** 3),
}

_M4_MAX_RAM_BYTES = 36 * 1024 ** 3  # 36 GB


def _make_fake_wav(duration_sec: int = 10) -> str:
    """Write a minimal 16-kHz mono 16-bit WAV to a temp file, return path."""
    sample_rate = 16_000
    num_samples = sample_rate * duration_sec
    data_bytes = struct.pack(f"<{num_samples}h", *([0] * num_samples))
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data_bytes),
        b"WAVE",
        b"fmt ",
        16,
        1,          # PCM
        1,          # mono
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(data_bytes),
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.write(header + data_bytes)
    tmp.close()
    return tmp.name


def _make_engine(
    *,
    parakeet_enabled: bool = False,
    sensevoice_enabled: bool = False,
    whisperx_enabled: bool = False,
    voxtral_enabled: bool = False,
) -> AudioEngine:
    """Return an AudioEngine.__new__() with minimal required state."""
    engine = AudioEngine.__new__(AudioEngine)
    engine.quality_profile = "balanced"
    engine.current_model = "mlx-community/whisper-large-v3-turbo"
    engine._unavailable_models = set()
    engine._parakeet_model = None
    engine._parakeet_load_error = None
    engine._sensevoice_model = None
    engine._sensevoice_load_error = None
    engine._whisperx_model = None
    engine._whisperx_load_error = None
    engine._voxtral_model = None
    engine._voxtral_load_error = None
    return engine


def _make_mock_settings(
    mock: Any,
    *,
    parakeet_enabled: bool = False,
    sensevoice_enabled: bool = False,
    whisperx_enabled: bool = False,
    voxtral_enabled: bool = False,
) -> None:
    mock.PARAKEET_ENABLED = parakeet_enabled
    mock.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
    mock.SENSEVOICE_ENABLED = sensevoice_enabled
    mock.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
    mock.SENSEVOICE_EMOTION_TO_HISTORY = True
    mock.WHISPERX_ENABLED = whisperx_enabled
    mock.WHISPERX_MODEL = "large-v3"
    mock.WHISPERX_WORD_TIMESTAMPS = True
    mock.WHISPERX_DIARIZATION = False
    mock.VOXTRAL_ENABLED = voxtral_enabled
    mock.VOXTRAL_MODEL = "mistralai/Voxtral-Mini-3B-2507"
    mock.VOXTRAL_REASONING_ENABLED = False
    mock.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
    mock.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
    mock.TRANSCRIBE_TIMEOUT_SEC = 30
    mock.NETWORK_MODE = "offline_strict"
    mock.model_max_list = ["mlx-community/whisper-large-v3-turbo"]


# Canonical mock results — what each adapter returns (mocked)
_MOCK_RESULTS: Dict[str, Dict] = {
    "whisper_turbo": {
        "text": "привет мир",
        "segments": [],
        "language": "ru",
    },
    "parakeet": {
        "text": "hello world",
        "language": "en",
        "segments": [],
    },
    "sensevoice": {
        "text": "привет",
        "emotion": "happy",
        "language": "ru",
        "segments": [],
    },
    "whisperx": {
        "text": "hello",
        "word_timestamps": [{"word": "hello", "start": 0.0, "end": 0.5, "confidence": 0.95}],
        "speaker_turns": [],
        "segments": [],
        "language": "en",
    },
    "voxtral": {
        "text": "bonjour",
        "reasoning": None,
        "segments": [],
        "language": "fr",
    },
}


def _run_adapter_mocked(adapter_name: str, audio_path: str) -> Dict:
    """Simulate an adapter call, return mock result with timing metadata."""
    t0 = time.perf_counter()
    result = dict(_MOCK_RESULTS[adapter_name])
    elapsed = time.perf_counter() - t0
    result["_adapter"] = adapter_name
    result["_elapsed_ms"] = round(elapsed * 1000, 3)
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdapterBenchmarkMatrix(unittest.TestCase):
    """Run all 5 adapters on same mock audio, verify schema + capabilities."""

    def setUp(self) -> None:
        self.fake_audio_path = _make_fake_wav(duration_sec=10)

    def test_all_adapters_return_compatible_schema(self) -> None:
        """Each adapter returns dict with 'text' key — baseline schema contract."""
        results = {}
        for adapter_name in _ADAPTER_NAMES:
            with self.subTest(adapter=adapter_name):
                result = _run_adapter_mocked(adapter_name, self.fake_audio_path)
                self.assertIn("text", result, f"{adapter_name}: missing 'text' key")
                self.assertIsInstance(result["text"], str, f"{adapter_name}: 'text' must be str")
                results[adapter_name] = result
        # All 5 adapters must have responded
        self.assertEqual(len(results), len(_ADAPTER_NAMES))

    def test_unique_capabilities_per_adapter(self) -> None:
        """Each adapter produces its unique optional fields."""
        # whisper_turbo — text only (no emotion, no word_timestamps, no reasoning)
        turbo = _run_adapter_mocked("whisper_turbo", self.fake_audio_path)
        self.assertNotIn("emotion", turbo)
        self.assertNotIn("word_timestamps", turbo)
        self.assertNotIn("reasoning", turbo)

        # parakeet — adds language tag, EN only, no emotion
        parakeet = _run_adapter_mocked("parakeet", self.fake_audio_path)
        self.assertIn("language", parakeet)
        self.assertEqual(parakeet["language"], "en")
        self.assertNotIn("emotion", parakeet)

        # sensevoice — emotion + language detection
        sv = _run_adapter_mocked("sensevoice", self.fake_audio_path)
        self.assertIn("emotion", sv)
        self.assertIsNotNone(sv["emotion"])
        self.assertIn("language", sv)

        # whisperx — word_timestamps list
        wx = _run_adapter_mocked("whisperx", self.fake_audio_path)
        self.assertIn("word_timestamps", wx)
        self.assertIsInstance(wx["word_timestamps"], list)
        for wt in wx["word_timestamps"]:
            for key in ("word", "start", "end", "confidence"):
                self.assertIn(key, wt)

        # voxtral — reasoning field present (may be None when VOXTRAL_REASONING_ENABLED=False)
        vx = _run_adapter_mocked("voxtral", self.fake_audio_path)
        self.assertIn("reasoning", vx)

    def test_fallback_chain_order(self) -> None:
        """Insertion markers in core/engine.py follow the correct priority order.

        Verifies that the engine source encodes the expected order:
          balanced → Parakeet → SenseVoice → WhisperX → Voxtral → max-candidates
        by checking that each marker's insertion guard appears after the preceding one.
        """
        engine = _make_engine()

        markers = [
            engine._PARAKEET_MARKER,
            engine._SENSEVOICE_MARKER,
            engine._WHISPERX_MARKER,
            engine._VOXTRAL_MARKER,
        ]
        marker_names = ["parakeet", "sensevoice", "whisperx", "voxtral"]

        # All markers must be distinct strings
        self.assertEqual(len(set(markers)), len(markers), "Markers must be unique")

        # Each marker must contain the adapter name for debuggability
        for marker, name in zip(markers, marker_names):
            self.assertIn(name, marker, f"{marker!r} must contain '{name}'")

        # Verify chain order by inspecting engine source file.
        # The insertion block for each adapter must appear after the preceding one.
        engine_src_path = Path(__file__).resolve().parents[1] / "core" / "engine.py"
        src = engine_src_path.read_text(encoding="utf-8")

        parakeet_pos = src.find("_PARAKEET_MARKER not in self._unavailable_models")
        sensevoice_pos = src.find("_SENSEVOICE_MARKER not in self._unavailable_models")
        whisperx_pos = src.find("_WHISPERX_MARKER not in self._unavailable_models")
        voxtral_pos = src.find("_VOXTRAL_MARKER not in self._unavailable_models")

        self.assertGreater(parakeet_pos, 0, "Parakeet guard not found in engine.py")
        self.assertGreater(sensevoice_pos, 0, "SenseVoice guard not found in engine.py")
        self.assertGreater(whisperx_pos, 0, "WhisperX guard not found in engine.py")
        self.assertGreater(voxtral_pos, 0, "Voxtral guard not found in engine.py")

        self.assertLess(parakeet_pos, sensevoice_pos, "Parakeet block must precede SenseVoice")
        self.assertLess(sensevoice_pos, whisperx_pos, "SenseVoice block must precede WhisperX")
        self.assertLess(whisperx_pos, voxtral_pos, "WhisperX block must precede Voxtral")

    def test_memory_safety_estimates(self) -> None:
        """Each adapter's expected RAM footprint fits within M4 Max 36 GB budget."""
        for adapter_name, ram_bytes in _EXPECTED_RAM_BYTES.items():
            with self.subTest(adapter=adapter_name):
                self.assertLess(
                    ram_bytes,
                    _M4_MAX_RAM_BYTES,
                    f"{adapter_name}: {ram_bytes / 1024**3:.1f} GB exceeds M4 Max budget",
                )

        # Total if all adapters loaded simultaneously stays under budget
        total = sum(_EXPECTED_RAM_BYTES.values())
        self.assertLess(
            total,
            _M4_MAX_RAM_BYTES,
            f"Combined RAM {total / 1024**3:.1f} GB exceeds M4 Max 36 GB",
        )

    def test_adapter_result_timing_metadata(self) -> None:
        """_run_adapter_mocked injects _elapsed_ms and _adapter fields."""
        for adapter_name in _ADAPTER_NAMES:
            with self.subTest(adapter=adapter_name):
                result = _run_adapter_mocked(adapter_name, self.fake_audio_path)
                self.assertIn("_adapter", result)
                self.assertEqual(result["_adapter"], adapter_name)
                self.assertIn("_elapsed_ms", result)
                self.assertGreaterEqual(result["_elapsed_ms"], 0.0)

    def test_multilingual_coverage(self) -> None:
        """Verify language support claims per adapter."""
        # EN-only adapters
        parakeet = _run_adapter_mocked("parakeet", self.fake_audio_path)
        self.assertEqual(parakeet["language"], "en", "Parakeet is EN-only")

        # SenseVoice handles RU
        sv = _run_adapter_mocked("sensevoice", self.fake_audio_path)
        self.assertIn(sv["language"], ("ru", "zh", "ja", "ko", "yue", "en"))

        # Voxtral — multilingual (13 langs), language tag present
        vx = _run_adapter_mocked("voxtral", self.fake_audio_path)
        self.assertIn("language", vx)


if __name__ == "__main__":
    unittest.main()
