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
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_generic_token_wins_over_gigaam_when_both_set(self):
        """W1755 hardening: hf_token (generic) имеет приоритет над stt_gigaam_hf_token.

        gigaam-специфичный токен может не иметь прав на pyannote gating (spurious 401).
        Поэтому general hf_token должен попадать в generic env-ключи.
        """
        svc = _StubService({
            "hf_token": "hf_generic",
            "stt_gigaam_hf_token": "hf_gigaam_specific",
        })
        svc._propagate_hf_token_to_env()

        # generic токен должен попасть в env (не gigaam-специфичный)
        for k in _ENV_KEYS:
            self.assertEqual(os.environ.get(k), "hf_generic")

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


class HfTokenOverwriteTestCase(unittest.TestCase):
    """W1755 hardening: overwrite=True обновляет os.environ при смене токена через set_settings.

    Проверяет что after_save hook (overwrite=True) действительно перезаписывает env когда
    токен меняется, а init (overwrite=False) сохраняет setdefault-семантику.
    """

    def setUp(self):
        self._env_snapshot = _save_env()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        _restore_env(self._env_snapshot)

    def test_overwrite_true_updates_env_on_token_change(self):
        """overwrite=True: смена tok_A → tok_B обновляет os.environ до tok_B."""
        # Шаг 1: init-вызов устанавливает tok_A
        svc = _StubService({"hf_token": "hf_TOKEN_A"})
        svc._propagate_hf_token_to_env(overwrite=False)
        for k in _ENV_KEYS:
            self.assertEqual(os.environ.get(k), "hf_TOKEN_A", f"{k} should be TOKEN_A after init")

        # Шаг 2: пользователь меняет токен — после_save hook вызывается с overwrite=True
        svc._runtime["hf_token"] = "hf_TOKEN_B"
        svc._propagate_hf_token_to_env(overwrite=True)
        for k in _ENV_KEYS:
            self.assertEqual(
                os.environ.get(k), "hf_TOKEN_B",
                f"{k} should be TOKEN_B after overwrite=True",
            )

    def test_overwrite_false_does_not_replace_existing_env(self):
        """overwrite=False (init): существующий env-токен НЕ перезаписывается GUI-токеном."""
        os.environ["HF_TOKEN"] = "existing_from_shell"
        svc = _StubService({"hf_token": "hf_FROM_GUI"})
        svc._propagate_hf_token_to_env(overwrite=False)
        # Существующий токен должен сохраниться
        self.assertEqual(os.environ["HF_TOKEN"], "existing_from_shell")

    def test_overwrite_true_replaces_all_three_keys(self):
        """overwrite=True: все три HF env-ключа получают новое значение."""
        # Устанавливаем все три ключа со старым значением
        for k in _ENV_KEYS:
            os.environ[k] = "hf_OLD"

        svc = _StubService({"hf_token": "hf_NEW"})
        svc._propagate_hf_token_to_env(overwrite=True)
        for k in _ENV_KEYS:
            self.assertEqual(os.environ.get(k), "hf_NEW", f"{k} should be hf_NEW")

    def test_generic_hf_token_preferred_over_gigaam_for_env_keys(self):
        """W1755 hardening: hf_token (generic) имеет приоритет над stt_gigaam_hf_token.

        gigaam-специфичный токен может не иметь прав на pyannote gating → spurious 401.
        Поэтому generic hf_token должен попадать в env-ключи, а не gigaam-токен.
        """
        svc = _StubService({
            "hf_token": "hf_GENERIC",
            "stt_gigaam_hf_token": "hf_GIGAAM_ONLY",
        })
        svc._propagate_hf_token_to_env()
        for k in _ENV_KEYS:
            self.assertEqual(
                os.environ.get(k), "hf_GENERIC",
                f"{k} should be GENERIC (not GIGAAM)",
            )

    def test_gigaam_token_fallback_when_generic_empty(self):
        """stt_gigaam_hf_token используется как фолбэк когда hf_token пустой."""
        svc = _StubService({
            "hf_token": "",
            "stt_gigaam_hf_token": "hf_GIGAAM_FALLBACK",
        })
        svc._propagate_hf_token_to_env()
        for k in _ENV_KEYS:
            self.assertEqual(os.environ.get(k), "hf_GIGAAM_FALLBACK", f"{k} should be GIGAAM_FALLBACK")

    def test_null_byte_token_does_not_raise(self):
        """Null-byte в токене (ValueError из os.environ) перехватывается без raise."""
        svc = _StubService({"hf_token": "hf_" + chr(0) + "invalid"})
        # Должно завершиться без исключения
        try:
            svc._propagate_hf_token_to_env(overwrite=True)
        except Exception as exc:
            self.fail(f"_propagate_hf_token_to_env raised unexpectedly: {exc}")
        # env не должен содержать мусор
        for k in _ENV_KEYS:
            self.assertNotEqual(os.environ.get(k), "hf_" + chr(0) + "invalid")

    def test_overwrite_clears_pipeline_and_cached_load_error_under_real_locks(self):
        """Новый токен обязан восстановить pyannote после закешированного 401."""
        engine = SimpleNamespace(
            _diarization_pipeline=object(),
            _diarization_load_error="старый 401",
            _diarization_run_lock=threading.Lock(),
            _diarization_load_lock=threading.RLock(),
        )
        svc = _StubService({"hf_token": "hf_RECOVERY"})
        svc.transcriber = SimpleNamespace(engine=engine)

        svc._propagate_hf_token_to_env(overwrite=True)

        self.assertIsNone(engine._diarization_pipeline)
        self.assertIsNone(engine._diarization_load_error)

    def test_invalidation_waits_until_active_inference_releases_run_lock(self):
        """Hot reload не должен обнулить pipeline во время активного инференса."""
        engine = SimpleNamespace(
            _diarization_pipeline=object(),
            _diarization_load_error="старый 401",
            _diarization_run_lock=threading.Lock(),
            _diarization_load_lock=threading.RLock(),
        )
        svc = _StubService({"hf_token": "hf_AFTER_INFERENCE"})
        svc.transcriber = SimpleNamespace(engine=engine)
        finished = threading.Event()

        engine._diarization_run_lock.acquire()
        worker = threading.Thread(
            target=lambda: (
                svc._propagate_hf_token_to_env(overwrite=True),
                finished.set(),
            ),
            daemon=True,
        )
        worker.start()
        try:
            self.assertFalse(finished.wait(0.05))
            self.assertIsNotNone(engine._diarization_pipeline)
            self.assertEqual(engine._diarization_load_error, "старый 401")
        finally:
            engine._diarization_run_lock.release()
        self.assertTrue(finished.wait(1.0))
        worker.join(timeout=1.0)
        self.assertIsNone(engine._diarization_pipeline)
        self.assertIsNone(engine._diarization_load_error)


if __name__ == "__main__":
    unittest.main()
