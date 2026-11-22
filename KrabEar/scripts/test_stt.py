import os
import sys
import time

import requests

# Конфигурация
URL = os.environ.get("KRAB_EAR_REST_URL", "http://127.0.0.1:5005").rstrip("/") + "/v1/stt/transcribe"
FILE_PATH = "test_audio.wav"


def _headers():
    key = os.environ.get("KRAB_EAR_REST_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


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


def test_transcribe() -> int:
    if not os.path.exists(FILE_PATH):
        create_test_audio()

    ts = int(time.time())
    data = {
        'chat_id': f'test_chat_{ts}',
        'message_id': f'test_msg_{ts}',
        'quality_profile': 'balanced'
    }

    print(f"Sending request to {URL}...")
    try:
        with open(FILE_PATH, 'rb') as fh:
            response = requests.post(URL, files={'file': fh}, data=data, headers=_headers(), timeout=120)
        print(f"Status Code: {response.status_code}")
        if response.status_code != 200:
            print("FAILED: первый запрос не вернул 200", file=sys.stderr)
            return 1
        res_json = response.json()
        # Содержимое (text/segments) не печатаем: только метаданные.
        print(f"Response text length: {len(res_json.get('text') or '')} chars")
        print(f"Confidence: {res_json.get('confidence')}")
        print(f"Duration MS: {res_json.get('duration_ms')}")
        print(f"Model: {res_json.get('model')}")
        print(f"Segments count: {len(res_json.get('segments', []))}")

        # Проверка идемпотентности: тот же chat_id/message_id → тот же ответ
        print("\nTesting idempotency (sending same request again)...")
        with open(FILE_PATH, 'rb') as fh:
            response_retry = requests.post(URL, files={'file': fh}, data=data, headers=_headers(), timeout=120)
        print(f"Retry Status Code: {response_retry.status_code}")
        if response_retry.status_code != 200:
            print("FAILED: повторный запрос не вернул 200", file=sys.stderr)
            return 1
        if response_retry.json() != res_json:
            print("FAILED: повторный запрос вернул другой ответ (идемпотентность нарушена)", file=sys.stderr)
            return 1
    except Exception as e:
        print(f"Error: {type(e).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(test_transcribe())
