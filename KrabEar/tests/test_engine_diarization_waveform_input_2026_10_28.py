"""Диаризация подаёт pyannote загруженный waveform, а не путь к файлу.

Корень (2026-10-28): pyannote 4 декодирует путь через torchcodec, а тот грузит
libtorchcodec_core{N} только под FFmpeg major 4–8. После обновления brew-FFmpeg
до 9 каждый прогон падал `NameError: AudioDecoder`, диаризация молча отключалась.
Словарь {'waveform', 'sample_rate'} обходит torchcodec; при сбое чтения остаётся путь.
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import soundfile as sf
    import torch
except Exception:  # ubuntu-CI без системных библиотек
    sf = None
    torch = None

_needs_audio_stack = unittest.skipUnless(
    sf is not None and torch is not None, "нужны soundfile и torch"
)


class _Turn:
    def __init__(self, start, end):
        self.start = start
        self.end = end


class _Annotation:
    def itertracks(self, yield_label=False):
        yield _Turn(0.0, 1.0), "_", "SPEAKER_00"

    def labels(self):
        return ["SPEAKER_00"]


class _Pipeline:
    def __init__(self):
        self.calls = []

    def __call__(self, audio):
        self.calls.append(audio)
        return _Annotation()


def _engine():
    from core.engine import AudioEngine
    eng = AudioEngine.__new__(AudioEngine)
    eng._diarization_pipeline = None
    eng._diarization_load_error = ""
    eng._diarization_load_lock = threading.RLock()
    eng._diarization_run_lock = threading.Lock()
    return eng


class DiarizationPipelineInputTests(unittest.TestCase):
    @_needs_audio_stack
    def test_stereo_wav_becomes_mono_waveform_dict(self):
        from core.engine import AudioEngine
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stereo.wav"
            sf.write(str(path), np.zeros((8000, 2), dtype=np.float32), 16000)
            result = AudioEngine._diarization_pipeline_input(str(path))
        self.assertIsInstance(result, dict)
        self.assertEqual(tuple(result["waveform"].shape), (1, 8000))
        self.assertEqual(result["sample_rate"], 16000)
        self.assertEqual(result["uri"], "stereo")

    def test_missing_file_falls_back_to_path(self):
        from core.engine import AudioEngine
        self.assertEqual(
            AudioEngine._diarization_pipeline_input("/nonexistent/win.wav"),
            "/nonexistent/win.wav",
        )

    def test_without_torch_falls_back_to_path(self):
        from core.engine import AudioEngine
        with patch("core.engine.torch", None):
            self.assertEqual(AudioEngine._diarization_pipeline_input("/tmp/a.wav"), "/tmp/a.wav")

    @_needs_audio_stack
    def test_diarize_window_passes_waveform_to_pipeline(self):
        eng = _engine()
        pipeline = _Pipeline()
        eng._diarization_pipeline = pipeline
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "win.wav"
            sf.write(str(path), np.zeros(4000, dtype=np.float32), 16000)
            eng.diarize_window(str(path))
        self.assertIsInstance(pipeline.calls[0], dict)
        self.assertEqual(tuple(pipeline.calls[0]["waveform"].shape), (1, 4000))

    @_needs_audio_stack
    def test_run_diarization_impl_passes_waveform_to_pipeline(self):
        eng = _engine()
        pipeline = _Pipeline()
        eng._diarization_pipeline = pipeline
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meeting.wav"
            sf.write(str(path), np.zeros(4000, dtype=np.float32), 16000)
            segments = eng._run_diarization_impl(str(path))
        self.assertIsInstance(pipeline.calls[0], dict)
        self.assertEqual(segments[0]["speaker"], "SPEAKER_00")


if __name__ == "__main__":
    unittest.main()
