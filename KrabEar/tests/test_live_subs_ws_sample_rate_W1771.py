"""Regression (W1771): WS /v1/stream sample_rate OOM + _flush privacy fail-safe.

The WS /v1/stream handler calls LiveSubsService.ingest() DIRECTLY, bypassing the
IPC handle_ingest() sanitizer. A tiny sample_rate (e.g. 1) made _flush() run
resample_poly(audio, 16000, 1) — a ~16000x upsample that tries to allocate tens
of GB (OOM / swap-thrash). Fix: clamp sample_rate at the ingest() choke point
(and defensively in _flush()). Plus: _flush() now privacy-fail-safes so a
privacy toggle mid-stream never emits a pre-toggle transcript over EventBus/SSE.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.live_subs_service import LiveSubsService, _MIN_SAMPLE_RATE, _MAX_SAMPLE_RATE


class _FakeTranscriber:
    def __init__(self, text: str = ""):
        self.last_audio_len = None
        self._text = text

    def transcribe(self, audio, **kw):
        self.last_audio_len = len(audio)
        return {"text": self._text, "language": "ru"}


class WSSampleRateOOMTest(unittest.TestCase):
    def setUp(self):
        self.tx = _FakeTranscriber()
        self.svc = LiveSubsService(
            transcriber=self.tx, translator=MagicMock(), settings_get=lambda k, d: d
        )

    def test_tiny_sample_rate_is_clamped_no_oom(self):
        # 1000 samples @ sample_rate=1 WOULD resample 16000x -> 16M samples (OOM).
        # ingest() now clamps sr<8000 up to _MIN_SAMPLE_RATE -> upsample <= 2x.
        pcm = np.zeros(1000, dtype=np.int16).tobytes()
        self.svc.ingest(audio_bytes=pcm, sample_rate=1, target_lang="off", is_final=True)
        self.assertIsNotNone(self.tx.last_audio_len, "transcriber should have run")
        self.assertLessEqual(
            self.tx.last_audio_len, 1000 * 4,
            "resample output must be bounded — sample_rate must be clamped before _flush",
        )

    def test_sanitize_sample_rate_bounds(self):
        for sr in (-5, 0, 1, 7999):
            self.assertEqual(LiveSubsService._sanitize_sample_rate(sr), _MIN_SAMPLE_RATE)
        for sr in (10 ** 9, 999999):
            self.assertEqual(LiveSubsService._sanitize_sample_rate(sr), _MAX_SAMPLE_RATE)
        self.assertEqual(LiveSubsService._sanitize_sample_rate("garbage"), 16000)
        self.assertEqual(LiveSubsService._sanitize_sample_rate(48000), 48000)


class FlushPrivacyGateTest(unittest.TestCase):
    def _svc(self, privacy, text: str = ""):
        # single_pass (2026-08-12): пустой результат первого движка больше не
        # эмитит событие (см. test_live_subs_single_pass_2026_08_12.py) — тесту
        # privacy-гейта нужен НЕПУСТОЙ текст, иначе emit не произойдёт по ДРУГОЙ
        # причине и тест перестанет проверять именно privacy.
        tx = _FakeTranscriber(text=text)
        svc = LiveSubsService(
            transcriber=tx, translator=MagicMock(),
            settings_get=lambda k, d: (privacy if k == "privacy_mode_enabled" else d),
        )
        return svc, tx

    def test_flush_emits_nothing_when_privacy_on(self):
        svc, tx = self._svc(privacy=True, text="привет")
        pcm = np.zeros(16000 * 4, dtype=np.int16).tobytes()  # >3s -> would flush
        with patch("backend.live_subs_service.event_bus.emit_typed") as mock_emit:
            res = svc.ingest(audio_bytes=pcm, sample_rate=16000, target_lang="off", is_final=True)
        mock_emit.assert_not_called()
        self.assertEqual(res.get("text"), "")
        self.assertIsNone(tx.last_audio_len, "transcriber must not run under privacy mode")

    def test_flush_emits_when_privacy_off(self):
        svc, _tx = self._svc(privacy=False, text="привет")
        pcm = np.zeros(16000 * 4, dtype=np.int16).tobytes()
        with patch("backend.live_subs_service.event_bus.emit_typed") as mock_emit:
            svc.ingest(audio_bytes=pcm, sample_rate=16000, target_lang="off", is_final=True)
        mock_emit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
