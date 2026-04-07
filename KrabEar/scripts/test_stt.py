import requests
import os
import json
import time

# Конфигурация
URL = "http://127.0.0.1:5005/v1/stt/transcribe"
FILE_PATH = "test_audio.wav"

def create_test_audio():
    # Создаем тихий wav файл для теста нормализации (если нет soundfile, просто пустышка)
    import numpy as np
    import soundfile as sf
    samplerate = 16000
    # 2 секунды синусоиды 440Гц, очень тихой (0.01 амплитуда)
    t = np.linspace(0, 2, 2 * samplerate)
    data = 0.01 * np.sin(2 * np.pi * 440 * t)
    sf.write(FILE_PATH, data, samplerate)
    print(f"Test audio created: {FILE_PATH}")

def test_transcribe():
    if not os.path.exists(FILE_PATH):
        create_test_audio()

    files = {'file': open(FILE_PATH, 'rb')}
    ts = int(time.time())
    data = {
        'chat_id': f'test_chat_{ts}',
        'message_id': f'test_msg_{ts}',
        'quality_profile': 'balanced'
    }

    print(f"Sending request to {URL}...")
    try:
        response = requests.post(URL, files=files, data=data)
        print(f"Status Code: {response.status_code}")
        res_json = response.json()
        print(f"Response (text): {res_json.get('text')}")
        print(f"Confidence: {res_json.get('confidence')}")
        print(f"Duration MS: {res_json.get('duration_ms')}")
        print(f"Model: {res_json.get('model')}")
        print(f"Segments count: {len(res_json.get('segments', []))}")
        
        # Проверка идемпотентности
        print("\nTesting idempotency (sending same request again)...")
        files_retry = {'file': open(FILE_PATH, 'rb')}
        response_retry = requests.post(URL, files=files_retry, data=data)
        print(f"Retry Status Code: {response_retry.status_code}")
        print(f"Retry Response: {json.dumps(response_retry.json(), indent=2, ensure_ascii=False)}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_transcribe()
