"""Ядро Krab Ear: локальная транскрибация и опциональные сетевые функции.

Файл связан с UI (`ui/window.py`), который использует `AudioEngine` для:
1) распознавания речи локально через MLX Whisper;
2) опционального запроса к LLM-шлюзу;
3) системной озвучки результата.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Any

import mlx_whisper
import requests
from dotenv import load_dotenv

logger = logging.getLogger("KrabEar.Engine")


class AudioEngine:
    """Сервисный слой для STT/TTS и опционального запроса к внешнему "мозгу"."""

    def __init__(self) -> None:
        load_dotenv()

        # Профили качества: balanced по умолчанию и более тяжёлые кандидаты для max.
        self.model_balanced = "mlx-community/whisper-large-v3-turbo"
        self.model_max_candidates = self._resolve_max_candidates()
        self.current_model = self.model_balanced
        self.quality_profile = "balanced"
        self._unavailable_models: set[str] = set()

        # Сетевой режим по умолчанию офлайн: вызовы ask_brain не активируются без явной настройки.
        self.network_mode = os.getenv("KRAB_EAR_NETWORK_MODE", "offline_default")

        # Шлюз оставлен как опциональная интеграция.
        self.gateway_url = os.getenv(
            "KRAB_EAR_GATEWAY_URL",
            "http://127.0.0.1:18789/v1/chat/completions",
        )
        self.api_key = os.getenv("OPENCLAW_GATEWAY_TOKEN", "sk-nexus-bridge")
        self.ai_model = os.getenv("KRAB_EAR_AI_MODEL", "google/gemini-2.0-flash")
        self.say_voice = os.getenv("KRAB_EAR_SAY_VOICE", "")

        # Промпт задаёт желаемый стиль: грамотная пунктуация и заглавные буквы.
        self.transcribe_prompt = os.getenv(
            "KRAB_EAR_TRANSCRIBE_PROMPT",
            (
                "Ты транскрибируешь русскую речь. "
                "Сохраняй смысл, ставь корректную пунктуацию и заглавные буквы."
            ),
        )
        self.transcribe_language = os.getenv("KRAB_EAR_TRANSCRIBE_LANGUAGE", "ru")

        logger.info(
            "Инициализация AudioEngine завершена. Профиль=%s, модель=%s, max_candidates=%s",
            self.quality_profile,
            self.current_model,
            ",".join(self.model_max_candidates),
        )

    def _resolve_max_candidates(self) -> list[str]:
        """Формирует список кандидатов max-модели из env, с безопасным fallback."""
        raw = os.getenv("KRAB_EAR_MODEL_MAX_CANDIDATES", "").strip()
        if not raw:
            # По умолчанию не ходим в сеть за несуществующими репозиториями.
            return [self.model_balanced]

        candidates = [item.strip() for item in raw.split(",") if item.strip()]
        if not candidates:
            return [self.model_balanced]
        if self.model_balanced not in candidates:
            candidates.append(self.model_balanced)
        return candidates

    def set_quality_profile(self, profile: str) -> bool:
        """Переключает профиль качества (`balanced`/`max`)."""
        clean_profile = profile.strip().lower()
        if clean_profile not in {"balanced", "max"}:
            clean_profile = "balanced"

        if clean_profile == "balanced":
            new_model = self.model_balanced
        else:
            # Для max пробуем кандидатов по порядку и оставляем первый рабочий.
            # На недоступной модели возможен runtime fallback в transcribe().
            new_model = self.model_max_candidates[0]

        if clean_profile == self.quality_profile and new_model == self.current_model:
            return False

        logger.info(
            "Смена профиля STT: %s/%s -> %s/%s",
            self.quality_profile,
            self.current_model,
            clean_profile,
            new_model,
        )
        self.quality_profile = clean_profile
        self.current_model = new_model
        return True

    def set_model_quality(self, use_max_quality: bool) -> bool:
        """Совместимость со старым API переключения качества."""
        return self.set_quality_profile("max" if use_max_quality else "balanced")

    def transcribe(
        self,
        audio_data: Any,
        cleanup_profile: str = "soft",
        is_preview: bool = False,
    ) -> str:
        """Распознаёт аудио (numpy-массив или путь к файлу) и возвращает текст."""
        start = time.time()
        try:
            result = self._transcribe_with_fallback(audio_data)
            raw_text = str(result.get("text", "")).strip()
            text = self._cleanup_transcript(raw_text, cleanup_profile=cleanup_profile)
            duration = time.time() - start
            logger.info(
                "Транскрибация завершена за %.2fs, длина=%d (raw=%d), профиль=%s, cleanup=%s, preview=%s, модель=%s",
                duration,
                len(text),
                len(raw_text),
                self.quality_profile,
                cleanup_profile,
                is_preview,
                self.current_model,
            )
            return text
        except Exception as exc:
            logger.exception("Ошибка транскрибации: %s", exc)
            return ""

    def _transcribe_with_fallback(self, audio_data: Any) -> dict[str, Any]:
        """Выполняет STT с fallback на turbo при недоступности heavy-модели."""
        # Порядок попыток зависит от выбранного профиля.
        if self.quality_profile == "max":
            candidates = list(dict.fromkeys(self.model_max_candidates))
        else:
            candidates = [self.model_balanced]

        last_error: Exception | None = None
        for model_name in candidates:
            if model_name in self._unavailable_models:
                continue
            try:
                result = self._transcribe_model(audio_data, model_name)
                self.current_model = model_name
                return result
            except Exception as exc:
                last_error = exc
                logger.warning("STT модель %s недоступна: %s", model_name, exc)
                self._unavailable_models.add(model_name)

        # Жёсткий fallback на balanced, если профиль max не сработал.
        if self.model_balanced not in candidates:
            result = self._transcribe_model(audio_data, self.model_balanced)
            self.current_model = self.model_balanced
            self.quality_profile = "balanced"
            self._unavailable_models.discard(self.model_balanced)
            return result

        assert last_error is not None
        raise last_error

    def _transcribe_model(self, audio_data: Any, model_name: str) -> dict[str, Any]:
        """Вызывает mlx_whisper с набором анти-галлюцинационных опций и безопасным fallback."""
        base_kwargs = {
            "path_or_hf_repo": model_name,
            "initial_prompt": self.transcribe_prompt,
            "language": self.transcribe_language,
            "temperature": 0.0,
            "verbose": False,
        }

        # Не все версии mlx_whisper поддерживают расширенные параметры:
        # пробуем от строгого набора к минимально совместимому.
        kwargs_variants = [
            {
                **base_kwargs,
                "condition_on_previous_text": False,
                "compression_ratio_threshold": 2.2,
                "logprob_threshold": -1.0,
                "no_speech_threshold": 0.6,
            },
            {
                **base_kwargs,
                "condition_on_previous_text": False,
            },
            base_kwargs,
        ]

        last_type_error: Exception | None = None
        for kwargs in kwargs_variants:
            try:
                return mlx_whisper.transcribe(audio_data, **kwargs)
            except TypeError as exc:
                last_type_error = exc
                continue

        assert last_type_error is not None
        raise last_type_error

    @staticmethod
    def _cleanup_transcript(text: str, cleanup_profile: str = "soft") -> str:
        """Убирает типичные хвостовые артефакты, не трогая основной смысл."""
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return clean

        profile = AudioEngine._normalize_cleanup_profile(cleanup_profile)
        clean = AudioEngine._cleanup_soft(clean)
        if profile == "strict":
            clean = AudioEngine._cleanup_strict(clean)
        return clean.strip()

    @staticmethod
    def _cleanup_soft(clean: str) -> str:
        """Мягкий профиль очистки: удаляет только очевидные повторы в конце."""
        # Частый артефакт: повтор короткой финальной фразы.
        segments = [part.strip() for part in re.split(r"[.!?…]+", clean) if part.strip()]
        if len(segments) >= 2:
            last = segments[-1]
            prev = segments[-2]
            if AudioEngine._same_short_phrase(last, prev):
                tail = clean.rfind(last)
                if tail > 0:
                    clean = clean[:tail].rstrip(" .,!?:;")

        # Второй тип артефакта: короткий хвост из 1-3 слов, повторённый дважды.
        match = re.search(r"(.+?)\s+([А-Яа-яA-Za-z0-9'-]+(?:\s+[А-Яа-яA-Za-z0-9'-]+){0,2})\s+\2[.!?…]*$", clean)
        if match:
            clean = match.group(1).rstrip(" .,!?:;")

        # Третий тип артефакта: три одинаковых хвостовых куска подряд.
        words = clean.split()
        for size in (5, 4, 3, 2, 1):
            if len(words) < size * 3 + 2:
                continue
            part_a = " ".join(words[-(size * 3):-(size * 2)])
            part_b = " ".join(words[-(size * 2):-size])
            part_c = " ".join(words[-size:])
            norm_a = AudioEngine._normalize_phrase(part_a)
            norm_b = AudioEngine._normalize_phrase(part_b)
            norm_c = AudioEngine._normalize_phrase(part_c)
            if norm_a and norm_a == norm_b == norm_c:
                clean = " ".join(words[:-size]).rstrip(" .,!?:;")
                break

        return clean.strip()

    @staticmethod
    def _cleanup_strict(clean: str) -> str:
        """Строгий профиль: дополнительно режет повторяющийся хвост из более ранней фразы."""
        segments = [part.strip() for part in re.split(r"[.!?…]+", clean) if part.strip()]
        if len(segments) >= 3:
            last = segments[-1]
            norm_last = AudioEngine._normalize_phrase(last)
            if norm_last:
                for candidate in segments[:-1]:
                    norm_candidate = AudioEngine._normalize_phrase(candidate)
                    is_tail_repeat = (
                        norm_candidate == norm_last
                        or norm_candidate.endswith(norm_last)
                        or norm_last.endswith(norm_candidate)
                    )
                    if is_tail_repeat and 2 <= len(norm_last.split()) <= 12:
                        tail = clean.rfind(last)
                        if tail > 0:
                            clean = clean[:tail].rstrip(" .,!?:;")
                        break

        words = clean.split()
        for size in (4, 3, 2, 1):
            if len(words) < size * 2 + 4:
                continue
            left = " ".join(words[-(size * 2):-size])
            right = " ".join(words[-size:])
            if AudioEngine._normalize_phrase(left) and AudioEngine._normalize_phrase(left) == AudioEngine._normalize_phrase(right):
                clean = " ".join(words[:-size]).rstrip(" .,!?:;")
                break

        clean = AudioEngine._strip_known_hallucination_tail(clean)
        return clean.strip()

    @staticmethod
    def _strip_known_hallucination_tail(clean: str) -> str:
        """Удаляет типичные шаблоны галлюцинаций в самом конце фразы."""
        lowered = clean.lower().strip()
        if len(lowered.split()) < 10:
            return clean

        patterns = [
            r"(?:спасибо за просмотр|спасибо за внимание)[.!?…]*$",
            r"(?:субтитры сделал [^.!?…]{1,40})[.!?…]*$",
            r"(?:подписывайтесь на канал)[.!?…]*$",
            r"(?:до новых встреч)[.!?…]*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if not match:
                continue
            cut_pos = match.start()
            if cut_pos <= 0:
                continue
            return clean[:cut_pos].rstrip(" .,!?:;")
        return clean

    @staticmethod
    def _normalize_cleanup_profile(value: str) -> str:
        """Нормализует профиль очистки текста."""
        clean = value.strip().lower()
        if clean not in {"soft", "strict"}:
            return "soft"
        return clean

    @staticmethod
    def _normalize_phrase(text: str) -> str:
        """Нормализует фразу для безопасного сравнения повторов."""
        return re.sub(r"[^\w\s-]+", "", text.lower()).strip()

    @staticmethod
    def _same_short_phrase(a: str, b: str) -> bool:
        """Сравнение коротких фраз без регистра и пунктуации."""
        na = AudioEngine._normalize_phrase(a)
        nb = AudioEngine._normalize_phrase(b)
        if not na or not nb:
            return False
        return na == nb and len(na.split()) <= 8

    def ask_brain(self, text: str) -> str:
        """Отправляет текст во внешний LLM-шлюз, если он доступен."""
        if self.network_mode == "offline_strict":
            return ""

        clean_text = text.strip()
        if not clean_text:
            return ""

        payload = {
            "model": self.ai_model,
            "messages": [
                {
                    "role": "system",
                    "content": "Ты краткий ассистент. Ответь на русском языке одной короткой фразой.",
                },
                {"role": "user", "content": clean_text},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            response = requests.post(
                self.gateway_url,
                json=payload,
                headers=headers,
                timeout=20,
            )
            if response.status_code != 200:
                logger.warning(
                    "LLM-шлюз вернул %s: %s",
                    response.status_code,
                    response.text[:200],
                )
                return ""

            data = response.json()
            return self._extract_assistant_text(data)
        except Exception as exc:
            logger.warning("LLM-шлюз недоступен: %s", exc)
            return ""

    @staticmethod
    def _extract_assistant_text(payload: dict[str, Any]) -> str:
        """Аккуратно извлекает текст ассистента из OpenAI-совместимого ответа."""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content", "")
        return str(content).strip()

    def speak(self, text: str, rate: int = 185) -> None:
        """Озвучивает текст через системный `say` на macOS."""
        clean_text = text.strip()
        if not clean_text:
            return

        cmd = ["say", "-r", str(rate)]
        if self.say_voice:
            cmd.extend(["-v", self.say_voice])
        cmd.append(clean_text)

        try:
            subprocess.run(cmd, check=False)
        except Exception as exc:
            logger.warning("Ошибка TTS: %s", exc)
