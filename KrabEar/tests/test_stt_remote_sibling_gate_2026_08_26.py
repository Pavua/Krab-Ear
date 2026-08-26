"""Sibling-гейт облачного STT в ОСНОВНОМ каскаде (не только в multipass-retry).

🔴 Живой инцидент 2026-08-26 06:21 у владельца: тост «Критическая ошибка
распознавания речи — обратитесь к разработчику». Цепочка по логу:

    Таймаут 3600s при транскрибации моделью whisper-large-v3-mlx — пропускаю
    Локальные модели недоступны, переключаюсь на Remote STT...
    ERROR: Критическая ошибка распознавания
      → RuntimeError: Remote STT (openai) недоступен: no_api_key

Волна 2026-08-22 закрыла ровно этот класс, но только у ОДНОГО из двух
вызывающих (`retry_candidates` в multipass). Основной fallback-каскад
(«локальные модели недоступны → облако») гейта не получил — классическая
sibling-asymmetry. Побочный эффект хуже самой попытки: настоящая причина
(все локальные движки не справились) подменялась сообщением про облако.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _engine():
    from core.engine import AudioEngine

    eng = AudioEngine.__new__(AudioEngine)
    eng._settings_get = lambda k, d=None: {"cloud_stt_provider": "openai"}.get(k, d)
    eng._unavailable_models = {}
    return eng


class RemoteFallbackSiblingGateTest(unittest.TestCase):
    def test_no_api_key_does_not_call_remote_from_main_cascade(self):
        """Без ключа облачный путь не должен даже пытаться."""
        eng = _engine()
        with patch("backend.cloud_stt._load_settings", return_value={"openai_api_key": ""}):
            self.assertFalse(eng._remote_stt_retry_configured())

    def test_main_cascade_source_has_the_gate(self):
        """AST-контракт: обе ветки выбора remote обязаны звать один гейт.

        Проверяем именно вызов, а не подстроку в комментарии.
        """
        import ast

        src = (PROJECT_ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        gate_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_remote_stt_retry_configured"
        ]
        self.assertGreaterEqual(
            len(gate_calls), 2,
            "гейт применён только к одному вызывающему — sibling остался открыт",
        )

    def test_local_failure_message_names_the_real_cause(self):
        """🔴 Ошибка обязана называть КОРЕНЬ (локальные движки), а не последнее звено."""
        src = (PROJECT_ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("Все доступные STT-движки вышли из строя", src)
        idx = src.index("Локальные модели недоступны")
        # Окно в ОБЕ стороны: гейт стоит ПЕРЕД строкой лога, и односторонняя
        # проверка вперёд краснела бы на корректной реализации.
        window = src[max(0, idx - 800):idx + 800]
        self.assertIn("_remote_stt_retry_configured", window,
                      "основной каскад обязан проверять конфигурацию облака")


if __name__ == "__main__":
    unittest.main()
