"""E2E smoke-тест: Krab Ear call_assist + Voice Gateway.

Требует: оба сервиса запущены (VG :8090, Krab Ear :5005).
Пропускается автоматически если VG недоступен.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_e2e_voice_loop.py -v
"""
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    import requests
except ImportError:
    requests = None

VG_URL = os.getenv("VG_URL", "http://127.0.0.1:8090")
EAR_URL = os.getenv("EAR_URL", "http://127.0.0.1:5005")


def _services_available() -> bool:
    try:
        if requests:
            vg_ok = requests.get(f"{VG_URL}/health", timeout=2).ok
            ear_ok = requests.get(f"{EAR_URL}/health", timeout=2).ok
        else:
            import urllib.request
            vg_ok = urllib.request.urlopen(f"{VG_URL}/health", timeout=2).status == 200
            ear_ok = urllib.request.urlopen(f"{EAR_URL}/health", timeout=2).status == 200
        return vg_ok and ear_ok
    except Exception:
        return False


@unittest.skipUnless(_services_available(), "VG or Krab Ear not running")
class TestE2EVoiceLoop(unittest.TestCase):

    def test_vg_health(self):
        """Voice Gateway отвечает на health."""
        if requests:
            resp = requests.get(f"{VG_URL}/health", timeout=5)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "ok")
        else:
            import urllib.request
            import json
            resp = urllib.request.urlopen(f"{VG_URL}/health", timeout=5)
            data = json.loads(resp.read())
            self.assertEqual(data["status"], "ok")

    def test_ear_health(self):
        """Krab Ear отвечает на health."""
        if requests:
            resp = requests.get(f"{EAR_URL}/health", timeout=5)
            self.assertEqual(resp.status_code, 200)
        else:
            import urllib.request
            resp = urllib.request.urlopen(f"{EAR_URL}/health", timeout=5)
            self.assertEqual(resp.status, 200)

    def test_stt_proxy_through_vg(self):
        """STT через VG proxy — Krab Ear обрабатывает аудио."""
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "test_phrase_ru.wav")
        if not os.path.exists(fixture):
            self.skipTest("test fixture not found")

        if requests:
            with open(fixture, "rb") as f:
                resp = requests.post(
                    f"{VG_URL}/v1/stt/proxy",
                    files={"file": ("test.wav", f, "audio/wav")},
                    data={"language": "ru"},
                    timeout=30,
                )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "ok")
            self.assertTrue(len(data["text"]) > 0)
            self.assertEqual(data["engine"], "krab_ear")
        else:
            self.skipTest("requests not available")

    def test_vg_session_lifecycle(self):
        """Создание и удаление VG-сессии."""
        if not requests:
            self.skipTest("requests not available")

        resp = requests.post(
            f"{VG_URL}/v1/sessions",
            json={"translation_mode": "ru_to_es", "source": "mic"},
            timeout=10,
        )
        self.assertEqual(resp.status_code, 200)
        session_id = resp.json()["session_id"]
        self.assertTrue(session_id.startswith("vs_"))

        resp = requests.get(f"{VG_URL}/v1/sessions/{session_id}", timeout=5)
        self.assertEqual(resp.status_code, 200)

        resp = requests.delete(f"{VG_URL}/v1/sessions/{session_id}", timeout=5)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
