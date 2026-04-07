import mlx_whisper
import numpy as np
import soundfile as sf
import os

def test_whisper_direct():
    path = "test_whisper.wav"
    samplerate = 16000
    # 3 seconds of sound
    data = np.random.uniform(-0.1, 0.1, 3 * samplerate).astype(np.float32)
    sf.write(path, data, samplerate)
    
    print(f"Testing mlx_whisper with {path}...")
    try:
        # We use a small model for speed
        result = mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-tiny")
        print("Success!")
        print(f"Text: {result['text']}")
    except Exception as e:
        print(f"Failed with path: {e}")
        
    try:
        print("Testing mlx_whisper with numpy array...")
        result = mlx_whisper.transcribe(data, path_or_hf_repo="mlx-community/whisper-tiny")
        print("Success!")
        print(f"Text: {result['text']}")
    except Exception as e:
        print(f"Failed with numpy: {e}")

if __name__ == "__main__":
    test_whisper_direct()
