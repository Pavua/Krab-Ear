"""Константная уверенность обязана называться константой.

GigaAM не возвращает logprob, и адаптеры подставляют 0.9 — «типичное качество
модели». Значение уходит дальше как обычное измерение: в историю, в тренды
качества, в предупреждения о низкой уверенности и в гейт multipass-ретрая
(``confidence < threshold``). Следствия, замеренные 03.09.2026 на живой
истории владельца: русские записи (GigaAM) всегда 0.90 против 0.66–0.74 у
Whisper — то есть выглядят лучше независимо от реального качества, а ретрай по
уверенности для русского не может сработать ни разу.

Тот же класс, что ``SNR=0.0`` и пустой каталог LM Studio: sentinel «оценить не
смог» неотличим от измерения. Лечится не выбрасыванием значения (оно нужно
multipass, иначе GigaAM-текст всегда проигрывает whisper-прогону), а честной
пометкой ИСТОЧНИКА: ``confidence_source`` = ``constant`` | ``logprob``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class GigaAMAdaptersMarkConstantConfidence(unittest.TestCase):
    """Оба GigaAM-адаптера строят результат с пометкой источника."""

    def _adapter_sources(self) -> list[tuple[str, str]]:
        import ast

        out: list[tuple[str, str]] = []
        for name in ("stt_gigaam.py", "stt_gigaam_mlx.py"):
            path = PACKAGE_ROOT / "core" / "pipeline" / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                keys = {
                    k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if "confidence" in keys and "engine" in keys:
                    out.append((name, "confidence_source" if "confidence_source" in keys else ""))
        return out

    def test_every_result_dict_carries_confidence_source(self) -> None:
        found = self._adapter_sources()
        self.assertTrue(found, "не найден ни один результат-словарь адаптеров GigaAM")
        missing = [name for name, src in found if not src]
        self.assertEqual(
            missing, [],
            f"результат GigaAM без confidence_source: {missing} — константа 0.9 "
            "снова неотличима от замера",
        )


class EngineForwardsConfidenceSource(unittest.TestCase):
    """Движок обязан прокидывать источник наружу, а не терять его при сборке."""

    def test_engine_result_contains_confidence_source(self) -> None:
        src = (PACKAGE_ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        self.assertIn(
            '"confidence_source"', src,
            "engine собирает результат без confidence_source — пометка адаптера "
            "теряется на границе, и потребители снова видят голое число",
        )

    def test_default_source_is_logprob_not_constant(self) -> None:
        """Умолчание — logprob: путь Whisper реально считает по сегментам.

        Умолчание ``constant`` пометило бы честные замеры как фикцию — ошибка
        в обратную сторону, такая же вредная.
        """
        src = (PACKAGE_ROOT / "core" / "engine.py").read_text(encoding="utf-8")
        idx = src.find('"confidence_source"')
        self.assertGreater(idx, 0)
        window = src[idx : idx + 200]
        self.assertIn("logprob", window)
