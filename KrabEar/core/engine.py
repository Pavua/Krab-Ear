"""Ядро Krab Ear: локальная транскрибация и интеграция с внешними сервисами.

AudioEngine управляет жизненным циклом STT-моделей (через mlx-whisper), нормализацией аудио
и взаимодействием со шлюзами (LLM/Remote STT). Для файловых импортов движок
может дополнять результат diarization спикеров через pyannote.audio.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re as _re
import subprocess
import tempfile
import time
from typing import Any
from pathlib import Path

import mlx_whisper
import numpy as np
import requests
import soundfile as sf
import torch
from pyannote.audio import Pipeline

from .config import settings
from .utils import TextUtils

logger = logging.getLogger("KrabEar.Engine")

# ---------------------------------------------------------------------------
# Утилита: проверка доступной памяти macOS через vm_stat
# ---------------------------------------------------------------------------

# Минимум свободной (free + inactive) памяти для загрузки тяжёлых моделей.
# whisper-large-v3-mlx занимает ~3GB, pyannote ~1.5GB. Оставляем запас.
_HEAVY_MODEL_MIN_FREE_GB = 4.0


def _get_available_memory_gb() -> float:
    """Возвращает примерный объём доступной памяти (free + inactive) в GB.

    Использует macOS `vm_stat` — это дешёвый вызов без зависимостей.
    При ошибке возвращает -1 (не блокируем работу если не macOS).
    """
    try:
        result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
        if result.returncode != 0:
            return -1.0
        page_size = os.sysconf("SC_PAGE_SIZE")
        free_pages = 0
        inactive_pages = 0
        for line in result.stdout.splitlines():
            if "Pages free" in line:
                m = _re.search(r"(\d+)", line.split(":")[1])
                if m:
                    free_pages = int(m.group(1))
            elif "Pages inactive" in line:
                m = _re.search(r"(\d+)", line.split(":")[1])
                if m:
                    inactive_pages = int(m.group(1))
        return (free_pages + inactive_pages) * page_size / (1024 ** 3)
    except Exception:
        return -1.0

class AudioEngine:
    """Сервисный слой для STT ( Speech-to-Text) и TTS (Text-to-Speech)."""

    DOMAIN_PROMPTS = {
        "casual": "Разговорная речь, сленг, обычный стиль.",
        "finance": "Финансовая терминология, числа, валюты, банки, котировки.",
        "code": "Программирование, названия функций, переменные, технический английский. Python, Javascript, API.",
        "meeting": "Протокольная запись встречи, четкая структура, официальный тон.",
        "legal": "Юридические термины, законы, кодексы, официальные документы."
    }

    def __init__(self) -> None:
        """Инициализирует двигатель, загружая настройки из централизованного конфига."""
        self.current_model = settings.MODEL_BALANCED
        self.quality_profile = "balanced"
        self._unavailable_models: set[str] = set()
        self._diarization_pipeline: Pipeline | None = None
        self._diarization_load_error: str | None = None

        logger.info(
            "AudioEngine инициализирован. Профиль=%s, Модель=%s, Max Candidates=%d",
            self.quality_profile,
            self.current_model,
            len(settings.model_max_list),
        )

    def set_quality_profile(self, profile: str) -> bool:
        """Переключает профиль качества (balanced или max)."""
        clean_profile = profile.strip().lower()
        if clean_profile not in {"balanced", "max"}:
            clean_profile = "balanced"

        new_model = settings.MODEL_BALANCED if clean_profile == "balanced" else settings.model_max_list[0]

        if clean_profile == self.quality_profile and new_model == self.current_model:
            return False

        logger.info("Смена профиля STT: %s -> %s (модель: %s)", self.quality_profile, clean_profile, new_model)
        self.quality_profile = clean_profile
        self.current_model = new_model
        return True

    def normalize_audio(self, audio_path: str) -> bool | str:
        """Нормализует громкость аудиофайла до целевого уровня (примерно -20 dBFS).

        Совместимость:
        - для отсутствующего файла возвращает исходный путь (legacy-контракт тестов),
        - для успешной нормализации возвращает True.
        """
        if not os.path.exists(audio_path):
            logger.error("Файл не найден для нормализации: %s", audio_path)
            return audio_path
        try:
            data, samplerate = sf.read(audio_path)
            if len(data.shape) > 1:
                data = data.mean(axis=1) # Стерео в моно
            
            rms = np.sqrt(np.mean(data**2))
            if rms < 1e-6:
                return True # Тишина
            
            gain = 0.1 / rms
            normalized_data = np.clip(data * gain, -1.0, 1.0)
            sf.write(audio_path, normalized_data, samplerate)
            return True
        except Exception as e:
            logger.error("Ошибка при нормализации аудио %s: %s", audio_path, e)
            return False

    # --- Legacy-совместимость для тестов и старых вызовов ---
    @staticmethod
    def _normalize_phrase(text: str) -> str:
        """Совместимый алиас нормализации фраз."""
        return TextUtils.normalize_phrase(text)

    @staticmethod
    def _same_short_phrase(a: str, b: str, max_words: int = 8) -> bool:
        """Совместимый алиас сравнения коротких фраз."""
        return TextUtils.same_short_phrase(a, b, max_words=max_words)

    @staticmethod
    def _cleanup_soft(text: str) -> str:
        """Совместимый алиас мягкой очистки хвостов.

        В legacy-тестах ожидается, что при снятии дублированного хвоста
        у фразы сохранится финальная точка.
        """
        cleaned = TextUtils._cleanup_soft(text)
        if cleaned and not cleaned.endswith((".", "!", "?", "…")) and text.strip().endswith((".", "!", "?", "…")):
            return f"{cleaned}."
        return cleaned

    @staticmethod
    def _cleanup_transcript(text: str, cleanup_profile: str = "soft") -> str:
        """Совместимый алиас общей очистки транскрипта."""
        return TextUtils.cleanup_transcript(text, profile=cleanup_profile)

    # Допустимые языковые коды для lang_hint (ISO 639-1).
    # None означает автоопределение whisper'ом по первым 30с аудио.
    _VALID_LANG_HINTS: frozenset[str] = frozenset({"ru", "es", "en", "auto"})

    @staticmethod
    def _resolve_language(lang_hint: str | None) -> str | None:
        """Преобразует lang_hint в параметр whisper language.

        - None / "auto" → None (whisper сам определяет язык)
        - "ru" / "es" / "en" → передаётся напрямую
        - неизвестное значение → None с предупреждением
        """
        if lang_hint is None or lang_hint.strip().lower() in ("auto", ""):
            return None
        clean = lang_hint.strip().lower()
        if clean in AudioEngine._VALID_LANG_HINTS - {"auto"}:
            return clean
        logger.warning("Неизвестный lang_hint=%r, используем авто-определение", lang_hint)
        return None

    def transcribe(
        self,
        audio_data: Any,
        cleanup_profile: str = "soft",
        is_preview: bool = False,
        domain: str = "casual",
        extra_vocabulary: list[str] | None = None,
        lang_hint: str | None = None,
    ) -> dict[str, Any]:
        """Основной метод распознавания речи. Поддерживает динамические промпты и доменные подсказки.

        Args:
            lang_hint: ISO 639-1 код языка ("ru", "es", "en") или None/"auto" для
                       автоопределения whisper'ом. По умолчанию берётся из конфига (settings.TRANSCRIBE_LANGUAGE).
        """
        start_time = time.time()
        resolved_lang = self._resolve_language(lang_hint) if lang_hint is not None else settings.TRANSCRIBE_LANGUAGE

        # 1. Формирование динамического промпта.
        # Preview path идёт с пустым prompt'ом: короткие аудиобуферы (<3s)
        # провоцируют whisper на "leakage" initial_prompt'а в output как
        # артефакта. Финальный stop_recording по-прежнему использует полный
        # TRANSCRIBE_PROMPT для пунктуации/брендов/имён. Defense-in-depth:
        # _postprocess_preview_text в service.py срезает известные фрагменты
        # промпта как safety net.
        if is_preview:
            dynamic_prompt = ""
        else:
            domain_desc = self.DOMAIN_PROMPTS.get(domain, self.DOMAIN_PROMPTS["casual"])
            dynamic_prompt = f"{settings.TRANSCRIBE_PROMPT} Тематика: {domain_desc}"
            if extra_vocabulary:
                dynamic_prompt += f" Ключевые слова: {', '.join(extra_vocabulary)}"

        # 2. Проверка лимитов для файлов
        if isinstance(audio_data, (str, Path)) and os.path.exists(audio_data):
            size_mb = os.path.getsize(audio_data) / (1024 * 1024)
            if size_mb > settings.MAX_AUDIO_MB:
                raise ValueError(f"Файл слишком большой: {size_mb:.1f}MB > {settings.MAX_AUDIO_MB}MB")

        try:
            # 3. Вызов распознавания с механизмом деградации (fallback)
            result = self._transcribe_with_fallback(audio_data, prompt=dynamic_prompt, language=resolved_lang)
            raw_text = str(result.get("text", "")).strip()
            segments = result.get("segments", [])
            diarization = self._maybe_run_diarization(audio_data, segments, is_preview=is_preview)

            # 4. Очистка результата через утилиты
            text = TextUtils.cleanup_transcript(raw_text, profile=cleanup_profile)

            # 5. Расчет метрик уверенности
            confidence = 0.0
            if segments:
                confidence = float(np.mean([np.exp(s.get("avg_logprob", -1.0)) for s in segments]))

            duration = time.time() - start_time
            logger.info("STT готово: %.2fs, уверенность: %.2f, язык: %s", duration, confidence, resolved_lang or "auto")

            return {
                "text": text,
                "raw_text": raw_text,
                "confidence": round(confidence, 3),
                "duration_ms": int(duration * 1000),
                "engine": result.get("engine", "mlx-whisper"),
                "model": result.get("model_used", self.current_model),
                "language": result.get("language", resolved_lang),
                "segments": segments if not is_preview else [],
                "diarization": diarization,
            }
        except Exception as exc:
            logger.exception("Критическая ошибка распознавания")
            return {"text": "", "error": str(exc), "status": "error"}

    def _transcribe_with_fallback(self, audio_data: Any, prompt: str, language: str | None = None) -> dict[str, Any]:
        """Пробует несколько моделей при возникновении ошибок (например, нехватка VRAM).

        Перед загрузкой тяжёлых моделей (не balanced) проверяет свободную память
        через vm_stat, чтобы macOS Jetsam не убил процесс (SIGKILL).
        """
        candidates = [self.current_model]
        if self.quality_profile == "max":
            candidates = list(dict.fromkeys(settings.model_max_list))

        balanced_model = settings.MODEL_BALANCED

        for model_name in candidates:
            if model_name in self._unavailable_models:
                continue

            # Проверка памяти перед тяжёлыми моделями (не balanced)
            if model_name != balanced_model:
                avail_gb = _get_available_memory_gb()
                if 0 < avail_gb < _HEAVY_MODEL_MIN_FREE_GB:
                    logger.warning(
                        "Пропускаю тяжёлую модель %s: доступно %.1f GB, нужно >= %.1f GB",
                        model_name, avail_gb, _HEAVY_MODEL_MIN_FREE_GB,
                    )
                    continue

            try:
                timeout = settings.TRANSCRIBE_TIMEOUT_SEC
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self._transcribe_model, audio_data, model_name, prompt, language)
                    result = future.result(timeout=timeout)
                result["model_used"] = model_name
                return result
            except concurrent.futures.TimeoutError:
                logger.error(
                    "Таймаут %ds при транскрибации моделью %s — пропускаю",
                    settings.TRANSCRIBE_TIMEOUT_SEC, model_name,
                )
                self._unavailable_models.add(model_name)
            except MemoryError:
                logger.error("MemoryError при загрузке модели %s — помечаю как недоступную", model_name)
                self._unavailable_models.add(model_name)
            except OSError as e:
                # errno 12 = Cannot allocate memory — ядро отказало в mmap
                if e.errno == 12 or "Cannot allocate memory" in str(e):
                    logger.error("OOM (OSError) при модели %s: %s — помечаю как недоступную", model_name, e)
                    self._unavailable_models.add(model_name)
                else:
                    logger.warning("Модель %s не сработала (OSError): %s", model_name, e)
                    self._unavailable_models.add(model_name)
            except Exception as e:
                logger.warning("Модель %s не сработала: %s", model_name, e)
                self._unavailable_models.add(model_name)

        # Если локально ничего не вышло — пробуем облако (если разрешено)
        if settings.NETWORK_MODE != "offline_strict":
            logger.info("Локальные модели недоступны, переключаюсь на Remote STT...")
            return self._transcribe_remote(audio_data, prompt)

        raise RuntimeError("Все доступные STT-движки вышли из строя.")

    def _transcribe_model(self, audio_data: Any, model_name: str, prompt: str, language: str | None = None) -> dict[str, Any]:
        """Низкоуровневый вызов MLX Whisper с обработкой несовместимых аргументов."""
        effective_language = language if language is not None else settings.TRANSCRIBE_LANGUAGE
        base_params = {
            "path_or_hf_repo": model_name,
            "initial_prompt": prompt,
            "language": effective_language,
            "temperature": 0.0,
            "verbose": False,
        }

        # Варианты аргументов для разных версий библиотеки
        variants = [
            {**base_params, "condition_on_previous_text": False, "no_speech_threshold": 0.6},
            {**base_params, "condition_on_previous_text": False},
            base_params,
        ]

        last_err = None
        for params in variants:
            try:
                return mlx_whisper.transcribe(audio_data, **params)
            except TypeError as e:
                last_err = e
        raise last_err or RuntimeError("Ошибка вызова mlx_whisper.transcribe")

    def _maybe_run_diarization(
        self,
        audio_data: Any,
        whisper_segments: list[dict[str, Any]],
        *,
        is_preview: bool,
    ) -> dict[str, Any]:
        """Пытается проставить спикеров для файловой транскрибации.

        Решение сделано мягким: любая ошибка diarization логируется и попадает в
        результат как служебное поле, но не ломает базовую STT-транскрибацию.
        """
        base_result: dict[str, Any] = {
            "enabled": False,
            "speaker_segments": [],
            "annotated_segments": [],
            "speaker_turns": [],
        }
        if is_preview or not settings.DIARIZATION_ENABLED:
            return base_result

        audio_path = self._resolve_audio_path(audio_data)
        if audio_path is None:
            return base_result

        try:
            speaker_segments = self._run_diarization(audio_path)
            annotated_segments = self._annotate_segments_with_speakers(whisper_segments, speaker_segments)
            speaker_turns = self._merge_speaker_turns(annotated_segments)
            return {
                "enabled": True,
                "speaker_segments": speaker_segments,
                "annotated_segments": annotated_segments,
                "speaker_turns": speaker_turns,
            }
        except Exception as exc:
            logger.warning("Diarization недоступен для %s: %s", audio_path, exc)
            return {**base_result, "error": str(exc)}

    def _resolve_audio_path(self, audio_data: Any) -> str | None:
        """Возвращает путь к файлу, если diarization можно запускать по месту."""
        if isinstance(audio_data, Path):
            return str(audio_data.expanduser().resolve())
        if isinstance(audio_data, str):
            candidate = Path(audio_data).expanduser().resolve()
            if candidate.exists():
                return str(candidate)
        return None

    def _load_diarization_pipeline(self) -> Pipeline:
        """Ленивая загрузка pyannote pipeline с токеном Hugging Face."""
        if self._diarization_pipeline is not None:
            return self._diarization_pipeline
        if self._diarization_load_error:
            raise RuntimeError(self._diarization_load_error)

        hf_token = os.environ.get("HF_TOKEN") or settings.HF_TOKEN
        if not hf_token:
            self._diarization_load_error = "Не задан HF_TOKEN для pyannote diarization."
            raise RuntimeError(self._diarization_load_error)

        # Используем ленивую инициализацию, чтобы не тянуть модель в realtime-пути.
        self._diarization_pipeline = Pipeline.from_pretrained(settings.DIARIZATION_MODEL, token=hf_token)
        diarization_device = self._resolve_diarization_device()
        self._diarization_pipeline.to(diarization_device)
        logger.info("Diarization pipeline загружен на устройство %s", diarization_device)
        return self._diarization_pipeline

    @staticmethod
    def _resolve_diarization_device() -> torch.device:
        """Выбирает лучшее доступное устройство для pyannote."""
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")

    def _run_diarization(self, audio_path: str) -> list[dict[str, Any]]:
        """Запускает pyannote diarization и нормализует результат в словари."""
        pipeline = self._load_diarization_pipeline()
        prepared_audio_path, should_cleanup = self._prepare_audio_for_diarization(audio_path)
        try:
            diarization = pipeline(prepared_audio_path)
        except Exception as e:
            # --- Krab's Black Box ---
            import traceback
            error_log_path = "/tmp/krab_ear_diarization_error.log"
            logging.error(f"FATAL: Unhandled exception in diarization pipeline. Writing details to {error_log_path}")
            with open(error_log_path, "w") as f:
                f.write(f"A critical error occurred in the pyannote.audio pipeline block.\\n")
                f.write(f"Exception Type: {type(e).__name__}\\n")
                f.write(f"Exception Args: {e}\\n\\n")
                f.write("--- Traceback ---\\n")
                traceback.print_exc(file=f)
            # Re-raise to let the outer handler log and continue
            raise e
        finally:
            if should_cleanup:
                Path(prepared_audio_path).unlink(missing_ok=True)
        if hasattr(diarization, "speaker_diarization"):
            diarization = diarization.speaker_diarization
        speaker_segments: list[dict[str, Any]] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            speaker_segments.append(
                {
                    "start": round(float(turn.start), 3),
                    "end": round(float(turn.end), 3),
                    "speaker": str(speaker),
                }
            )
        return speaker_segments

    def _prepare_audio_for_diarization(self, audio_path: str) -> tuple[str, bool]:
        """Подготавливает совместимый WAV для pyannote.

        Для `.m4a` и других контейнеров прогоняем ffmpeg в mono/16k WAV, потому
        что torchcodec/pyannote иногда получают нестабильное число сэмплов.
        """
        source_path = Path(audio_path)
        if source_path.suffix.lower() == ".wav":
            return str(source_path), False

        with tempfile.NamedTemporaryFile(prefix="krab_ear_diarization_", suffix=".wav", delete=False) as handle:
            temp_path = Path(handle.name)

        cmd = [
            "/opt/homebrew/bin/ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(temp_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg не смог подготовить аудио для diarization: {completed.stderr.strip()}")
        return str(temp_path), True

    def _annotate_segments_with_speakers(
        self,
        whisper_segments: list[dict[str, Any]],
        speaker_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Назначает каждому whisper-сегменту спикера по максимальному overlap."""
        annotated: list[dict[str, Any]] = []
        for segment in whisper_segments:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            best_speaker = "SPEAKER_UNKNOWN"
            best_overlap = -1.0

            for speaker_segment in speaker_segments:
                overlap = self._segment_overlap(
                    start,
                    end,
                    float(speaker_segment.get("start", 0.0)),
                    float(speaker_segment.get("end", 0.0)),
                )
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = str(speaker_segment.get("speaker", "SPEAKER_UNKNOWN"))

            annotated.append({**segment, "speaker": best_speaker})
        return annotated

    @staticmethod
    def _segment_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
        """Вычисляет длительность пересечения двух сегментов."""
        return max(0.0, min(end_a, end_b) - max(start_a, start_b))

    def _merge_speaker_turns(self, annotated_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Склеивает соседние сегменты одного спикера в более читаемые реплики."""
        turns: list[dict[str, Any]] = []
        for segment in annotated_segments:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            speaker = str(segment.get("speaker", "SPEAKER_UNKNOWN"))
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))

            if turns and turns[-1]["speaker"] == speaker:
                turns[-1]["end"] = end
                turns[-1]["text"] = f"{turns[-1]['text']} {text}".strip()
                continue

            turns.append(
                {
                    "speaker": speaker,
                    "start": start,
                    "end": end,
                    "text": text,
                }
            )
        return turns

    def _transcribe_remote(self, audio_data: Any, prompt: str) -> dict[str, Any]:
        """Обращение к внешнему OpenAI-совместимому API."""
        try:
            # Упрощенная логика: предполагаем, что audio_data - путь к файлу
            with open(audio_data, "rb") as f:
                resp = requests.post(
                    settings.STT_GATEWAY_URL,
                    headers={"Authorization": f"Bearer token_here"}, # В реальности использовать правильный ключ
                    files={"file": (os.path.basename(audio_data), f, "audio/wav")},
                    data={"model": settings.STT_MODEL, "prompt": prompt},
                    timeout=60
                )
                resp.raise_for_status()
                data = resp.json()
                return {"text": data.get("text", ""), "engine": "remote"}
        except Exception as e:
            logger.error("Ошибка Remote STT: %s", e)
            raise

    def speak(self, text: str, rate: int = 185) -> None:
        """Озвучка текста через macOS `say`."""
        if not text.strip(): return
        cmd = ["say", "-r", str(rate)]
        if settings.SAY_VOICE:
            cmd.extend(["-v", settings.SAY_VOICE])
        cmd.append(text)
        subprocess.run(cmd, check=False)
