"""Тесты безопасности Wave 1766: REST/telephony surface hardening.

Охватывает 4 уязвимости (#8 снят, G3 audio-06 A06-11):
  #1 (HIGH) — CRLF header-injection в api_versioning.py
  #3 (MED)  — утечка temp-файла при ошибке file.save в rest_server.py
  #5 (MED)  — TOCTOU 0644 window в rest_auth.py _save()
  #6 (MED)  — timing oracle в rest_auth.py verify_token()
  #8 — телефонный PII в логах telnyx_adapter.py: адаптер удалён целиком
       (#1990, звонки через Voice Gateway), тестировать здесь нечего.
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
    """rest_server: partial temp-файл удаляется даже если file.save() падает.

    G3-audio-06 (A06-03): прежний тест сам создавал файл и сам же удалял его в собственном
    try/finally, ни разу не вызывая rest_server — он остался бы зелёным и при сломанной очистке
    настоящего HTTP-upload. Теперь запрос идёт через реальное Flask-приложение.
    """

    def _client(self, tmp_dir):
        from unittest.mock import patch

        import backend.rest_server as rs
        from test_rest_vg_contract_M1 import _deps_with

        deps = _deps_with()
        deps.store.call_privacy_mode.return_value = False
        deps.store.is_idempotent.return_value = False
        patches = [
            patch.object(rs, "TEMP_DIR", tmp_dir),
            patch.object(rs.settings, "REST_API_AUTH_ENABLED", False),
            patch.object(rs.settings, "REST_API_KEY", ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        app = rs.create_app(deps)
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        return app.test_client()

    def test_real_rest_upload_removes_partial_file_when_save_fails(self):
        import io
        from pathlib import Path
        from unittest.mock import patch

        from werkzeug.datastructures import FileStorage

        saved: list[object] = []

        def failing_save(storage, dst, *args, **kwargs):
            # Загрузка идёт через backend.private_upload.save_private: dst — открытый файловый
            # объект (0600, O_EXCL), а не путь. Пишем частичные данные и падаем как ENOSPC.
            saved.append(dst)
            dst.write(b"partial")
            dst.flush()
            raise OSError(28, "No space left on device")

        with tempfile.TemporaryDirectory() as td:
            client = self._client(Path(td))
            with patch.object(FileStorage, "save", failing_save):
                response = client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(b"RIFF" + b"\x00" * 64), "audio.wav")},
                )
            self.assertNotEqual(response.status_code, 200)
            self.assertTrue(saved, "реальный путь upload не дошёл до file.save()")
            self.assertEqual(list(Path(td).iterdir()), [])


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
