"""Тесты scripts/record_golden.py: парсер сценария R2 и приём записи.

Сценарий `docs/golden/r2-scenario.md` — источник фраз для записи
golden-набора (стенд качества STT, эпик R2). Парсер обязан выдавать
ровно 30 RU + 12 ES + 8 EN со сквозными id `ru_001…` и не тащить
markdown-обёртки в текст фразы.

Приём WAV: размер выше порога недостаточен. Цифровая тишина (peak≈0)
не считается готовой записью — иначе loopback без микрофона даёт 50
нулевых файлов как «успех».
"""
from __future__ import annotations

import array
import importlib.util
import io
import math
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "record_golden.py"
SCENARIO_PATH = REPO_ROOT / "docs" / "golden" / "r2-scenario.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("record_golden_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses резолвит аннотации через sys.modules[cls.__module__] — до exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ParseScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()
        cls.phrases = cls.mod.parse_scenario(SCENARIO_PATH.read_text(encoding="utf-8"))

    def test_counts_per_language(self) -> None:
        counts: dict[str, int] = {}
        for phrase in self.phrases:
            counts[phrase.lang] = counts.get(phrase.lang, 0) + 1
        self.assertEqual(counts, {"ru": 30, "es": 12, "en": 8})

    def test_ids_are_sequential_and_unique(self) -> None:
        for lang, total in (("ru", 30), ("es", 12), ("en", 8)):
            ids = [p.phrase_id for p in self.phrases if p.lang == lang]
            self.assertEqual(ids, [f"{lang}_{i:03d}" for i in range(1, total + 1)])
        all_ids = [p.phrase_id for p in self.phrases]
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_first_phrases_sane(self) -> None:
        by_id = {p.phrase_id: p.text for p in self.phrases}
        self.assertTrue(by_id["ru_001"].startswith("Напомни"))
        self.assertTrue(by_id["es_001"].startswith("Recuérdame"))
        self.assertTrue(by_id["en_001"].startswith("Remind"))
        # Парсер не тащит markdown-обёртки и не режет текст.
        for text in by_id.values():
            self.assertNotIn("`", text)
            self.assertGreater(len(text), 10)

    def test_numbered_lines_outside_phrase_sections_ignored(self) -> None:
        """«Как записывать» тоже нумерован (1–6) — в фразы попасть не должен."""
        self.assertGreater(len(self.phrases), 0)
        for phrase in self.phrases:
            self.assertNotIn("mkdir", phrase.text)
            self.assertNotIn("ffmpeg", phrase.text)

    def test_empty_text_yields_no_phrases(self) -> None:
        self.assertEqual(self.mod.parse_scenario(""), [])

    def test_text_without_phrase_sections_yields_nothing(self) -> None:
        self.assertEqual(self.mod.parse_scenario("# Заголовок\n\n1. не фраза\n"), [])


def _write_mono_pcm16_wav(path: Path, samples: list[int], rate: int = 16000) -> None:
    """Канон golden: 16 kHz mono PCM16. Без numpy — как сам скрипт."""
    frames = array.array("h", samples)
    if sys.byteorder != "little":
        frames.byteswap()
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames.tobytes())


def _sine_pcm16(seconds: float = 1.0, amplitude: int = 8000, freq: float = 440.0, rate: int = 16000) -> list[int]:
    n = int(rate * seconds)
    return [
        int(amplitude * math.sin(2 * math.pi * freq * i / rate))
        for i in range(n)
    ]


class _FakeFfmpegProc:
    """ffmpeg, который на SIGINT дописывает заданный PCM в выходной путь."""

    def __init__(self, part_path: Path, samples: list[int]) -> None:
        self._part_path = part_path
        self._samples = samples
        self.returncode = None

    def poll(self):
        return self.returncode

    def send_signal(self, _sig) -> None:
        _write_mono_pcm16_wav(self._part_path, self._samples)
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.returncode = 1


class IsRecordedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_silent_wav_above_size_threshold_is_not_recorded(self) -> None:
        # Честный комментарий: смотрим поведение _is_recorded, не подстроку скрипта.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ru_001.wav"
            _write_mono_pcm16_wav(path, [0] * 16000)
            self.assertGreaterEqual(path.stat().st_size, self.mod.MIN_WAV_BYTES)
            self.assertFalse(self.mod._is_recorded(path))

    def test_wav_with_peak_energy_counts_as_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ru_002.wav"
            _write_mono_pcm16_wav(path, _sine_pcm16())
            self.assertGreaterEqual(path.stat().st_size, self.mod.MIN_WAV_BYTES)
            self.assertTrue(self.mod._is_recorded(path))

    def test_record_one_rejects_silence_and_does_not_leave_ready_wav(self) -> None:
        silent = [0] * 16000
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "ru_001.wav"
            part_path = out_path.with_name(out_path.name + ".part")

            def _popen(cmd, stdin=None):
                return _FakeFfmpegProc(Path(cmd[-1]), silent)

            captured = io.StringIO()
            with patch.object(self.mod.subprocess, "Popen", side_effect=_popen), \
                    patch.object(self.mod.time, "sleep"), \
                    patch("builtins.input", return_value=""), \
                    redirect_stdout(captured):
                ok = self.mod._record_one(":0", out_path)

            self.assertFalse(ok)
            self.assertFalse(out_path.exists())
            self.assertFalse(part_path.exists())
            output = captured.getvalue()
            self.assertIn("молчит", output)
            self.assertIn("list_devices", output)


if __name__ == "__main__":
    unittest.main()
