"""F03 (ревью #2329): потолок декодированного PCM в аудиоаналитике.

Предел снимка (1 ГиБ) и предел файла в analyze_file (500 МБ) считают байты
НА ДИСКЕ. Сжатый файл раскрывается многократно: 30 минут стерео-тишины 48 кГц
во FLAC — 335 КБ на диске, 659 МБ float32 после декодирования; пик RSS
обработчиков quality/silence/profile_noise был 1.4–2.4 ГБ (замер 28.11).

Тесты работают на НАСТОЯЩИХ FLAC-файлах и настоящем soundfile. Подменяется
только числовой потолок (маленький), а не размер файла. «До декодирования»
доказывается tracemalloc: numpy регистрирует аллокации, и пик должен быть
намного меньше декодированного объёма.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_analytics_service import AudioAnalyticsService  # noqa: E402

SR = 16000


def _make_service() -> AudioAnalyticsService:
    return AudioAnalyticsService(
        audio_converter=MagicMock(),
        quality_trends=MagicMock(),
        audio_fingerprinter=MagicMock(),
        word_timing_analyzer=MagicMock(),
        store=MagicMock(data_dir=None),
    )


def _write_flac(path: Path, seconds: float, channels: int = 1, silent: bool = False) -> int:
    """Пишет FLAC (тихий тон или тишина); возвращает число сэмплов (кадры × каналы)."""
    frames = int(SR * seconds)
    t = np.arange(frames, dtype=np.float32) / SR
    mono = np.zeros(frames, dtype=np.float32) if silent else (0.05 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    data = np.repeat(mono[:, None], channels, axis=1) if channels > 1 else mono
    sf.write(str(path), data, SR, format="FLAC", subtype="PCM_16")
    return frames * channels


HANDLERS = {
    "analyze_audio_quality": lambda svc, p: svc.handle_analyze_audio_quality({"file_path": p}),
    "analyze_silence": lambda svc, p: svc.handle_analyze_silence({"file_path": p}),
    "profile_noise": lambda svc, p: svc.handle_profile_noise({"file_path": p}),
}


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ke_f03_")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _cap(self, samples: int):
        import core.audio_decode_guard as guard
        return patch.object(guard, "MAX_DECODED_SAMPLES", samples)


class TestDecodedPcmCapRefusesBeforeDecoding(_Tmp):
    def test_over_cap_file_is_refused_without_decoding(self) -> None:
        # 2 минуты стерео-тишины: ~десятки КБ на диске, 15 МБ float32 после декодирования.
        path = self.root / "long.flac"
        samples = _write_flac(path, seconds=120.0, channels=2, silent=True)
        decoded_bytes = samples * 4
        self.assertLess(path.stat().st_size * 50, decoded_bytes, "файл должен сжиматься многократно")
        # Импорты анализаторов заранее — их аллокации не должны попасть в замер.
        import core.audio_quality  # noqa: F401
        import core.noise_profiler  # noqa: F401
        import core.silence_detector  # noqa: F401
        for name, call in HANDLERS.items():
            with self.subTest(handler=name), self._cap(samples // 4):
                svc = _make_service()
                tracemalloc.start()
                try:
                    with self.assertRaises(ValueError) as cm:
                        call(svc, str(path))
                    _cur, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertIn("декодирован", str(cm.exception))
                self.assertLess(peak, decoded_bytes // 4,
                                f"{name}: пик {peak} Б — похоже, PCM декодирован до отказа")

    def test_cap_counts_samples_across_channels(self) -> None:
        # Стерео вдвое тяжелее моно той же длительности: потолок — по кадрам × каналы.
        mono = self.root / "mono.flac"
        stereo = self.root / "stereo.flac"
        mono_samples = _write_flac(mono, seconds=2.0, channels=1)
        _write_flac(stereo, seconds=2.0, channels=2)
        with self._cap(mono_samples):
            svc = _make_service()
            HANDLERS["analyze_silence"](svc, str(mono))  # ровно на пределе — допускается
            with self.assertRaises(ValueError):
                HANDLERS["analyze_silence"](svc, str(stereo))


class TestDecodedPcmCapKeepsLegitAnalysis(_Tmp):
    def test_under_cap_file_is_analyzed_as_before(self) -> None:
        path = self.root / "ok.flac"
        _write_flac(path, seconds=1.5, channels=1)
        svc = _make_service()
        quality = HANDLERS["analyze_audio_quality"](svc, str(path))
        self.assertAlmostEqual(quality["duration_sec"], 1.5, places=2)
        silence = HANDLERS["analyze_silence"](svc, str(path))
        self.assertAlmostEqual(silence["duration_sec"], 1.5, places=2)
        noise = HANDLERS["profile_noise"](svc, str(path))
        self.assertIsInstance(noise, dict)


class TestReadAudioCappedHelper(_Tmp):
    def test_returns_same_data_as_sf_read(self) -> None:
        from core.audio_decode_guard import read_audio_capped

        path = self.root / "same.flac"
        _write_flac(path, seconds=0.5, channels=2)
        expected, esr = sf.read(str(path), dtype="float32", always_2d=False)
        got, gsr = read_audio_capped(path)
        self.assertEqual(gsr, esr)
        np.testing.assert_array_equal(got, expected)

    def test_frames_beyond_declared_header_are_refused(self) -> None:
        # Защитная ветка: если библиотека отдаст больше кадров, чем объявлено в
        # заголовке (заголовок занижен), потолок держится по ФАКТИЧЕСКИ прочитанному.
        import core.audio_decode_guard as guard

        class LyingFile:
            channels = 1
            samplerate = SR
            frames = 10  # заголовок «10 кадров»

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, frames, dtype, always_2d):
                return np.zeros(frames, dtype=np.float32)  # отдаёт столько, сколько попросили

        with patch.object(guard, "MAX_DECODED_SAMPLES", 100), \
             patch("soundfile.SoundFile", return_value=LyingFile()):
            with self.assertRaises(ValueError):
                guard.read_audio_capped(self.root / "x.flac")


if __name__ == "__main__":
    unittest.main()
