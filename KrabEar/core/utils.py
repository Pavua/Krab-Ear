"""Утилиты Krab Ear: обработка текста и аудио.

Включает в себя логику очистки транскрибатов от галлюцинаций и повторов.
"""

import re
import logging

logger = logging.getLogger("KrabEar.Utils")

class TextUtils:
    """Статичный набор инструментов для нормализации и очистки текста."""

    @staticmethod
    def normalize_phrase(text: str) -> str:
        """Нормализует фразу для безопасного сравнения (нижний регистр, только буквы/цифры)."""
        return re.sub(r"[^\w\s-]+", "", text.lower()).strip()

    @staticmethod
    def same_short_phrase(a: str, b: str, max_words: int = 8) -> bool:
        """Сравнивает, являются ли две короткие фразы идентичными без учета пунктуации."""
        na = TextUtils.normalize_phrase(a)
        nb = TextUtils.normalize_phrase(b)
        if not na or not nb:
            return False
        return na == nb and len(na.split()) < max_words

    @staticmethod
    def cleanup_transcript(text: str, profile: str = "soft") -> str:
        """Основной метод очистки транскрипции от артефактов Whispera."""
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return clean

        # Мягкая очистка (всегда включена)
        clean = TextUtils._cleanup_soft(clean)
        # Базовая фильтрация известных артефактов нужна и в soft-профиле.
        clean = TextUtils._strip_hallucinations(clean)
        
        # Строгая очистка
        if profile.lower() == "strict":
            clean = TextUtils._cleanup_strict(clean)
            
        return clean.strip()

    @staticmethod
    def _cleanup_soft(clean: str) -> str:
        """Удаляет явные непосредственные повторы фраз в конце текста."""
        # 1. Повтор финальной фразы
        segments = [part.strip() for part in re.split(r"[.!?…]+", clean) if part.strip()]
        if len(segments) >= 2:
            last = segments[-1]
            prev = segments[-2]
            if TextUtils.same_short_phrase(last, prev):
                tail = clean.rfind(last)
                if tail > 0:
                    clean = clean[:tail].rstrip(" .,!?:;")

        # 2. Повтор 1-3 слов дважды в конце
        match = re.search(r"(.+?)\s+([А-Яа-яA-Za-z0-9'-]+(?:\s+[А-Яа-яA-Za-z0-9'-]+){0,2})\s+\2[.!?…]*$", clean)
        if match:
            clean = match.group(1).rstrip(" .,!?:;")

        return clean.strip()

    @staticmethod
    def _cleanup_strict(clean: str) -> str:
        """Более агрессивное удаление повторов и известных галлюцинаций."""
        # 0. Убираем повтор финального предложения, если оно уже встречалось ранее.
        segments = [part.strip() for part in re.split(r"[.!?…]+", clean) if part.strip()]
        if len(segments) >= 2:
            last = segments[-1]
            normalized_last = TextUtils.normalize_phrase(last)
            for previous in reversed(segments[:-1]):
                normalized_prev = TextUtils.normalize_phrase(previous)
                is_suffix_repeat = bool(
                    normalized_last
                    and normalized_prev
                    and (
                        normalized_prev == normalized_last
                        or normalized_prev.endswith(f" {normalized_last}")
                    )
                )
                if TextUtils.same_short_phrase(last, previous) or is_suffix_repeat:
                    clean = re.sub(rf"{re.escape(last)}[.!?…]*\s*$", "", clean, flags=re.IGNORECASE).rstrip(" .,!?:;")
                    break

        # 3. Три одинаковых куска подряд (заикание модели)
        words = clean.split()
        for size in (5, 4, 3, 2, 1):
            if len(words) < size * 3 + 2:
                continue
            part_a = " ".join(words[-(size * 3):-(size * 2)])
            part_b = " ".join(words[-(size * 2):-size])
            part_c = " ".join(words[-size:])
            if TextUtils.normalize_phrase(part_a) == TextUtils.normalize_phrase(part_b) == TextUtils.normalize_phrase(part_c):
                clean = " ".join(words[:-size]).rstrip(" .,!?:;")
                break

        # 4. Удаление известных фраз-галлюцинаций (YouTube-стайл)
        clean = TextUtils._strip_hallucinations(clean)
        return clean.strip()

    @staticmethod
    def _strip_hallucinations(clean: str) -> str:
        """Удаляет типичные шаблоны галлюцинаций Whispera."""
        lowered = clean.lower()
        patterns = [
            r"(?:спасибо за просмотр|спасибо за внимание)[.!?…]*$",
            r"(?:субтитры сделал [^.!?…]{1,40})[.!?…]*$",
            r"(?:подписывайтесь на канал)[.!?…]*$",
            r"(?:до новых встреч)[.!?…]*$",
            r"(?:продолжение следует)[.!?…]*$",
            r"(?:to be continued)[.!?…]*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if not match:
                continue
            if match.start() <= 0:
                return ""
            return clean[:match.start()].rstrip(" .,!?:;")
        return clean
