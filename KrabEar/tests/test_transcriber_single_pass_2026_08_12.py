"""Transcriber.transcribe пробрасывает single_pass в AudioEngine.transcribe (2026-08-12).

Живой инцидент: окно live-субтитров длиной 2.5с прошло полную цепочку STT,
спроектированную для диктовки (GigaAM → confidence-retry на whisper-large-v3 →
whisper-large-v3-turbo) — 9.49с на окно, которое приходит каждые ~3с.
`single_pass=True` отключает confidence-driven multi-pass retry и
request-local fallback на Whisper (см. core/engine.py::AudioEngine.transcribe).
Transcriber — тонкая обёртка (см. class docstring), обязана пробрасывать флаг
без изменений.

Спека: docs/superpowers/specs/2026-08-12-live-subs-single-pass-design.md

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_transcriber_single_pass_2026_08_12.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcriber import Transcriber


class TranscriberSinglePassPassthroughTest(unittest.TestCase):
    """Transcriber.transcribe(single_pass=...) обязан пробрасываться в engine.transcribe."""

    def setUp(self):
        self.fake_engine = MagicMock()
        self.fake_engine._llm_rewriter = None
        self.fake_engine.transcribe.return_value = {"text": "ok"}
        self.transcriber = Transcriber(engine=self.fake_engine)

    def test_single_pass_true_reaches_engine(self):
        self.transcriber.transcribe(b"audio", single_pass=True)

        self.fake_engine.transcribe.assert_called_once()
        _, kwargs = self.fake_engine.transcribe.call_args
        self.assertTrue(kwargs.get("single_pass"))

    def test_single_pass_default_is_false(self):
        """По умолчанию (не передан явно) — False, путь диктовки не меняется."""
        self.transcriber.transcribe(b"audio")

        _, kwargs = self.fake_engine.transcribe.call_args
        self.assertFalse(kwargs.get("single_pass"))

    def test_single_pass_false_explicit(self):
        self.transcriber.transcribe(b"audio", single_pass=False)

        _, kwargs = self.fake_engine.transcribe.call_args
        self.assertFalse(kwargs.get("single_pass"))


if __name__ == "__main__":
    unittest.main()
