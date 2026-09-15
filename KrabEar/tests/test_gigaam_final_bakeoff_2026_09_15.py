"""GigaAM-final bake-off T1 (волна 2026-09-15-gigaam-final-ru).

Холодный GigaAM-MLX теряет куски со звучащей речью → GigaAMMLXChunkLoss →
chain отдаёт финал Whisper (прод 14.09: кусок 0.0–1.0с сразу после загрузки
модели). Тест ловит это детерминированно: ОДИН свежий адаптер на весь файл,
первый файл — самый длинный (холод + максимум чанков = максимальный стресс).

Корпус — синтетика (say Milena + ffmpeg 16k mono), живых диктовок владельца
здесь нет. Ассерты — покрытие ключевыми словами (робастно к пунктуации
и нормализации числительных), НЕ exact-match: цель T1 — надёжность
(no chunk loss), качество меряем в T3.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

SAY = shutil.which("say")
FFMPEG = shutil.which("ffmpeg")
try:
    import mlx  # noqa: F401
    HAVE_MLX = True
except ImportError:
    HAVE_MLX = False

PHRASES = (
    (
        "long",
        (
            "Здравствуйте, это проверка связи. Неплохо работает быстрый "
            "транспорт, слова появляются практически сразу. Позвоните, "
            "пожалуйста, завтра после обеда, обсудим детали договора. "
            "Раз, два, три, четыре, пять."
        ),
        ("здравствуйте", "неплохо", "транспорт", "позвоните", "договора"),
    ),
    (
        "medium",
        (
            "Неплохо работает быстрый транспорт, слова появляются "
            "практически сразу. Позвоните завтра после обеда."
        ),
        ("неплохо", "транспорт", "позвоните"),
    ),
    (
        "short",
        "Неплохо работает быстрый транспорт.",
        ("неплохо", "транспорт"),
    ),
)


def _norm(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _weights_cached() -> bool:
    """Веса GigaAM-MLX в локальном HF-кэше (иначе офлайн-загрузка упадёт).

    CI-раннеры без кэша скипаются, а не краснеют: сеть в тестах запрещена
    W957-гардом, а качать ~2 ГБ ради одного файла — не его работа.
    """
    import glob as _glob
    roots = [os.environ.get("HF_HUB_CACHE"),
             os.path.expanduser("~/.cache/huggingface/hub")]
    for root in roots:
        if not root:
            continue
        for pat in ("*gigaam*", "*GigaAM*"):
            if _glob.glob(os.path.join(root, pat)):
                return True
    return False


def _needs_env() -> None:
    if sys.platform != "darwin" or SAY is None:
        raise unittest.SkipTest("нужен macOS say для синтетического корпуса")
    if FFMPEG is None:
        raise unittest.SkipTest("нужен ffmpeg для ресемпла в 16k")
    if not HAVE_MLX:
        raise unittest.SkipTest("нужен mlx для GigaAM-MLX")
    if not _weights_cached():
        raise unittest.SkipTest("нет весов GigaAM-MLX в HF-кэше (офлайн)")


@pytest.mark.slow
class GigaamFinalBakeoffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _needs_env()
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.files = []
        for name, phrase, keywords in PHRASES:
            aiff = Path(cls._tmp.name) / f"{name}.aiff"
            wav = Path(cls._tmp.name) / f"{name}_16k.wav"
            subprocess.run(["say", "-v", "Milena", "-o", str(aiff), phrase],
                           check=True, timeout=120)
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(aiff),
                            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                            str(wav)], check=True, timeout=120)
            cls.files.append((name, wav, keywords))
        # ОДИН свежий адаптер на весь файл: первый transcribe идёт на
        # холодный Metal (условие cold-loss из прода 14.09), дальше тепло.
        # Офлайн-режим HF как у прод-процесса (launchd ставит
        # TRANSFORMERS_OFFLINE=1): веса берём из локального кэша, сеть в
        # тестах запрещена W957-гардом conftest.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter
        cls.adapter = GigaAMMLXAdapter(mode="v3_e2e_rnnt")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.adapter.close()
        except Exception:
            pass
        cls._tmp.cleanup()

    def _transcribe_file(self, name, wav, keywords):
        import soundfile as sf
        audio, sr = sf.read(str(wav), dtype="float32")
        # GigaAMMLXChunkLoss на холодных/потерянных кусках = RED этой волны:
        # исключение НЕ гасим, тест падает.
        result = self.adapter.transcribe(audio, sample_rate=sr)
        normed = _norm(str(result.get("text") or ""))
        missing = [kw for kw in keywords if kw not in normed]
        self.assertEqual(
            missing, [],
            f"{name}: потеряны ключевые слова {missing} (распознано: {normed[:120]!r})",
        )

    def test_1_long_cold(self):
        name, wav, keywords = self.files[0]
        self._transcribe_file(name, wav, keywords)

    def test_2_medium_warm(self):
        name, wav, keywords = self.files[1]
        self._transcribe_file(name, wav, keywords)

    def test_3_short_warm(self):
        name, wav, keywords = self.files[2]
        self._transcribe_file(name, wav, keywords)


if __name__ == "__main__":
    unittest.main()
