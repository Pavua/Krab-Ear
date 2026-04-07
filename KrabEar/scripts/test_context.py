#!/usr/bin/env python3
import requests
import json
import time

URL_BASE = "http://127.0.0.1:5005"
API_STT = f"{URL_BASE}/v1/stt/transcribe"
API_VOCAB = f"{URL_BASE}/v1/vocabulary"

def test_context():
    # 1. Добавляем слово в словарь
    print("Updating vocabulary...")
    res = requests.post(API_VOCAB, json={"words": ["Antigravity", "Pablito"]})
    print(f"Vocab status: {res.json()}")

    # 2. Проверяем список
    res = requests.get(API_VOCAB)
    print(f"Current vocab: {res.json()}")

    # 3. Транскрибация с доменом
    print("\nSending transcribe request with domain='code'...")
    with open("test_audio.wav", "rb") as f:
        data = {
            "chat_id": "context_test",
            "message_id": f"ctx_{time.time()}",
            "domain": "code",
            "vocabulary": "Python, MLX"
        }
        res = requests.post(API_STT, files={"file": f}, data=data)
    
    print(f"Status: {res.status_code}")
    if res.status_code == 200:
        print(f"Result: {res.json().get('text')}")
    else:
        print(f"Error: {res.text}")

if __name__ == "__main__":
    test_context()
