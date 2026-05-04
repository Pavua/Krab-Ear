import sys
import os

# Allow running from repo root or from KrabEar/ subdirectory
_script_dir = os.path.dirname(os.path.abspath(__file__))
_krabear_dir = os.path.dirname(_script_dir)
if _krabear_dir not in sys.path:
    sys.path.insert(0, _krabear_dir)

import mlx_whisper
import numpy as np
import soundfile as sf

from core.mlx_lock import mlx_lock


def test_whisper_direct():
    path = "test_whisper.wav"
    samplerate = 16000
    # 3 seconds of sound
    data = np.random.uniform(-0.1, 0.1, 3 * samplerate).astype(np.float32)
    sf.write(path, data, samplerate)

    print(f"Testing mlx_whisper with {path}...")
    try:
        # We use a small model for speed
        with mlx_lock():
            result = mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-tiny")
        print("Success!")
        print(f"Text: {result['text']}")
    except Exception as e:
        print(f"Failed with path: {e}")

    try:
        print("Testing mlx_whisper with numpy array...")
        with mlx_lock():
            result = mlx_whisper.transcribe(data, path_or_hf_repo="mlx-community/whisper-tiny")
        print("Success!")
        print(f"Text: {result['text']}")
    except Exception as e:
        print(f"Failed with numpy: {e}")


if __name__ == "__main__":
    test_whisper_direct()
