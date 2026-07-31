"""Интеграция транспорта "mlx" в STTRouter + валидатор настроек (W-A волны).

Без реальной библиотеки gigaam_mlx (py3.12 ubuntu-parity): адаптер MLX
конструируется лениво и не импортирует gigaam_mlx в __init__.
"""
import unittest

from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter
from core.stt_router import STTRouter


class _Settings:
    STT_GIGAAM_ENABLED = True
    STT_GIGAAM_MODE = "v3_e2e_rnnt"
    STT_GIGAAM_DEVICE = "cpu"
    STT_GIGAAM_TRANSPORT = "mlx"
    STT_GIGAAM_VENV_PYTHON = ""
    MLX_TRANSCRIBE_TIMEOUT_SEC = 120.0


class TestRouterMLXTransport(unittest.TestCase):
    def test_mlx_transport_returns_mlx_adapter(self):
        router = STTRouter(_Settings())
        adapter = router.get_gigaam_adapter()
        self.assertIsInstance(adapter, GigaAMMLXAdapter)
        # Кеш: повторный вызов возвращает тот же экземпляр.
        self.assertIs(router.get_gigaam_adapter(), adapter)

    def test_transport_change_recreates_adapter(self):
        settings = _Settings()
        router = STTRouter(settings)
        mlx_adapter = router.get_gigaam_adapter()
        self.assertIsInstance(mlx_adapter, GigaAMMLXAdapter)
        # subprocess → PyTorch-класс (или None при недоступном стеке),
        # но точно НЕ прежний MLX-экземпляр: fingerprint включает transport.
        settings.STT_GIGAAM_TRANSPORT = "subprocess"
        other = router.get_gigaam_adapter()
        self.assertIsNot(other, mlx_adapter)

    def test_invalid_mode_degrades_to_none(self):
        settings = _Settings()
        settings.STT_GIGAAM_MODE = "no-such-mode"
        router = STTRouter(settings)
        self.assertIsNone(router.get_gigaam_adapter())

    def test_disabled_returns_none(self):
        settings = _Settings()
        settings.STT_GIGAAM_ENABLED = False
        router = STTRouter(settings)
        self.assertIsNone(router.get_gigaam_adapter())


class TestValidatorTransportEnum(unittest.TestCase):
    def test_mlx_accepted_and_garbage_fixed(self):
        from backend.settings_validator import _ENUM_FIELDS

        allowed = _ENUM_FIELDS["stt_gigaam_transport"]
        self.assertIn("mlx", allowed)
        self.assertIn("subprocess", allowed)
        # Первый элемент — безопасный дефолт при повреждённой настройке.
        self.assertEqual(allowed[0], "subprocess")

    def test_default_settings_have_transport_key(self):
        from core.config import DEFAULT_SETTINGS

        self.assertEqual(DEFAULT_SETTINGS.get("stt_gigaam_transport"), "subprocess")


if __name__ == "__main__":
    unittest.main(verbosity=2)
