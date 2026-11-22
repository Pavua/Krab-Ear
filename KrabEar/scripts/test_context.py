#!/usr/bin/env python3
"""Ручная диагностика контекста STT (словарь + domain) против запущенного REST.

Адрес — KRAB_EAR_REST_URL (по умолчанию http://127.0.0.1:5005), ключ — KRAB_EAR_REST_API_KEY.
Скрипт печатает только статусы и размеры: тексты транскриптов и содержимое словаря в вывод
не попадают. Любая HTTP-ошибка — ненулевой код выхода.
"""
import os
import sys
import time

import requests

URL_BASE = os.environ.get("KRAB_EAR_REST_URL", "http://127.0.0.1:5005").rstrip("/")
API_STT = f"{URL_BASE}/v1/stt/transcribe"
API_VOCAB = f"{URL_BASE}/v1/vocabulary"


def _headers():
    key = os.environ.get("KRAB_EAR_REST_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def test_context() -> int:
    failures = 0
    print("Updating vocabulary...")
    res = requests.post(API_VOCAB, json={"words": ["Antigravity", "Pablito"]}, headers=_headers(), timeout=30)
    print(f"Vocab update status: {res.status_code}")
    failures += res.status_code != 200

    res = requests.get(API_VOCAB, headers=_headers(), timeout=30)
    print(f"Vocab list status: {res.status_code}")
    failures += res.status_code != 200

    print("\nSending transcribe request with domain='code'...")
    with open("test_audio.wav", "rb") as f:
        data = {
            "chat_id": "context_test",
            "message_id": f"ctx_{time.time()}",
            "domain": "code",
            "vocabulary": "Python, MLX",
        }
        res = requests.post(API_STT, files={"file": f}, data=data, headers=_headers(), timeout=120)

    print(f"Transcribe status: {res.status_code}")
    if res.status_code == 200:
        print(f"Result length: {len(res.json().get('text') or '')} chars")
    else:
        failures += 1
    if failures:
        print(f"FAILED: {failures} check(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(test_context())
