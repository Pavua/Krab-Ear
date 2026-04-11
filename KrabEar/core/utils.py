"""Утилиты Krab Ear: обработка текста и аудио.

Включает в себя логику очистки транскрибатов от галлюцинаций и повторов.
"""

import re
import logging

logger = logging.getLogger("KrabEar.Utils")

# ── Precompiled regex patterns ──────────────────────────────────────────

_WHITESPACE_RE = re.compile(r"\s+")
_NORMALIZE_RE = re.compile(r"[^\w\s-]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+")
_WORD_REPEAT_RE = re.compile(
    r"(.+?)\s+([А-Яа-яA-Za-z0-9'-]+(?:\s+[А-Яа-яA-Za-z0-9'-]+){0,2})\s+\2[.!?…]*$"
)

# Кириллические искажения имён собственных → каноническая латиница.
# Whisper на русской речи транскрибирует бренды в кириллицу; возвращаем их в латиницу
# детерминированно, независимо от того, сработал ли initial_prompt.
_BRAND_REPLACEMENTS_RAW: list[tuple[str, str]] = [
    # Порядок важен: более длинные/составные варианты идут раньше.
    (r"\bKrab\s*Voice\s*Gateway\b", "Krab Voice Gateway"),
    (r"\bКраб\s*Войс\s*Гейтвей\b", "Krab Voice Gateway"),
    (r"\bCrab\s*Ear\b", "Krab Ear"),
    (r"\bКраб\s*Ир\b", "Krab Ear"),
    (r"\bКрабИр\b", "Krab Ear"),
    # Ловим все падежи: Меркадона/-ы/-е/-у/-ой/-ной + удвоенное «нн».
    (r"\bМеркадонн?(?:а|ы|е|у|ой|ою)\b", "Mercadona"),
    (r"\bАнти[-\s]?Гравити\b", "Antigravity"),
    (r"\bAnti[-\s]?Gravity\b", "Antigravity"),
    (r"\bХаммер[-\s]?Спун\b", "Hammerspoon"),
    (r"\bHammer\s*Spoon\b", "Hammerspoon"),
    (r"\bОпен[-\s]?Клоу\b", "OpenClaw"),
    (r"\bПианот\b", "Pyannote"),
    (r"\bПайрофорк\b", "Pyrofork"),
    (r"\bПайрайт\b", "Pyright"),
    (r"\bПаблито\b", "Pablito"),
    (r"\bТелеграм\b", "Telegram"),
    (r"\bВиспер\b", "Whisper"),
    (r"\bКлод\b", "Claude"),
    (r"\bЭм\s*Эл\s*Икс\b", "MLX"),
    (r"\bФаст\s*АПИ\b", "FastAPI"),
    (r"\bГит[-\s]?Хаб\b", "GitHub"),
    (r"\bМак[-\s]?Бук\b", "MacBook"),
    # AI/ML инструменты
    (r"\bЧат\s*Джи\s*Пи\s*[Тт]\b", "ChatGPT"),
    (r"\bДжи\s*Пи\s*[Тт]\b", "GPT"),
    (r"\bОпен\s*[Ээ]й\s*[Аа]й\b", "OpenAI"),
    (r"\bМидж[оё]рни\b", "Midjourney"),
    (r"\bСтейбл\s*Диффь?южн\b", "Stable Diffusion"),
    (r"\bЛлама\b", "Llama"),
    (r"\bДжемини\b", "Gemini"),
    # Dev-инструменты
    (r"\bВи\s*Эс\s*Код\b", "VS Code"),
    (r"\bГит\b", "Git"),
    (r"\bНод\s*[Дд]жи\s*[Ээс]\b", "Node.js"),
    (r"\bРеакт\b", "React"),
    (r"\bДокер\b", "Docker"),
    (r"\bКубернетис\b", "Kubernetes"),
    (r"\bЛинукс\b", "Linux"),
    # Сервисы
    (r"\bАмазон\b", "Amazon"),
    (r"\bНетфликс\b", "Netflix"),
    (r"\bСпотифай\b", "Spotify"),
    (r"\bЮ\s*[Тт]юб\b", "YouTube"),
    (r"\bИнстаграм\b", "Instagram"),
    (r"\bВотс\s*[Аа]п\b", "WhatsApp"),
    # Испания (розничные сети)
    (r"\bКарр[еэ]фур\b", "Carrefour"),
    (r"\bЛидл\b", "Lidl"),
    (r"\bАльди\b", "Aldi"),
]
BRAND_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(pat, re.IGNORECASE), repl) for pat, repl in _BRAND_REPLACEMENTS_RAW
]

