"""Tests для backend/va_multimodal.py — Phase 2A vision injection skeleton.

ВСЕ ТЕСТЫ SKIPPED: Phase 2A не интегрирован в основной pipeline.
Wave 56+ снимет @unittest.skip когда:
  1. MultimodalVAClient подключён к IPC dispatch (va_send_with_image метод)
  2. ConversationViewController+Vision.swift реализован (FSEvents watcher)
  3. LM Studio dual-model routing стабилизирован (OQ-1 product decision)

Не запускать real LLM calls в CI — тесты используют mocks/stubs.
"""

from __future__ import annotations

import sys
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Path setup — стандартный паттерн для test files в Krab Ear
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


_PHASE_2A_WIP = "Phase 2A WIP — не интегрирован с основным VA pipeline"


@unittest.skip(_PHASE_2A_WIP)
class TestVAMultimodalResultContract(unittest.TestCase):
    """VAMultimodalResult dataclass — контракт поведения."""

    def test_text_or_fallback_returns_text_when_ok(self):
        from backend.va_multimodal import VAMultimodalResult
        result = VAMultimodalResult(
            ok=True, text="Ответ ассистента", model_used="supergemma-mm", latency_ms=4800
        )
        self.assertEqual(result.text_or_fallback("fallback"), "Ответ ассистента")

    def test_text_or_fallback_returns_fallback_when_not_ok(self):
        from backend.va_multimodal import VAMultimodalResult
        result = VAMultimodalResult(
            ok=False, text=None, model_used=None, latency_ms=None,
            fallback_reason="lm_studio_not_running"
        )
        self.assertEqual(result.text_or_fallback("оригинальный текст"), "оригинальный текст")

    def test_text_or_fallback_returns_fallback_when_text_empty(self):
        from backend.va_multimodal import VAMultimodalResult
        result = VAMultimodalResult(
            ok=True, text="", model_used="supergemma-mm", latency_ms=100
        )
        self.assertEqual(result.text_or_fallback("fallback text"), "fallback text")


@unittest.skip(_PHASE_2A_WIP)
class TestImageEncoding(unittest.TestCase):
    """MultimodalVAClient._encode_image — кодирование изображения в base64."""

    def test_encode_png_returns_base64_and_mime(self):
        """Корректный PNG файл → base64 string + 'image/png'."""
        import tempfile, base64
        from backend.va_multimodal import MultimodalVAClient

        # minimal valid PNG (1×1 px transparent)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png_bytes)
            tmp_path = Path(f.name)

        try:
            b64, mime = MultimodalVAClient._encode_image(tmp_path)
            self.assertEqual(mime, "image/png")
            decoded = base64.b64decode(b64)
            self.assertEqual(decoded, png_bytes)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_encode_jpeg_returns_jpeg_mime(self):
        from backend.va_multimodal import MultimodalVAClient
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 20)  # minimal JPEG header
            tmp_path = Path(f.name)

        try:
            _, mime = MultimodalVAClient._encode_image(tmp_path)
            self.assertEqual(mime, "image/jpeg")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_encode_raises_value_error_when_too_large(self):
        """Файл > 8MB → ValueError (защита от OOM в LM Studio context window)."""
        from backend.va_multimodal import MultimodalVAClient, _MAX_IMAGE_BYTES
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x00" * (_MAX_IMAGE_BYTES + 1))
            tmp_path = Path(f.name)

        try:
            with self.assertRaises(ValueError, msg="expected ValueError for oversized image"):
                MultimodalVAClient._encode_image(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_encode_raises_os_error_for_missing_file(self):
        from backend.va_multimodal import MultimodalVAClient

        with self.assertRaises(OSError):
            MultimodalVAClient._encode_image(Path("/tmp/krab_ear_nonexistent_image_XY.png"))


@unittest.skip(_PHASE_2A_WIP)
class TestBuildMessages(unittest.TestCase):
    """MultimodalVAClient._build_messages — сборка messages[] для OpenAI Vision API."""

    def test_vision_turn_has_image_url_content(self):
        from backend.va_multimodal import MultimodalVAClient

        messages = MultimodalVAClient._build_messages(
            text="Что на скриншоте?",
            image_b64="AAAA==",
            mime_type="image/png",
            conversation_history=[],
            system_prompt=None,
        )
        # last message — user vision turn
        last = messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertIsInstance(last["content"], list)
        types = [c["type"] for c in last["content"]]
        self.assertIn("image_url", types)
        self.assertIn("text", types)

    def test_image_url_uses_data_uri_format(self):
        from backend.va_multimodal import MultimodalVAClient

        messages = MultimodalVAClient._build_messages(
            text="Опиши",
            image_b64="base64data",
            mime_type="image/png",
            conversation_history=[],
            system_prompt=None,
        )
        image_content = next(c for c in messages[-1]["content"] if c["type"] == "image_url")
        url = image_content["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"), f"unexpected url: {url}")
        self.assertIn("base64data", url)

    def test_conversation_history_prepended_before_vision_turn(self):
        from backend.va_multimodal import MultimodalVAClient

        history = [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Привет!"},
        ]
        messages = MultimodalVAClient._build_messages(
            text="Что на скриншоте?",
            image_b64="AAAA==",
            mime_type="image/png",
            conversation_history=history,
            system_prompt=None,
        )
        # [system, user(Привет), assistant(Привет!), user(vision)]
        self.assertGreaterEqual(len(messages), 4)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[-1], "user")

    def test_custom_system_prompt_used_when_provided(self):
        from backend.va_multimodal import MultimodalVAClient

        messages = MultimodalVAClient._build_messages(
            text="Q",
            image_b64="AA==",
            mime_type="image/png",
            conversation_history=[],
            system_prompt="Custom system prompt",
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "Custom system prompt")


