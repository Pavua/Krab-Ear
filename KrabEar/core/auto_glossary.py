"""auto_glossary.py — автоматическое построение глоссария из истории транскрибаций.

Извлекает часто встречающиеся имена собственные и технические термины
из истории за последние N дней и добавляет их в initial_prompt при следующих
транскрибациях. Формирует петлю обратной связи:
  история → глоссарий → лучшие транскрибации → богатейшая история.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, List, Optional

from core.term_extractor import TermExtractor, _is_stop_word

logger = logging.getLogger("KrabEar.Core.AutoGlossary")

# ── Публичные константы ───────────────────────────────────────────────────────

# Имя файла кэша на диске (в data_dir).
AUTO_GLOSSARY_CACHE_FILE = "auto_glossary.json"

# ── Вспомогательные функции ───────────────────────────────────────────────────

# W1294: known filler words that often start sentence-initial bigrams.
# Bigrams whose first token matches any of these are noise in the auto-glossary
# because they carry no lexical signal for STT prompt injection.
_FILLER_STARTERS = frozenset({
    # Russian fillers that often start sentences
    "ну", "вот", "так", "хорошо", "ладно", "слушай", "знаешь",
    "понимаешь", "значит", "короче", "типа", "давайте", "итак",
    "затем", "потом", "просто", "вообще", "кстати",
    # English fillers
    "well", "okay", "ok", "like", "you", "i", "so", "right",
    # Spanish fillers
    "bueno", "pues", "vale", "oye", "entonces",
})


def _starts_with_filler(text: str) -> bool:
    """W1294: True if the first word of text is a known filler starter."""
    if not text:
        return False
    first = text.split(maxsplit=1)[0].lower().strip(".,!?;:")
    return first in _FILLER_STARTERS


def _is_capitalized_or_multiword(term: str) -> bool:
    """Возвращает True если термин — заглавное слово или многословная фраза.

    Критерии включения:
    - Первая буква заглавная (имена собственные, аббревиатуры)
    - Многословная фраза (биграмм/триграмм — технические термины)
    - Технические слова с цифрами (GPT-4, iPhone13)
    - Аббревиатуры (2+ заглавных букв)
    """
    if not term:
        return False
    # Многословные фразы всегда включаем
    if " " in term:
        return True
    # Заглавная первая буква
    if term[0].isupper():
        return True
    # Слово содержит цифры (технический термин)
    if any(c.isdigit() for c in term):
        return True
    # Аббревиатура (≥2 заглавных)
    if sum(1 for c in term if c.isupper()) >= 2:
        return True
    return False


# Известные Whisper hallucinations (паттерны субтитров и filler-токены).
# Если term контейнирует эти подстроки целиком — считаем галлюцинацией.
# См. https://github.com/openai/whisper/discussions/928 — типичные RU паттерны.
_HALLUCINATION_PATTERNS = frozenset({
    "продолжение следует",
    "следует продолжение",
    "субтитры от",
    "субтитры подготовил",
    "редактор субтитров",
    "корректор",
    "thanks for watching",
    "subscribe",
    "like and subscribe",
    "конец конец",
    "порядке порядке",
    "голосом голосом",
    "совсем совсем",
})

# Императивные глаголы инструкций (часто "вытекают" из system prompt'а
# rewriter'а или из ChatGPT-style диктовки в audio). Не реальная лексика
# пользователя — фильтруем.
_INSTRUCTION_VERBS = frozenset({
    "пиши", "напиши", "запиши",
    "повторяй", "повтори",
    "сохрани", "сохраняй",
    "укажи", "укажите",
    "верни", "вернёт",
    "исправь", "исправь",
    "проверь", "проверьте",
    "переведи", "переведите",
})


def _looks_like_hallucination(term: str) -> bool:
    """True если term — типичная Whisper hallucination или artefact промпта.

    Защищает auto-glossary feedback loop от self-poisoning: если
    Whisper однажды нагенерил "продолжение следует" на тихом clip'е,
    AutoGlossary не должен его повторно инжектить в prompt → reinforcing.
    """
    if not term:
        return True
    t = term.strip().lower()

    # 1. Точное совпадение с known hallucinations
    if t in _HALLUCINATION_PATTERNS:
        return True

    # 2. Repeated word: "X X" где X — то же слово
    parts = t.split()
    if len(parts) == 2 and parts[0] == parts[1]:
        return True
    # Также 3-grams "X X X"
    if len(parts) == 3 and parts[0] == parts[1] == parts[2]:
        return True

    # 3. Начинается с императивного глагола инструкции
    if parts and parts[0] in _INSTRUCTION_VERBS:
        return True
    # Любой токен в multiword — instruction verb
    if len(parts) >= 2 and any(p in _INSTRUCTION_VERBS for p in parts):
        return True

    # 4. Подстрока совпадает с known hallucination (для embedded artefacts)
    for pattern in _HALLUCINATION_PATTERNS:
        if pattern in t:
            return True

    return False


def _ts_to_epoch(ts: str) -> float:
    """Конвертирует ISO-8601 строку в Unix epoch float.

    Аналогично _iso_to_epoch в transcript_context.py — обрабатывает UTC-строки
    без суффикса "Z" (формат StateStore).

    При ошибке парсинга возвращает 0.0 (элемент считается очень старым).
    """
    import calendar
    import datetime

    ts_clean = ts.strip()
    if ts_clean.endswith("Z"):
        ts_clean = ts_clean[:-1] + "+00:00"

    try:
        dt = datetime.datetime.fromisoformat(ts_clean)
        if dt.tzinfo is not None:
            return dt.timestamp()
        return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.datetime.strptime(ts_clean, fmt)
            return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
        except ValueError:
            continue

    return 0.0


# ── Основной класс ────────────────────────────────────────────────────────────


class AutoGlossaryBuilder:
    """Автоматически строит глоссарий из истории транскрибаций.

    Алгоритм build():
    1. Выбирает записи истории за последние window_days дней.
    2. Для каждой записи извлекает термины через TermExtractor.
    3. Суммирует частоты по всем записям.
    4. Фильтрует: только Capitalized слова, многословные фразы,
       технические термины; стоп-слова исключаются.
    5. Возвращает top-N терминов по частоте.

    Результат кэшируется в памяти и на диске (auto_glossary.json) на
    refresh_hours часов — при повторных вызовах возвращается кэш без
    полного пересчёта.

    Args:
        store: StateStore — источник истории (get_history_page).
        term_extractor: TermExtractor инстанс. Если None — создаётся новый.
        data_dir: директория для хранения кэша на диске. Если None — диск
                  не используется (кэш только в памяти).
        refresh_hours: TTL кэша в часах (default 6).
    """

    def __init__(
        self,
        store: Any,
        term_extractor: Optional[TermExtractor] = None,
        data_dir: Optional[Path] = None,
        refresh_hours: float = 6.0,
        settings_provider: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._store = store
        self._extractor = term_extractor or TermExtractor()
        self._data_dir = data_dir
        self._refresh_hours = refresh_hours
        # Callable returning current settings dict; used to check privacy_mode.
        # None means privacy_mode is assumed off (backward-compatible).
        self._settings_provider = settings_provider

        # In-memory cache
        self._cache: List[str] = []
        self._cache_built_at: float = 0.0  # Unix epoch

        # Загружаем кэш с диска при инициализации
        if self._data_dir:
            self._load_cache_from_disk()

    # ── Публичный API ─────────────────────────────────────────────────────────

    def build(
        self,
        window_days: int = 7,
        top_n: int = 30,
        force: bool = False,
    ) -> List[str]:
        """Возвращает список top-N часто-используемых терминов из истории.

        Результат кэшируется. При force=True — пересчитывает принудительно.

        Args:
            window_days: горизонт истории в днях.
            top_n: максимальное число терминов в результате.
            force: игнорировать кэш, принудительно пересчитать.

        Returns:
            Список строк-терминов (не более top_n), отсортированных по частоте.
        """
        if not force and self._is_cache_valid():
            logger.debug("auto_glossary: cache hit (%d terms)", len(self._cache))
            return list(self._cache[:top_n])

        logger.info(
            "auto_glossary: building from history (window=%dd, top_n=%d)",
            window_days,
            top_n,
        )
        terms = self._build_from_history(window_days=window_days, top_n=top_n)

        self._cache = terms
        self._cache_built_at = time.time()

        if self._data_dir and not self._is_privacy_mode_active():
            self._save_cache_to_disk()
        elif self._is_privacy_mode_active():
            logger.debug(
                "auto_glossary: privacy_mode активен — пропускаем сохранение на диск"
            )

        return list(terms)

    def get_cached(self) -> List[str]:
        """Возвращает текущий кэш без пересчёта (может быть пустым)."""
        return list(self._cache)

    def invalidate(self) -> None:
        """Сбрасывает кэш — следующий вызов build() пересчитает глоссарий."""
        self._cache = []
        self._cache_built_at = 0.0
        if self._data_dir:
            self._save_cache_to_disk()

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _is_cache_valid(self) -> bool:
        """Возвращает True если кэш существует и не устарел."""
        if not self._cache:
            return False
        age_hours = (time.time() - self._cache_built_at) / 3600.0
        return age_hours < self._refresh_hours

    def _is_privacy_mode_active(self) -> bool:
        """Возвращает True если privacy_mode включён в текущих настройках."""
        if self._settings_provider is None:
            return False
        try:
            settings = self._settings_provider()
            return bool(settings.get("privacy_mode", False))
        except Exception as exc:
            logger.warning(
                "auto_glossary: не удалось получить настройки для privacy_mode: %s", exc
            )
            return False

    def _build_from_history(self, window_days: int, top_n: int) -> List[str]:
        """Основная логика построения глоссария из истории."""
        cutoff = time.time() - window_days * 86400

        # Загружаем историю (берём с запасом, потом фильтруем по дате)
        scan_limit = max(500, top_n * 20)
        try:
            items, _ = self._store.get_history_page(cursor=None, limit=scan_limit)
        except Exception as exc:
            logger.error("auto_glossary: ошибка загрузки истории: %s", exc)
            return []

        if not items:
            return []

        # Конвертируем в dict если нужно
        raw_items = [
            (i.to_dict() if hasattr(i, "to_dict") else dict(i)) for i in items
        ]

        # Фильтруем по дате
        recent_items = [
            item for item in raw_items
            if _ts_to_epoch(str(item.get("ts", "") or "")) >= cutoff
        ]

        if not recent_items:
            logger.debug("auto_glossary: нет записей за последние %d дней", window_days)
            return []

        # Агрегируем частоты через TermExtractor
        freq: Counter = Counter()

        for item in recent_items:
            raw_text = str(
                item.get("source_text", "") or item.get("text", "") or ""
            ).strip()
            if not raw_text:
                continue

            extracted = self._extractor.extract_terms(raw_text)
            for et in extracted:
                if _is_stop_word(et.term):
                    continue
                if len(et.term) < 3:
                    continue
                if not _is_capitalized_or_multiword(et.term):
                    continue
                if _looks_like_hallucination(et.term):
                    # Защита от self-poisoning loop: не подбираем Whisper
                    # hallucinations и instruction-style фрагменты.
                    continue
                # W1294: skip bigrams/phrases that start with a filler word
                # (e.g. "хорошо давайте", "знаешь что") — they pollute STT prompt.
                if " " in et.term and _starts_with_filler(et.term):
                    continue
                key = et.term  # сохраняем оригинальный регистр
                freq[key] += et.frequency

        if not freq:
            return []

        # top-N по частоте
        top_terms = [term for term, _ in freq.most_common(top_n)]
        logger.info(
            "auto_glossary: извлечено %d терминов из %d записей",
            len(top_terms),
            len(recent_items),
        )
        return top_terms

    # ── Работа с диском ───────────────────────────────────────────────────────

    def _cache_path(self) -> Optional[Path]:
        if not self._data_dir:
            return None
        return self._data_dir / AUTO_GLOSSARY_CACHE_FILE

    def _load_cache_from_disk(self) -> None:
        """Загружает кэш с диска (при старте сервиса)."""
        path = self._cache_path()
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._cache = data.get("terms", [])
            self._cache_built_at = float(data.get("built_at", 0.0))
            logger.debug(
                "auto_glossary: loaded %d terms from disk (age=%.1fh)",
                len(self._cache),
                (time.time() - self._cache_built_at) / 3600.0,
            )
        except Exception as exc:
            logger.warning("auto_glossary: ошибка загрузки кэша с диска: %s", exc)
            self._cache = []
            self._cache_built_at = 0.0

    def _save_cache_to_disk(self) -> None:
        """Атомарно сохраняет кэш на диск через tmp-файл + fsync + os.replace.

        Запись во временный файл в той же директории гарантирует, что
        os.replace() выполняется в рамках одной файловой системы (атомарно
        на POSIX). Это исключает частично-записанный auto_glossary.json при
        crash или SIGKILL в момент записи.
        """
        path = self._cache_path()
        if not path:
            return
        try:
            payload = json.dumps(
                {"terms": self._cache, "built_at": self._cache_built_at},
                ensure_ascii=False,
                indent=2,
            )
            dir_path = path.parent
            dir_path.mkdir(parents=True, exist_ok=True)
            # Пишем во временный файл в той же директории (важно для os.replace)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(dir_path), prefix=".auto_glossary_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, str(path))
            except Exception:
                # Убираем tmp-файл при любой ошибке, не прячем исключение
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("auto_glossary: ошибка сохранения кэша на диск: %s", exc)
