"""E5 п.1: `set_settings({"hf_token": null})` не должен ронять следующий старт backend.

Корень: `hf_token` / `stt_gigaam_hf_token` не входили в `_STRING_FIELDS` валидатора, поэтому
None сохранялся. При старте `_propagate_hf_token_to_env` делал
`self._get_runtime_setting("hf_token", "").strip()` — `dict.get(key, default)` при ключе со
значением None отдаёт None, `.strip()` падал AttributeError прямо в `BackendService.__init__`.
Backend не поднимался, пока файл настроек не правили руками.

Два слоя:
 1. запись: валидатор отвергает не-строку (как для `cloud_rewriter_api_key`);
 2. чтение: уже испорченный `settings.json` не должен класть старт (деградация к «токена нет»).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import BackendService  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402

_ENV_KEYS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN")
_TOKEN_FIELDS = ("hf_token", "stt_gigaam_hf_token")


class _StubService:
    """Только `_get_runtime_setting` + реальный `_propagate_hf_token_to_env` (как в W1755)."""

    def __init__(self, runtime: dict):
        self._runtime = runtime

    def _get_runtime_setting(self, key, default=None):
        # Ровно семантика прод-метода: ключ присутствует со значением None → вернётся None.
        return self._runtime.get(key, default)

    _propagate_hf_token_to_env = BackendService._propagate_hf_token_to_env


class HfTokenValidatorTests(unittest.TestCase):
    def test_non_string_token_is_rejected(self):
        for field in _TOKEN_FIELDS:
            for bad in (None, 123, ["x"], {"a": 1}, True):
                with self.subTest(field=field, value=bad):
                    result = SettingsValidator().validate({field: bad})
                    self.assertFalse(result.valid, f"{field}={bad!r} должен быть отвергнут")
                    self.assertTrue(any(field in e for e in result.errors), result.errors)

    def test_string_token_is_accepted(self):
        for field in _TOKEN_FIELDS:
            for ok in ("", "hf_abc123"):
                with self.subTest(field=field, value=ok):
                    self.assertTrue(SettingsValidator().validate({field: ok}).valid)

    def test_error_text_does_not_echo_token_value(self):
        result = SettingsValidator().validate({"hf_token": ["hf_SECRETVALUE"]})
        self.assertFalse(result.valid)
        self.assertNotIn("hf_SECRETVALUE", " ".join(result.errors))


class HfTokenPropagationToleratesPoisonedSettingsTests(unittest.TestCase):
    def setUp(self):
        self._snapshot = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_none_tokens_do_not_raise_and_leave_env_untouched(self):
        svc = _StubService({"hf_token": None, "stt_gigaam_hf_token": None})
        svc._propagate_hf_token_to_env(overwrite=True)
        for k in _ENV_KEYS:
            self.assertNotIn(k, os.environ)

    def test_none_generic_falls_back_to_gigaam_token(self):
        svc = _StubService({"hf_token": None, "stt_gigaam_hf_token": "hf_gigaam"})
        svc._propagate_hf_token_to_env(overwrite=True)
        self.assertEqual(os.environ.get("HF_TOKEN"), "hf_gigaam")

    def test_non_string_tokens_do_not_raise(self):
        svc = _StubService({"hf_token": 123, "stt_gigaam_hf_token": ["x"]})
        svc._propagate_hf_token_to_env(overwrite=True)
        for k in _ENV_KEYS:
            self.assertNotIn(k, os.environ)


if __name__ == "__main__":
    unittest.main()
