"""W2b: офлайн-замер аномалии длительности VAD vs chunker vs WAV.

Не ходит в прод-сокет и не гоняет GigaAM/MLX.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "measure_duration_anomaly.py"


def _write_sine_wav(path: Path, duration_sec: float = 0.5, sample_rate: int = 16000) -> None:
    import soundfile as sf

    n = int(duration_sec * sample_rate)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    audio = (0.1 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    sf.write(str(path), audio, sample_rate)


class MeasureDurationAnomalyScriptTests(unittest.TestCase):

    def test_script_prints_three_durations_and_deltas(self) -> None:
        self.assertTrue(SCRIPT.is_file(), f"нет скрипта {SCRIPT}")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "clip.wav"
            _write_sine_wav(wav, duration_sec=0.5, sample_rate=16000)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO_ROOT / "KrabEar")
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(wav)],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        for key in (
            "wav_duration_sec",
            "vad_total_sec",
            "chunker_duration_sec",
            "delta_chunker_minus_vad",
            "delta_chunker_minus_wav",
        ):
            self.assertIn(key, payload, key)
            self.assertIsInstance(payload[key], (int, float), key)
        self.assertAlmostEqual(payload["wav_duration_sec"], 0.5, places=2)
        self.assertAlmostEqual(payload["vad_total_sec"], payload["wav_duration_sec"], places=2)
        self.assertAlmostEqual(
            payload["chunker_duration_sec"], payload["wav_duration_sec"], places=2,
        )


if __name__ == "__main__":
    unittest.main()