@unittest.skip(_PHASE_2A_WIP)
class TestSendWithImageHTTPLayer(unittest.TestCase):
    """MultimodalVAClient.send_with_image — HTTP mock tests."""

    def _make_client(self):
        from backend.va_multimodal import MultimodalVAClient
        return MultimodalVAClient(
            base_url="http://localhost:1234",
            api_key="test-key",
            vision_model="supergemma-test",
            timeout_sec=5.0,
        )

    def _make_tiny_png(self) -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    @patch("requests.Session.post")
    def test_successful_vision_turn_returns_ok_result(self, mock_post):
        import tempfile
        client = self._make_client()

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "На скриншоте код Python."}}]
        }
        mock_post.return_value = mock_response

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(self._make_tiny_png())
            tmp = Path(f.name)

        try:
            result = client.send_with_image(text="Что на скриншоте?", image_path=tmp)
        finally:
            tmp.unlink(missing_ok=True)

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "На скриншоте код Python.")
        self.assertEqual(result.model_used, "supergemma-test")
        self.assertIsNotNone(result.latency_ms)

    @patch("requests.Session.post")
    def test_connection_error_returns_not_ok(self, mock_post):
        import tempfile
        import requests as req_lib
        client = self._make_client()

        mock_post.side_effect = req_lib.exceptions.ConnectionError("LM Studio not running")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(self._make_tiny_png())
            tmp = Path(f.name)

        try:
            result = client.send_with_image(text="Q", image_path=tmp)
        finally:
            tmp.unlink(missing_ok=True)

        self.assertFalse(result.ok)
        self.assertIsNone(result.text)
        self.assertEqual(result.fallback_reason, "lm_studio_not_running")

    def test_missing_image_returns_not_ok_without_raising(self):
        """Несуществующий файл → VAMultimodalResult(ok=False), без исключения."""
        client = self._make_client()
        result = client.send_with_image(
            text="Q", image_path=Path("/tmp/krab_ear_nonexistent_XYZABC.png")
        )
        self.assertFalse(result.ok)
        self.assertIn("image_encode_error", result.fallback_reason or "")

    @patch("requests.Session.post")
    def test_timeout_returns_not_ok(self, mock_post):
        import tempfile
        import requests as req_lib
        client = self._make_client()

        mock_post.side_effect = req_lib.exceptions.Timeout()

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(self._make_tiny_png())
            tmp = Path(f.name)

        try:
            result = client.send_with_image(text="Q", image_path=tmp)
        finally:
            tmp.unlink(missing_ok=True)

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.fallback_reason)
        self.assertIn("timeout", result.fallback_reason)

    @patch("requests.Session.post")
    def test_payload_includes_vision_model_name(self, mock_post):
        """Убедиться, что в POST payload идёт vision model, не baseline model."""
        import tempfile
        client = self._make_client()

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "OK"}}]
        }
        mock_post.return_value = mock_response

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(self._make_tiny_png())
            tmp = Path(f.name)

        try:
            client.send_with_image(text="Q", image_path=tmp)
        finally:
            tmp.unlink(missing_ok=True)

        call_kwargs = mock_post.call_args
        payload_sent = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        self.assertEqual(payload_sent.get("model"), "supergemma-test")


if __name__ == "__main__":
    unittest.main()
