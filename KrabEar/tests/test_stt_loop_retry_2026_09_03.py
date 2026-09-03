"""Зацикленная транскрипция перепрогоняется другим движком, а не отдаётся как есть.

До 03.09.2026 детектор `is_likely_repetition_loop` только предупреждал: текст
возвращался неизменным, владелец получал тост «перезапиши». Для русского это
единственная защита вовсе — ретрай по уверенности там мёртв, потому что GigaAM
отдаёт константу 0.9 (см. confidence_source, #1985), и порог никогда не срабатывает.

Контракт ретрая:
  * срабатывает только на финальной транскрибации (не превью, не live subs);
  * ровно одна дополнительная попытка — диктовка не должна удлиняться вдвое;
  * берём результат повтора ТОЛЬКО если он сам не зациклен и не пуст;
  * иначе возвращаем исходный текст — «не врём про input» остаётся в силе;
  * сбой повтора (исключение, таймаут) не ломает диктовку.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.engine import AudioEngine  # noqa: E402
from core.utils import is_likely_repetition_loop  # noqa: E402

LOOPED = "да да да да да да да да да да да да да да да да да да да да да да"
CLEAN = "Позвони в аптеку и уточни дозировку прегабалина на завтра."


class _Engine(AudioEngine):
    """Движок без инициализации тяжёлых зависимостей — нужен только метод ретрая."""

    def __init__(self) -> None:  # noqa: D107 — намеренно не зовём super()
        self.current_model = "mlx-community/whisper-large-v3-mlx"
        # Реальный тип — dict[model_id, monotonic_ts] с TTL, не set: фейк с set
        # ронял бы _is_model_unavailable на .get и проверял бы фантазию.
        self._unavailable_models: dict[str, float] = {}
        self.calls: list[str] = []


class LoopRetryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _Engine()
        self.first = {"text": LOOPED, "engine": "gigaam-rnnt", "confidence": 0.9,
                      "confidence_source": "constant", "language": "ru"}

    def _retry(self, replacement: dict | Exception):
        # patch.object подменяет атрибут КЛАССА, поэтому self приходит первым —
        # без него аргументы едут на один вправо и метод падает TypeError
        # (fail-safe кода это проглотит, и тест проверял бы не то, что заявляет).
        def fake(_self, audio, model_name, prompt, language=None):
            self.engine.calls.append(model_name)
            if isinstance(replacement, Exception):
                raise replacement
            return dict(replacement)

        with patch.object(_Engine, "_transcribe_model", fake):
            return self.engine._maybe_repetition_loop_retry(
                audio_data=object(), prompt="", language="ru",
                first_result=self.first, loop_reason="bigram_loop",
            )

    def test_clean_retry_replaces_looped_text(self) -> None:
        out = self._retry({"text": CLEAN, "engine": "mlx-whisper", "confidence": 0.71})
        self.assertEqual(out["text"], CLEAN, "чистый повтор обязан заменить зацикленный текст")
        self.assertTrue(out.get("loop_retry_applied"))
        self.assertEqual(len(self.engine.calls), 1, "ретрай обязан быть ровно один")

    def test_looped_retry_keeps_original(self) -> None:
        out = self._retry({"text": LOOPED + " да", "engine": "mlx-whisper"})
        self.assertEqual(out["text"], LOOPED, "оба зациклены — отдаём исходный, не врём про input")
        self.assertFalse(out.get("loop_retry_applied", False))

    def test_empty_retry_keeps_original(self) -> None:
        out = self._retry({"text": "   ", "engine": "mlx-whisper"})
        self.assertEqual(out["text"], LOOPED, "пустой повтор не лучше зацикленного")

    def test_retry_failure_never_breaks_dictation(self) -> None:
        out = self._retry(RuntimeError("MLX упал"))
        self.assertEqual(out["text"], LOOPED)
        self.assertIn("loop_retry_error", out)

    def test_disabled_by_setting(self) -> None:
        with patch("core.engine.settings") as st:
            st.STT_LOOP_RETRY_ENABLED = False
            out = self.engine._maybe_repetition_loop_retry(
                audio_data=object(), prompt="", language="ru",
                first_result=self.first, loop_reason="bigram_loop",
            )
        self.assertEqual(out["text"], LOOPED)
        self.assertEqual(self.engine.calls, [], "выключенная настройка не должна тратить время")

    def test_fixture_sanity(self) -> None:
        """Образцы обязаны быть тем, чем притворяются, иначе тест проверяет пустоту."""
        self.assertTrue(is_likely_repetition_loop(LOOPED)[0])
        self.assertFalse(is_likely_repetition_loop(CLEAN)[0])
