"""W1755 regression: runtime hf_token propagated to os.environ for pyannote diarization.

Bug: user sets hf_token via GUI (settings.json). pyannote / transcriber read only
os.environ["HF_TOKEN"], never settings.json.  Without propagation the env key stays
empty → diarization.no_token error → speaker labels silently dropped.

Fix: BackendService._propagate_hf_token_to_env() runs at __init__ and via
after_save hook so a GUI token change takes effect without restart.

Tests here exercise _propagate_hf_token_to_env in isolation (lightweight stub
_get_runtime_setting) to avoid heavy BackendService construction.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENV_KEYS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN")


def _save_env():
    """Сохраняет текущие значения HF env ключей."""
    return {k: os.environ.get(k) for k in _ENV_KEYS}


def _restore_env(snapshot: dict):
    """Восстанавливает HF env ключи из snapshot."""
    for k, v in snapshot.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class _StubService:
    """Минимальный stub BackendService с только _get_runtime_setting и
    _propagate_hf_token_to_env — позволяет тестировать метод без тяжёлой инициализации."""

    def __init__(self, runtime_settings: dict):
        self._runtime = runtime_settings

    def _get_runtime_setting(self, key: str, default=None):
        return self._runtime.get(key, default)

    # Импортируем реальный метод из BackendService как несвязанный и переносим сюда.
    # Это гарантирует что тест проверяет именно production-код, а не дубликат.
    from backend.service import BackendService as _BS
    _propagate_hf_token_to_env = _BS._propagate_hf_token_to_env


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class HfTokenPropagationTestCase(unittest.TestCase):
    """W1755: _propagate_hf_token_to_env wires hf_token из settings.json в os.environ."""

    def setUp(self):
        self._env_snapshot = _save_env()
        # Очищаем HF env ключи для чистоты теста
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        _restore_env(self._env_snapshot)

    # ------------------------------------------------------------------ basic

    def test_token_set_in_all_three_env_keys(self):
        """hf_token из settings → все три HF env-ключа получают значение."""
        svc = _StubService({"hf_token": "hf_TESTtoken123"})
        svc._propagate_hf_token_to_env()

        for k in _ENV_KEYS:
            self.assertEqual(
                os.environ.get(k), "hf_TESTtoken123",
                f"os.environ[{k!r}] should be 'hf_TESTtoken123' after propagation",
            )

    def test_empty_token_does_not_touch_env(self):
        """Пустой hf_token не трогает os.environ."""
        svc = _StubService({"hf_token": ""})
        svc._propagate_hf_token_to_env()

        for k in _ENV_KEYS:
            self.assertNotIn(k, os.environ)

    def test_missing_token_key_does_not_touch_env(self):
        """Отсутствие hf_token в settings не трогает os.environ."""
        svc = _StubService({})
        svc._propagate_hf_token_to_env()

        for k in _ENV_KEYS:
            self.assertNotIn(k, os.environ)

    # ---------------------------------------------------------------- precedence

    def test_existing_env_token_is_not_overwritten(self):
        """Уже установленный os.environ["HF_TOKEN"] НЕ перезаписывается токеном из GUI.

        Явный env-токен (например, из KRAB_EAR_HF_TOKEN или системного окружения)
        имеет приоритет над значением из settings.json.
        """
        os.environ["HF_TOKEN"] = "existing_env_token"
        svc = _StubService({"hf_token": "hf_GUI_token"})
        svc._propagate_hf_token_to_env()

        # Существующий токен должен сохраниться
        self.assertEqual(os.environ["HF_TOKEN"], "existing_env_token")

    def test_only_unset_keys_are_filled(self):
        """Только незаполненные env-ключи получают значение из GUI."""
        os.environ["HF_TOKEN"] = "already_set"
        # HUGGING_FACE_HUB_TOKEN и HUGGINGFACE_TOKEN не установлены
        svc = _StubService({"hf_token": "hf_GUI"})
        svc._propagate_hf_token_to_env()

        self.assertEqual(os.environ["HF_TOKEN"], "already_set")
        self.assertEqual(os.environ.get("HUGGING_FACE_HUB_TOKEN"), "hf_GUI")
        self.assertEqual(os.environ.get("HUGGINGFACE_TOKEN"), "hf_GUI")

    # ----------------------------------------------------------------- gigaam

    def test_gigaam_token_used_when_both_set(self):
        """stt_gigaam_hf_token имеет приоритет над hf_token если оба заданы."""
        svc = _StubService({
            "hf_token": "hf_generic",
            "stt_gigaam_hf_token": "hf_gigaam_specific",
        })
        svc._propagate_hf_token_to_env()

        # gigaam-специфичный токен должен попасть в env
        for k in _ENV_KEYS:
            self.assertEqual(os.environ.get(k), "hf_gigaam_specific")

    def test_generic_token_used_when_gigaam_empty(self):
        """Когда stt_gigaam_hf_token пустой, используется hf_token."""
        svc = _StubService({
            "hf_token": "hf_generic",
            "stt_gigaam_hf_token": "",
        })
        svc._propagate_hf_token_to_env()

        for k in _ENV_KEYS:
            self.assertEqual(os.environ.get(k), "hf_generic")

    # ---------------------------------------------------------------- no-log-value

    def test_token_value_not_logged(self):
        """Значение токена не должно попадать в лог (privacy).

        Проверяем через LogRecord handler — ловим все записи, которые генерирует
        _propagate_hf_token_to_env, и убеждаемся что ни одна не содержит сам токен.
        """
        import logging

        captured_messages: list[str] = []

        class _CapHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
                captured_messages.append(self.format(record))

        handler = _CapHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            svc = _StubService({"hf_token": "hf_SECRET_VALUE"})
            svc._propagate_hf_token_to_env()
        finally:
            root_logger.removeHandler(handler)

        # Ни одна лог-запись не должна содержать сам токен
        for line in captured_messages:
            self.assertNotIn("hf_SECRET_VALUE", line,
                             "token value must not appear in log output")


class HfTokenAfterSaveHookTestCase(unittest.TestCase):
    """W1755: after_save hook перепропагирует токен при изменении через set_settings.

    Lightweight: использует _StubService без тяжёлой BackendService инициализации.
    """

    def setUp(self):
        self._env_snapshot = _save_env()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        _restore_env(self._env_snapshot)

    def test_runtime_token_change_updates_env(self):
        """Изменение hf_token в runtime (старый vs новый dict) → env обновляется."""
        # Симулируем before/after save
        svc = _StubService({"hf_token": ""})

        # Первый вызов: токен пустой → env не трогается
        svc._propagate_hf_token_to_env()
        for k in _ENV_KEYS:
            self.assertNotIn(k, os.environ)

        # GUI устанавливает токен → обновляем stub и вызываем снова (как hook)
        svc._runtime["hf_token"] = "hf_NEW"
        svc._propagate_hf_token_to_env()

        for k in _ENV_KEYS:
            self.assertEqual(os.environ.get(k), "hf_NEW")


if __name__ == "__main__":
    unittest.main()
