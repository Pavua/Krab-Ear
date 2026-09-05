"""C1 sibling: cloud только если LM Studio НЕДОСТУПЕН, не если каталог пуст.

Политика владельца 2026-09-05:
- Сначала всегда Studio (каталог / chat), без `lms load` на пустой слот.
- Пустой каталог ≠ недоступен → extractive / сырой текст, без облака.
- Connection/timeout → cloud, если `cloud_rewriter_enabled` и не privacy.
- Ключ тот же: `cloud_rewriter_enabled` (не новый флаг). Дефолт остаётся False.
- `memory_conductor_enforce*` не включать. `llm_rewrite_enabled` не включать.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriteResult, LLMRewriter, is_studio_unavailable  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402

BASE_URL = "http://localhost:1234/v1"
LONG_TEXT = "long enough transcript for a summary " * 8


def _settings(**over):
    base = {
        "llm_rewrite_enabled": True,
        "privacy_mode_enabled": False,
        "cloud_rewriter_enabled": False,
        "cloud_rewriter_provider": "openai",
        "stt_punctuation_llm_pass_enabled": False,
    }
    base.update(over)
    return lambda k, d=None: base.get(k, d)


class StudioUnavailableClassifierTest(unittest.TestCase):
    def test_timeout_is_unavailable(self) -> None:
        self.assertTrue(is_studio_unavailable("timeout"))

    def test_connection_error_is_unavailable(self) -> None:
        self.assertTrue(is_studio_unavailable("connection_error"))

    def test_empty_catalog_is_not_unavailable(self) -> None:
        self.assertFalse(is_studio_unavailable("studio_empty_no_autoload"))

    def test_http_400_empty_slot_is_not_unavailable(self) -> None:
        self.assertFalse(is_studio_unavailable("http_400"))

    def test_circuit_open_after_timeout_is_unavailable(self) -> None:
        self.assertTrue(is_studio_unavailable("circuit_open", last_error="timeout"))

    def test_circuit_open_after_http_400_is_not_unavailable(self) -> None:
        self.assertFalse(is_studio_unavailable("circuit_open", last_error="http_400"))

    def test_privacy_and_none_are_not_unavailable(self) -> None:
        self.assertFalse(is_studio_unavailable("privacy_mode"))
        self.assertFalse(is_studio_unavailable(None))


class SchemaStaysHoldoffTest(unittest.TestCase):
    def test_cloud_rewriter_default_stays_off(self) -> None:
        self.assertIs(DEFAULT_SETTINGS["cloud_rewriter_enabled"], False)

    def test_rewrite_default_stays_off(self) -> None:
        self.assertIs(DEFAULT_SETTINGS["llm_rewrite_enabled"], False)

    def test_enforce_flags_stay_off(self) -> None:
        for key in (
            "memory_conductor_enforce",
            "memory_conductor_enforce_brain",
            "memory_conductor_enforce_rewriter",
            "memory_conductor_enforce_gigaam",
            "memory_conductor_enforce_whisper",
            "memory_conductor_enforce_recording_sequence",
        ):
            self.assertIs(DEFAULT_SETTINGS[key], False, key)


class EngineCloudOnlyWhenStudioDownTest(unittest.TestCase):
    def _fake_whisper(self, text: str = "привет мир"):
        return {
            "text": text,
            "segments": [{"avg_logprob": -0.2}],
            "engine": "fake-whisper",
            "model_used": "fake",
            "language": "ru",
        }

    def _engine(self, rewriter, settings_get):
        from core.engine import AudioEngine

        engine = AudioEngine()
        engine._llm_rewriter = rewriter
        engine._settings_get = settings_get
        return engine

    @patch("core.engine.AudioEngine._maybe_run_diarization", return_value=None)
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_timeout_uses_cloud_when_enabled(self, mock_fallback, _mock_diar) -> None:
        mock_fallback.return_value = self._fake_whisper()
        rewriter = MagicMock()
        rewriter.rewrite.return_value = LLMRewriteResult(
            ok=False, text=None, fallback_reason="timeout", latency_ms=None
        )
        rewriter._last_error = "timeout"
        engine = self._engine(
            rewriter,
            _settings(cloud_rewriter_enabled=True),
        )
        with patch("backend.cloud_rewriter.cloud_rewrite", return_value="Привет, мир.") as cloud:
            result = engine.transcribe(audio_data="fake.wav")
        cloud.assert_called_once()
        self.assertEqual(result["text"], "Привет, мир.")

    @patch("core.engine.AudioEngine._maybe_run_diarization", return_value=None)
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_empty_catalog_does_not_use_cloud(self, mock_fallback, _mock_diar) -> None:
        mock_fallback.return_value = self._fake_whisper()
        rewriter = MagicMock()
        rewriter.rewrite.return_value = LLMRewriteResult(
            ok=False, text=None, fallback_reason="studio_empty_no_autoload", latency_ms=None
        )
        rewriter._last_error = None
        engine = self._engine(
            rewriter,
            _settings(cloud_rewriter_enabled=True),
        )
        with patch("backend.cloud_rewriter.cloud_rewrite", return_value="CLOUD") as cloud:
            result = engine.transcribe(audio_data="fake.wav")
        cloud.assert_not_called()
        self.assertEqual(result["text"], result["cleaned_text"])

    @patch("core.engine.AudioEngine._maybe_run_diarization", return_value=None)
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_http_400_empty_slot_does_not_use_cloud(self, mock_fallback, _mock_diar) -> None:
        mock_fallback.return_value = self._fake_whisper()
        rewriter = MagicMock()
        rewriter.rewrite.return_value = LLMRewriteResult(
            ok=False, text=None, fallback_reason="http_400", latency_ms=None
        )
        rewriter._last_error = "http_400"
        engine = self._engine(
            rewriter,
            _settings(cloud_rewriter_enabled=True),
        )
        with patch("backend.cloud_rewriter.cloud_rewrite", return_value="CLOUD") as cloud:
            result = engine.transcribe(audio_data="fake.wav")
        cloud.assert_not_called()
        self.assertEqual(result["text"], result["cleaned_text"])

    @patch("core.engine.AudioEngine._maybe_run_diarization", return_value=None)
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_timeout_skips_cloud_in_privacy(self, mock_fallback, _mock_diar) -> None:
        mock_fallback.return_value = self._fake_whisper()
        rewriter = MagicMock()
        rewriter.rewrite.return_value = LLMRewriteResult(
            ok=False, text=None, fallback_reason="timeout", latency_ms=None
        )
        engine = self._engine(
            rewriter,
            _settings(cloud_rewriter_enabled=True, privacy_mode_enabled=True),
        )
        with patch("backend.cloud_rewriter.cloud_rewrite", return_value="CLOUD") as cloud:
            engine.transcribe(audio_data="fake.wav")
        rewriter.rewrite.assert_not_called()
        cloud.assert_not_called()

    @patch("core.engine.AudioEngine._maybe_run_diarization", return_value=None)
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_timeout_skips_cloud_when_toggle_off(self, mock_fallback, _mock_diar) -> None:
        mock_fallback.return_value = self._fake_whisper()
        rewriter = MagicMock()
        rewriter.rewrite.return_value = LLMRewriteResult(
            ok=False, text=None, fallback_reason="timeout", latency_ms=None
        )
        rewriter._last_error = "timeout"
        engine = self._engine(
            rewriter,
            _settings(cloud_rewriter_enabled=False),
        )
        with patch("backend.cloud_rewriter.cloud_rewrite", return_value="CLOUD") as cloud:
            result = engine.transcribe(audio_data="fake.wav")
        cloud.assert_not_called()
        self.assertEqual(result["text"], result["cleaned_text"])


@pytest.mark.llm_network_live
class SummarizeCloudOnlyWhenStudioDownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rewriter = LLMRewriter(
            base_url=BASE_URL,
            api_key="",
            model="gigachat3.1-10b-a1.8b-mlx-oq8",
            timeout_sec=5.0,
            circuit_fail_threshold=3,
            idle_keepalive_enabled=False,
        )

    def test_empty_catalog_does_not_call_cloud(self) -> None:
        self.rewriter._settings_getter = _settings(cloud_rewriter_enabled=True)
        self.rewriter._session.post = MagicMock()
        with patch(
            "backend.lm_studio_lifecycle.probe_loaded_chat_models",
            return_value=[],
        ), patch("backend.cloud_rewriter.cloud_summarize", return_value="CLOUD") as cloud:
            result = self.rewriter.summarize(LONG_TEXT)
        cloud.assert_not_called()
        self.rewriter._session.post.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "studio_empty_no_autoload")

    def test_connection_error_uses_cloud_when_enabled(self) -> None:
        self.rewriter._settings_getter = _settings(cloud_rewriter_enabled=True)
        self.rewriter._session.post = MagicMock(
            side_effect=requests.ConnectionError("Studio down")
        )
        with patch(
            "backend.lm_studio_lifecycle.probe_loaded_chat_models",
            return_value=None,
        ), patch(
            "backend.cloud_rewriter.cloud_summarize",
            return_value="Краткое облачное резюме.",
        ) as cloud:
            result = self.rewriter.summarize(LONG_TEXT)
        cloud.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Краткое облачное резюме.")

    def test_timeout_skips_cloud_in_privacy(self) -> None:
        self.rewriter._settings_getter = _settings(
            cloud_rewriter_enabled=True,
            privacy_mode_enabled=True,
        )
        with patch("backend.cloud_rewriter.cloud_summarize", return_value="CLOUD") as cloud:
            result = self.rewriter.summarize(LONG_TEXT)
        cloud.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "privacy_mode")

    def test_timeout_skips_cloud_when_toggle_off(self) -> None:
        self.rewriter._settings_getter = _settings(cloud_rewriter_enabled=False)
        self.rewriter._session.post = MagicMock(side_effect=requests.Timeout("slow"))
        with patch(
            "backend.lm_studio_lifecycle.probe_loaded_chat_models",
            return_value=None,
        ), patch("backend.cloud_rewriter.cloud_summarize", return_value="CLOUD") as cloud:
            result = self.rewriter.summarize(LONG_TEXT)
        cloud.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "timeout")


class CloudSummarizeReusesProvidersTest(unittest.TestCase):
    def test_cloud_summarize_does_not_use_cleanup_length_floor(self) -> None:
        """Резюме короче входа — min-ratio 0.35 рерайта не должен резать summary."""
        import backend.cloud_rewriter as cr

        provider = MagicMock()
        provider.rewrite.return_value = {"text": "Короткое резюме."}
        with patch.object(cr, "_load_settings", return_value={
            "cloud_rewriter_provider": "openai",
        }), patch.object(cr, "get_cloud_rewriter", return_value=provider):
            out = cr.cloud_summarize("слово " * 80, max_sentences=3)
        self.assertEqual(out, "Короткое резюме.")
        kwargs = provider.rewrite.call_args
        self.assertIn("system_prompt", kwargs.kwargs)
        self.assertIn("summary", kwargs.kwargs["system_prompt"].lower())


if __name__ == "__main__":
    unittest.main()
