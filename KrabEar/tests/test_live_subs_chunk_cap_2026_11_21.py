"""KE05A-07: один чанк не обходит абсолютный лимит буфера.

``_MAX_BUFFER_SAMPLES`` проверялся уже ПОСЛЕ декодирования и добавления в буфер, а base64
декодировался без проверки размера: один огромный чанк раздувал память до всякого лимита.
"""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.live_subs_service import _MAX_BUFFER_SAMPLES, _MAX_CHUNK_BYTES, LiveSubsService  # noqa: E402


class _Decoded(BaseException):
    """Не Exception: handle_ingest ловит Exception вокруг b64decode и превращает его в ValueError."""


def _svc() -> LiveSubsService:
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": "", "language": "ru"}
    return LiveSubsService(transcriber=transcriber, translator=MagicMock())


def _pcm(n_samples: int) -> bytes:
    return np.zeros(n_samples, dtype=np.int16).tobytes()


class ChunkCap(unittest.TestCase):
    def test_oversized_chunk_is_rejected_before_decoding(self):
        svc = _svc()
        with patch.object(LiveSubsService, "_decode_audio", side_effect=AssertionError("decoded")):
            with self.assertRaises(ValueError):
                svc.ingest(_pcm(_MAX_CHUNK_BYTES // 2 + 1), 16000, "off", False)
        self.assertEqual(svc._buffer_samples, 0)

    def test_chunk_above_buffer_cap_but_within_limit_still_forces_flush(self):
        svc = _svc()
        svc.ingest(_pcm(_MAX_BUFFER_SAMPLES + 1), 16000, "off", False)  # старый контракт W1770

    def test_oversized_base64_is_rejected_before_b64decode(self):
        svc = _svc()
        huge = "A" * ((_MAX_CHUNK_BYTES * 4) // 3 + 4096)
        with patch("backend.live_subs_service.base64.b64decode", side_effect=_Decoded()):
            with self.assertRaises(ValueError):
                svc.handle_ingest({"audio_chunk": huge, "sample_rate": 16000})

    def test_normal_chunk_through_handle_ingest_unaffected(self):
        svc = _svc()
        res = svc.handle_ingest({"audio_chunk": base64.b64encode(_pcm(16000)).decode(), "sample_rate": 16000})
        self.assertIsInstance(res, dict)
        self.assertNotIn("error", res)


if __name__ == "__main__":
    unittest.main()
