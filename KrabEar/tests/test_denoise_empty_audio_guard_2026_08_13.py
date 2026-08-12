"""Гард против вырожденного аудио в STT-пути (живой инцидент 2026-08-12).

Живое воспроизведение (прод, backend pid 21798):
1. `live_subs_ingest` с 4.4с аудио 16 кГц.
2. `[STT] noise SNR=0.0 dB < 15.0 dB → denoising applied (strength=moderate)`.
3. `GigaAM transcribe failed (duration=0.0s, longform=False): ...
   ValueError: both buffer length (0) and count (-1) must not be 0`.

Разбор цепочки:
- `NoiseProfiler._silent_profile()` отдаёт `snr_db=0.0` как SENTINEL «оценить не
  смог» (аудио короче `_FRAME_SIZE`), и ещё четыре ветки `_compute_snr`
  возвращают ровно `0.0` по той же причине. То есть SNR=0.0 в логе —
  не «шум равен сигналу», а признак вырожденного входа.
- Деноизер укоротить массив не может (spectral gating выравнивает выход по
  `len(audio)`), но лог «denoising applied» печатается ДО его вызова — поэтому
  строка в логе выглядела как признание вины деноизера.
- Пустое окно рождается в `LiveSubsService.ingest`: при `is_final=True` flush
  выполняется безусловно, даже когда буфер содержит только что опустошённый
  список — снапшот получается нулевой длины и едет прямо в STT.

Три слоя защиты, по одному классу на слой:
1. `DenoiseResultGuardTestCase` — результат деноизера проверяется на пустоту,
   смену длины и обвал энергии; направление отказа — вернуть ИСХОДНОЕ аудио.
2. `TranscribeEmptyAudioGuardTestCase` — вырожденный numpy-вход не доезжает до
   STT-адаптера, а возвращает пустой результат по контракту.
3. `LiveSubsEmptyWindowGuardTestCase` — пустое окно вообще не сабмитится.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SR = 16000


def _make_audio(duration_sec: float = 3.0, sr: int = SR) -> np.ndarray:
    """Синтетическое шумное аудио достаточной длины для оценки SNR."""
    n = int(duration_sec * sr)
    rng = np.random.default_rng(1312)
    return (rng.standard_normal(n) * 0.05).astype(np.float32)


def _build_engine() -> "AudioEngine":  # noqa: F821
    """Минимально-инициализированный AudioEngine без тяжёлых зависимостей."""
    from core.engine import AudioEngine

    engine = AudioEngine.__new__(AudioEngine)
    engine._confidence_calibrator = MagicMock()
    engine._llm_rewriter = MagicMock()
    engine.current_model = "balanced"
    engine._unavailable_models = {}
    engine._metrics_collector = MagicMock()
    engine._error_bus = MagicMock()
    return engine


def _profiler_returning(snr_db: float) -> MagicMock:
    """Заглушка класса NoiseProfiler, чей profile() отдаёт заданный SNR."""
    instance = MagicMock()
    instance.profile.return_value = SimpleNamespace(snr_db=snr_db)
    return MagicMock(return_value=instance)


def _denoiser_returning(output: np.ndarray) -> MagicMock:
    """Заглушка класса AudioDenoiser, чей denoise() отдаёт заданный массив."""
    instance = MagicMock()
    instance.denoise.return_value = output
    return MagicMock(return_value=instance)


class DenoiseResultGuardTestCase(unittest.TestCase):
    """Результат шумоподавления проверяется перед передачей дальше по пайплайну."""

    def setUp(self) -> None:
        from core import config as cfg

        self.settings = cfg.settings
        self._orig_threshold = self.settings.STT_DENOISE_SNR_THRESHOLD_DB
        self._orig_strength = self.settings.STT_DENOISE_STRENGTH
        self.settings.STT_DENOISE_SNR_THRESHOLD_DB = 15.0
        self.settings.STT_DENOISE_STRENGTH = "moderate"
        self.engine = _build_engine()
        self.audio = _make_audio(3.0)

    def tearDown(self) -> None:
        self.settings.STT_DENOISE_SNR_THRESHOLD_DB = self._orig_threshold
        self.settings.STT_DENOISE_STRENGTH = self._orig_strength

    def _run_denoise(self, snr_db: float, denoised: np.ndarray) -> np.ndarray:
        """Прогоняет _maybe_denoise с подменёнными профайлером и деноизером."""
        with (
            patch("core.noise_profiler.NoiseProfiler", _profiler_returning(snr_db)),
            patch("core.audio_denoiser.AudioDenoiser", _denoiser_returning(denoised)),
        ):
            return self.engine._maybe_denoise(self.audio)

    def test_empty_denoiser_output_falls_back_to_original(self) -> None:
        """Пустой выход деноизера НЕ уходит дальше — возвращается исходное аудио."""
        result = self._run_denoise(6.0, np.array([], dtype=np.float32))

        self.assertEqual(len(result), len(self.audio))
        np.testing.assert_array_equal(result, self.audio)

    def test_empty_denoiser_output_logs_warning(self) -> None:
        """Откат обязан быть громким: WARNING, а не тихая подмена."""
        with self.assertLogs("KrabEar", level="WARNING") as captured:
            self._run_denoise(6.0, np.array([], dtype=np.float32))

        joined = "\n".join(captured.output)
        self.assertIn("denois", joined.lower())

    def test_length_mismatch_falls_back_to_original(self) -> None:
        """Смена длины — тоже повреждение: таймстемпы Whisper поедут."""
        truncated = self.audio[: len(self.audio) // 2].copy()

        result = self._run_denoise(6.0, truncated)

        np.testing.assert_array_equal(result, self.audio)

    def test_energy_collapse_falls_back_to_original(self) -> None:
        """Нулевая энергия при верной длине — сигнал выжжен, откатываемся."""
        silenced = np.zeros_like(self.audio)

        result = self._run_denoise(6.0, silenced)

        np.testing.assert_array_equal(result, self.audio)

    def test_healthy_denoise_result_passes_through(self) -> None:
        """Гард не должен глушить нормальную работу деноизера."""
        cleaned = (self.audio * 0.6).astype(np.float32)

        result = self._run_denoise(6.0, cleaned)

        np.testing.assert_array_equal(result, cleaned)

    def test_zero_snr_sentinel_skips_denoising(self) -> None:
        """SNR ровно 0.0 — sentinel «оценить не смог», деноизер не запускается."""
        denoiser_cls = _denoiser_returning(np.zeros_like(self.audio))

        with (
            patch("core.noise_profiler.NoiseProfiler", _profiler_returning(0.0)),
            patch("core.audio_denoiser.AudioDenoiser", denoiser_cls),
        ):
            result = self.engine._maybe_denoise(self.audio)

        denoiser_cls.assert_not_called()
        np.testing.assert_array_equal(result, self.audio)

    def test_degenerate_audio_skips_profiling_entirely(self) -> None:
        """Аудио короче окна NoiseProfiler не профилируется и не деноизится.

        Это ровно вход живого инцидента: окно нулевой длины, для которого
        _silent_profile() и выдавал SNR=0.0.
        """
        profiler_cls = _profiler_returning(0.0)
        denoiser_cls = _denoiser_returning(np.array([], dtype=np.float32))
        tiny = np.zeros(800, dtype=np.float32)

        with (
            patch("core.noise_profiler.NoiseProfiler", profiler_cls),
            patch("core.audio_denoiser.AudioDenoiser", denoiser_cls),
        ):
            result = self.engine._maybe_denoise(tiny)

        profiler_cls.assert_not_called()
        denoiser_cls.assert_not_called()
        np.testing.assert_array_equal(result, tiny)


class TranscribeEmptyAudioGuardTestCase(unittest.TestCase):
    """Вырожденный numpy-вход не должен доезжать до STT-адаптера."""

    def setUp(self) -> None:
        from core import config as cfg

        self.settings = cfg.settings
        self._saved = {
            "denoise": self.settings.STT_DENOISE_ENABLED,
            "vad": self.settings.STT_VAD_PREFILTER_ENABLED,
            "multipass": self.settings.STT_MULTIPASS_ENABLED,
            "streaming": self.settings.STT_STREAMING_ENABLED,
        }
        self.settings.STT_DENOISE_ENABLED = True
        self.settings.STT_VAD_PREFILTER_ENABLED = False
        self.settings.STT_MULTIPASS_ENABLED = False
        self.settings.STT_STREAMING_ENABLED = False
        self.engine = _build_engine()
        self.fallback = MagicMock(return_value={"text": "не должно вызваться"})
        self.engine._transcribe_with_fallback = self.fallback

    def tearDown(self) -> None:
        self.settings.STT_DENOISE_ENABLED = self._saved["denoise"]
        self.settings.STT_VAD_PREFILTER_ENABLED = self._saved["vad"]
        self.settings.STT_MULTIPASS_ENABLED = self._saved["multipass"]
        self.settings.STT_STREAMING_ENABLED = self._saved["streaming"]

    def test_empty_array_does_not_reach_stt(self) -> None:
        """Массив нулевой длины → пустой результат вместо краша в адаптере."""
        result = self.engine.transcribe(np.array([], dtype=np.float32))

        self.fallback.assert_not_called()
        self.assertEqual(result.get("text"), "")
        self.assertEqual(result.get("engine"), "empty_audio")

    def test_empty_result_matches_vad_skip_schema(self) -> None:
        """Ранний возврат обязан нести те же ключи, что и существующий vad_skip.

        Потребители (BackendService, live subs, история) читают эти поля без
        .get-заглушек — недостающий ключ обернулся бы KeyError уже в проде.
        """
        result = self.engine.transcribe(np.array([], dtype=np.float32))

        for key in (
            "text", "raw_text", "cleaned_text", "llm_applied", "confidence",
            "raw_confidence", "duration_ms", "engine", "model", "language",
            "segments", "diarization", "emotion",
        ):
            self.assertIn(key, result, f"ключ {key} отсутствует в пустом результате")

    def test_non_empty_audio_still_reaches_stt(self) -> None:
        """Гард не должен отсекать нормальную запись."""
        self.fallback.return_value = {
            "text": "привет", "segments": [], "confidence": 0.9,
        }

        with patch.object(self.engine, "_maybe_denoise", side_effect=lambda a: a):
            self.engine.transcribe(_make_audio(2.0))

        self.fallback.assert_called_once()


class LiveSubsEmptyWindowGuardTestCase(unittest.TestCase):
    """Пустое окно не сабмитится в STT-воркер (источник живого инцидента)."""

    def _build_service(self) -> "LiveSubsService":  # noqa: F821
        from backend.live_subs_service import LiveSubsService

        return LiveSubsService(
            transcriber=MagicMock(),
            translator=MagicMock(),
            settings_get=lambda k, d: d,
        )

    def test_final_flush_with_empty_buffer_does_not_submit(self) -> None:
        """is_final при пустом буфере — нечего распознавать, окно не создаётся.

        Ответ при этом пустой, а НЕ None: None по контракту handle_ingest
        означает «воркер не успел за таймаут» (reason=flush_timeout), и выдавать
        этот диагноз вместо честного «нечего флашить» — тот же класс вранья,
        что и исходный баг.
        """
        service = self._build_service()

        with patch.object(service, "_submit_window") as submit:
            result = service.ingest(b"", SR, "ru", is_final=True)

        submit.assert_not_called()
        self.assertEqual(result, {"text": "", "translation": None})

    def test_final_flush_with_empty_buffer_reports_flushed_over_ipc(self) -> None:
        """IPC-ответ не должен маскировать «нечего флашить» под flush_timeout."""
        import base64

        service = self._build_service()

        with patch.object(service, "_submit_window") as submit:
            response = service.handle_ingest({
                "audio_chunk": base64.b64encode(b"").decode(),
                "sample_rate": SR,
                "target_lang": "off",
                "is_final": True,
            })

        submit.assert_not_called()
        self.assertEqual(response.get("status"), "flushed")
        self.assertEqual(response.get("text"), "")
        self.assertNotIn("reason", response)

    def test_final_flush_with_data_still_submits(self) -> None:
        """Нормальный финальный flush по-прежнему уходит в воркер."""
        service = self._build_service()
        pcm = (np.ones(SR, dtype=np.int16) * 1000).tobytes()

        with (
            patch.object(service, "_submit_window", return_value=7) as submit,
            patch.object(service, "_await_completion", return_value={"text": "ок"}),
        ):
            result = service.ingest(pcm, SR, "ru", is_final=True)

        submit.assert_called_once()
        self.assertEqual(result, {"text": "ок"})

    def test_stop_with_only_empty_chunks_does_not_submit(self) -> None:
        """stop() после серии пустых чанков тоже не должен строить окно."""
        service = self._build_service()
        service.ingest(b"", SR, "ru", is_final=False)

        with patch.object(service, "_submit_window") as submit:
            service.stop()

        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
