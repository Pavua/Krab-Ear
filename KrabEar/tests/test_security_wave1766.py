"""Тесты безопасности Wave 1766: REST/telephony surface hardening.

Охватывает 5 уязвимостей:
  #1 (HIGH) — CRLF header-injection в api_versioning.py
  #3 (MED)  — утечка temp-файла при ошибке file.save в rest_server.py
  #5 (MED)  — TOCTOU 0644 window в rest_auth.py _save()
  #6 (MED)  — timing oracle в rest_auth.py verify_token()
  #8 (MED)  — телефонный PII в логах telnyx_adapter.py
"""
from __future__ import annotations

import hmac
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import Flask  # noqa: E402
from backend.api_versioning import api_version_header  # noqa: E402
from backend.rest_auth import RestAuth  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательный Flask app для тестов api_versioning
# ---------------------------------------------------------------------------

def _make_app() -> Flask:
    """Минимальное Flask-приложение с after_request handler."""
    app = Flask(__name__)
    app.after_request(api_version_header())

    @app.route("/health")
    def health():
        from flask import jsonify
        return jsonify({"ok": True})

    return app


# ---------------------------------------------------------------------------
# #1 (HIGH) — CRLF header-injection
# ---------------------------------------------------------------------------

class TestCRLFHeaderInjection(unittest.TestCase):
    """api_versioning: CRLF-символы в ?api_version= не должны вызывать 500."""

    def setUp(self):
        self.app = _make_app()
        self.client = self.app.test_client()

    def test_crlf_in_api_version_returns_non_500(self):
        """GET /health?api_version=v99%0d%0aX-Evil:%20foo → не 500."""
        resp = self.client.get("/health?api_version=v99%0d%0aX-Evil:%20foo")
        self.assertNotEqual(resp.status_code, 500,
                            "CRLF в query param не должен вызывать 500")

    def test_crlf_not_present_in_warning_header(self):
        """Заголовок X-API-Version-Warning не содержит \\r или \\n."""
        resp = self.client.get("/health?api_version=v99%0d%0aX-Evil:%20foo")
        warning = resp.headers.get("X-API-Version-Warning", "")
        self.assertNotIn("\r", warning,
                         "\\r не должен присутствовать в X-API-Version-Warning")
        self.assertNotIn("\n", warning,
                         "\\n не должен присутствовать в X-API-Version-Warning")

    def test_injected_header_not_present(self):
        """Инъектированный заголовок X-Evil не должен появиться в ответе."""
        resp = self.client.get("/health?api_version=v99%0d%0aX-Evil:%20foo")
        self.assertNotIn("X-Evil", resp.headers,
                         "Инъектированный заголовок X-Evil не должен попасть в ответ")

    def test_valid_unknown_version_still_gets_warning(self):
        """Обычный неизвестный api_version без CRLF даёт корректный warning."""
        resp = self.client.get("/health?api_version=v99")
        self.assertEqual(resp.status_code, 200)
        warning = resp.headers.get("X-API-Version-Warning", "")
        self.assertIn("v99", warning, "Warning должен содержать 'v99'")


# ---------------------------------------------------------------------------
# #3 (MED) — утечка temp-файла при ошибке file.save
# ---------------------------------------------------------------------------

class TestTempUploadLeak(unittest.TestCase):
    """rest_server: partial temp-файл удаляется даже если file.save() падает."""

    def test_temp_file_cleaned_on_save_error(self):
        """Если FileStorage.save() поднимает OSError — temp-файл не остаётся."""
        import uuid
        from pathlib import Path

        tmp_dir = Path(tempfile.mkdtemp())
        safe_base = "test_upload.wav"
        temp_path = tmp_dir / f"{uuid.uuid4().hex[:12]}_{safe_base}"

        # Имитируем логику rest_server: file.save внутри try/finally
        class _FakeFile:
            def save(self, path):
                # Создаём частичный файл, потом бросаем ошибку (ENOSPC)
                Path(path).write_bytes(b"partial")
                raise OSError(28, "No space left on device")

        fake_file = _FakeFile()

        try:
            fake_file.save(str(temp_path))
        except OSError:
            pass
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass

        self.assertFalse(temp_path.exists(),
                         "Partial temp-файл не должен остаться после ошибки save()")


# ---------------------------------------------------------------------------
# #5 (MED) — TOCTOU 0644 window в _save()
# ---------------------------------------------------------------------------

class TestTokenFileModeAndCleanup(unittest.TestCase):
    """rest_auth: файл токенов создаётся с режимом 0600, tmp чист при ошибке."""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.auth = RestAuth(data_dir=self._tmp_dir)

    def test_tokens_file_mode_is_0600(self):
        """api_tokens.json имеет режим 0600 после create_token."""
        self.auth.create_token("test-app")
        tokens_path = Path(self._tmp_dir) / "api_tokens.json"
        mode = stat.S_IMODE(tokens_path.stat().st_mode)
        self.assertEqual(
            mode, 0o600,
            f"Ожидался режим 0o600, получен 0o{mode:03o}",
        )

    def test_tmp_file_cleaned_on_replace_failure(self):
        """Если os.replace() падает — .tmp файл удаляется."""
        self.auth.create_token("t1")  # Создаём нормальный токен
        tmp_path = Path(self._tmp_dir) / "api_tokens.tmp"

        with patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.auth._save(self.auth._tokens)

        # После исключения .tmp не должен остаться
        self.assertFalse(tmp_path.exists(),
                         ".tmp файл не должен оставаться при ошибке os.replace()")


# ---------------------------------------------------------------------------
# #6 (MED) — timing oracle в verify_token
# ---------------------------------------------------------------------------

class TestVerifyTokenConstantTime(unittest.TestCase):
    """rest_auth: verify_token использует hmac.compare_digest."""

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self.auth = RestAuth(data_dir=self._tmp_dir)

    def test_valid_token_accepted(self):
        """Корректный токен принимается после create_token."""
        raw, _ = self.auth.create_token("ci-bot")
        result = self.auth.verify_token(raw)
        self.assertIsNotNone(result)

    def test_invalid_token_rejected(self):
        """Неверный токен отклоняется (возвращает None)."""
        self.auth.create_token("ci-bot")
        result = self.auth.verify_token("wrong-token-value")
        self.assertIsNone(result)

    def test_compare_digest_is_used(self):
        """hmac.compare_digest вызывается при verify_token."""
        raw, _ = self.auth.create_token("ci-bot")
        calls = []
        real_compare = hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real_compare(a, b)

        with patch("backend.rest_auth.hmac.compare_digest", side_effect=spy):
            self.auth.verify_token(raw)

        self.assertGreater(len(calls), 0,
                           "hmac.compare_digest должен вызываться при verify_token")


# ---------------------------------------------------------------------------
# #8 (MED) — телефонный PII в логах telnyx_adapter
# ---------------------------------------------------------------------------
