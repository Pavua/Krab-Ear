"""R7 (2026-11-07): после таймаута REST STT слот singleflight держит брошенный поток.

Живой замер R2: после 504 singleflight отпускался в ``finally``, а брошенная
транскрибация ещё ~90с держала MLX. Следующий не-call запрос захватывал слот и
молча висел до 92с. Теперь слот передаётся работающему Future и освобождается
по его завершении; пока сирота жив, новые запросы сразу получают 503 stt_busy.
"""
from __future__ import annotations

import io
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _wav_bytes() -> bytes:
    wav = io.BytesIO()
    wav.write(b"RIFF")
    wav.write(struct.pack("<I", 36))
    wav.write(b"WAVE")
    wav.write(b"fmt ")
    wav.write(struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16))
    wav.write(b"data")
    wav.write(struct.pack("<I", 0))
    return wav.getvalue()


class SingleflightHandoffAfterTimeoutTest(unittest.TestCase):
    def setUp(self):
        try:
            import backend.rest_server as rs
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"rest_server unavailable: {exc}")
        self.rs = rs
        self.gate = threading.Event()
        self.finished = threading.Event()

        def _slow_transcribe(*_a, **_k):
            self.gate.wait(10)
            self.finished.set()
            return {"text": "", "confidence": 0.0}

        transcriber = MagicMock()
        transcriber.transcribe.side_effect = _slow_transcribe
        store = MagicMock()
        store.load_vocabulary.return_value = []
        store.is_idempotent.return_value = False
        store.load_settings.return_value = {}
        engine = MagicMock()
        info = MagicMock()
        info.duration = 1.0
        rs.app.config["TESTING"] = True
        self._orig_limiter = rs.limiter.enabled
        rs.limiter.enabled = False
        self.exit_mock = MagicMock()
        self._patches = [
            patch.object(rs, "transcriber", transcriber),
            patch.object(rs, "store", store),
            patch.object(rs, "engine", engine),
            patch.object(rs, "_DEADLINE_SEC_MIN", 0.2),
            # 🔴 иначе закрытие 504-ответа сделает os._exit в процессе теста
            patch.object(rs, "_exit_poisoned_rest_process", self.exit_mock),
            patch("soundfile.info", return_value=info),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        self.gate.set()
        self.finished.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # дождаться освобождения слота сиротой
            if self.rs.try_acquire_stt_singleflight(0.05):
                self.rs.release_stt_singleflight()
                break
        for p in reversed(self._patches):
            p.stop()
        self.rs.limiter.enabled = self._orig_limiter

    def _post(self, **form):
        client = self.rs.app.test_client()
        data = {"file": (io.BytesIO(_wav_bytes()), "voice.wav")}
        data.update(form)
        return client.post("/v1/stt/transcribe", data=data, content_type="multipart/form-data")

    def test_orphan_keeps_slot_and_next_request_gets_fast_503(self):
        resp = self._post(deadline_sec="0.3")
        self.assertEqual(resp.status_code, 504)
        self.assertFalse(self.finished.is_set(), "сирота должен ещё работать")

        t0 = time.monotonic()
        resp2 = self._post(deadline_sec="5")
        elapsed = time.monotonic() - t0
        self.assertEqual(resp2.status_code, 503)
        self.assertEqual(resp2.get_json().get("error"), "stt_busy")
        self.assertLess(elapsed, 1.0, "запрос не должен молча ждать сироту")

    def test_slot_released_when_orphan_finishes(self):
        self.assertEqual(self._post(deadline_sec="0.3").status_code, 504)
        self.gate.set()
        self.assertTrue(self.finished.wait(5))
        deadline = time.monotonic() + 3
        acquired = False
        while time.monotonic() < deadline and not acquired:
            acquired = self.rs.try_acquire_stt_singleflight(0.05)
        self.assertTrue(acquired, "слот обязан освободиться после завершения сироты")
        self.rs.release_stt_singleflight()


if __name__ == "__main__":
    unittest.main()