# Время "15.00" / "15 00" после цифр → "15:00" (только в диапазоне часов).
# Не трогаем числа с плавающей точкой: условие — час 0-23 и минуты 00-59.
TIME_NORMALIZE_RE = re.compile(r"\b([01]?\d|2[0-3])\s*[.:]\s*([0-5]\d)(?!\d)")

_HALLUCINATION_PATTERNS: list[re.Pattern] = [
    re.compile(pat) for pat in [
        r"(?:спасибо за просмотр|спасибо за внимание)[.!?…]*$",
        r"(?:субтитры сделал [^.!?…]{1,40})[.!?…]*$",
        r"(?:подписывайтесь на канал)[.!?…]*$",
        r"(?:до новых встреч)[.!?…]*$",
        r"(?:продолжение следует)[.!?…]*$",
        r"(?:to be continued)[.!?…]*$",
        r"(?:подписывайтесь на наш канал)[.!?…]*$",
        r"(?:ставьте лайки)[.!?…]*$",
        r"(?:смотрите в описании)[.!?…]*$",
        r"(?:поддержите канал)[.!?…]*$",
        r"(?:приятного просмотра)[.!?…]*$",
        r"(?:увидимся в следующем видео)[.!?…]*$",
        r"(?:всем пока)[.!?…]*$",
        r"(?:спасибо всем за внимание)[.!?…]*$",
        r"(?:\.\s+)?спасибо\.?\s*$",  # standalone trailing "Спасибо."
    ]
]


class TextUtils:
    """Статичный набор инструментов для нормализации и очистки текста."""

    @staticmethod
    def normalize_phrase(text: str) -> str:
        """Нормализует фразу для безопасного сравнения (нижний регистр, только буквы/цифры)."""
        return _NORMALIZE_RE.sub("", text.lower()).strip()

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
        clean = _WHITESPACE_RE.sub(" ", text).strip()
        if not clean:
            return clean

        # Мягкая очистка (всегда включена)
        clean = TextUtils._cleanup_soft(clean)
        # Базовая фильтрация известных артефактов нужна и в soft-профиле.
        clean = TextUtils._strip_hallucinations(clean)
        # Нормализация брендов/имён и времени — всегда, чтобы диктовка не требовала ручной правки.
        clean = TextUtils.normalize_entities(clean)
        
        # Строгая очистка
        if profile.lower() == "strict":
            clean = TextUtils._cleanup_strict(clean)
            
        return clean.strip()

    @staticmethod
    def _cleanup_soft(clean: str) -> str:
        """Удаляет явные непосредственные повторы фраз в конце текста."""
        # 1. Повтор финальной фразы
        segments = [part.strip() for part in _SENTENCE_SPLIT_RE.split(clean) if part.strip()]
        if len(segments) >= 2:
            last = segments[-1]
            prev = segments[-2]
            if TextUtils.same_short_phrase(last, prev):
                tail = clean.rfind(last)
                if tail > 0:
                    clean = clean[:tail].rstrip(" .,!?:;")

        # 2. Повтор 1-3 слов дважды в конце
        match = _WORD_REPEAT_RE.search(clean)
        if match:
            clean = match.group(1).rstrip(" .,!?:;")

        return clean.strip()

    @staticmethod
    def _cleanup_strict(clean: str) -> str:
        """Более агрессивное удаление повторов и известных галлюцинаций."""
        # 0. Убираем повтор финального предложения, если оно уже встречалось ранее.
        segments = [part.strip() for part in _SENTENCE_SPLIT_RE.split(clean) if part.strip()]
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
    def normalize_entities(text: str) -> str:
        """Канонизация брендов/имён (кириллица→латиница) и формата времени (ЧЧ:ММ).

        Применяется детерминированно поверх вывода Whisper, чтобы диктовка не
        требовала ручной правки «Меркадонна→Mercadona» и «15.00→15:00».
        """
        if not text:
            return text
        result = text
        for compiled_re, replacement in BRAND_REPLACEMENTS:
            result = compiled_re.sub(replacement, result)
        result = TIME_NORMALIZE_RE.sub(r"\1:\2", result)
        return result

    @staticmethod
    def _strip_hallucinations(clean: str) -> str:
        """Удаляет типичные шаблоны галлюцинаций Whispera."""
        lowered = clean.lower()
        for compiled_re in _HALLUCINATION_PATTERNS:
            match = compiled_re.search(lowered)
            if not match:
                continue
            if match.start() <= 0:
                return ""
            return clean[:match.start()].rstrip(" .,!?:;")
        return clean
