"""Многоязычные стоп-слова для текстового анализа.

Поддерживаемые языки: русский (ru), испанский (es), английский (en), украинский (uk).
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Словари стоп-слов
# ---------------------------------------------------------------------------

_RU: frozenset = frozenset({
    # Предлоги
    "в", "на", "с", "по", "из", "от", "до", "за", "под", "над", "к", "о",
    "об", "про", "при", "для", "без", "через", "между", "перед", "после",
    "во", "со", "ко", "вместо", "вокруг", "около", "среди", "вдоль",
    "напротив", "против", "согласно", "вслед", "вследствие", "насчёт",
    # Союзы
    "и", "а", "но", "да", "то", "или", "либо", "ни", "ни…ни", "что",
    "как", "если", "хотя", "пока", "когда", "чтобы", "потому", "поэтому",
    "зато", "однако", "тоже", "также", "причём", "притом", "при",
    # Частицы
    "не", "ни", "бы", "же", "ли", "вот", "ну", "вдруг", "уже", "ещё",
    "еще", "даже", "только", "лишь", "именно", "вовсе", "разве", "неужели",
    # Местоимения
    "он", "она", "оно", "они", "мы", "вы", "я", "его", "её", "ее", "их",
    "мой", "твой", "наш", "ваш", "свой", "себя", "тот", "та", "те",
    "этот", "это", "эта", "этой", "этого", "этим", "этих", "такой",
    "такие", "такая", "такого", "такую", "такими", "такими", "сам", "сама",
    "сами", "самого", "самой", "самих", "каждый", "каждая", "каждое",
    "каждого", "каждой", "весь", "вся", "всё", "все", "всего", "всей",
    # Глаголы-связки и вспомогательные
    "быть", "есть", "был", "была", "были", "было", "будет", "будут",
    "буду", "будешь", "будем", "будете", "является", "являются",
    "стать", "стал", "стала", "стали", "стало",
    # Наречия
    "там", "здесь", "тут", "где", "когда", "потом", "затем", "тогда",
    "очень", "более", "менее", "больше", "меньше", "совсем", "просто",
    "так", "вот", "уже", "ещё", "еще", "всегда", "никогда", "иногда",
    "часто", "редко", "скоро", "сейчас", "теперь", "раньше", "позже",
    # Personal pronouns oblique forms (W1106)
    "вам", "вами", "вас", "его", "ему", "ею", "им", "ими", "меня", "мне",
    "мной", "мною", "нам", "нами", "нас", "неё", "ней", "нею", "ним", "ними",
    "тебе", "тебя", "тобой", "тобою", "их",
    # Relative pronoun "который" paradigm (W1106)
    "которая", "которого", "которой", "которому", "которую", "которые",
    "которых", "которым", "которыми", "котором",
    # Прочее
    "можно", "нужно", "надо", "нет", "да", "ладно", "хорошо",
})

_ES: frozenset = frozenset({
    # Artículos
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    # Preposiciones
    "de", "del", "al", "en", "con", "por", "para", "sin", "sobre",
    "entre", "ante", "bajo", "desde", "hasta", "hacia", "durante",
    "mediante", "según", "contra", "excepto", "salvo", "tras",
    # Conjunciones
    "y", "e", "o", "u", "pero", "sino", "que", "como", "si", "aunque",
    "porque", "cuando", "mientras", "donde", "pues", "luego", "además",
    "tampoco", "ni", "ya",
    # Pronombres
    "yo", "tú", "él", "ella", "ello", "nosotros", "nosotras", "vosotros",
    "vosotras", "ellos", "ellas", "usted", "ustedes", "me", "te", "le",
    "nos", "os", "les", "lo", "la", "se", "mi", "mis", "tu", "tus",
    "su", "sus", "nuestro", "nuestra", "nuestros", "nuestras",
    "vuestro", "vuestra", "vuestros", "vuestras",
    # Demostrativos
    "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas",
    "aquel", "aquella", "aquellos", "aquellas", "esto", "eso", "aquello",
    # Verbos auxiliares / ser / estar
    "es", "son", "era", "eran", "fue", "fueron", "ser", "estar",
    "estoy", "estás", "está", "estamos", "estáis", "están",
    "he", "has", "ha", "hemos", "habéis", "han", "haber", "hay",
    "tengo", "tiene", "tener",
    # Adverbios frecuentes
    "no", "sí", "ya", "más", "muy", "bien", "también", "así",
    "todo", "todos", "todas", "aquí", "allí", "ahí", "ahora", "antes",
    "después", "siempre", "nunca", "algo", "nada", "mucho", "poco",
    "tan", "tanto", "tal", "cuál", "qué", "quién", "dónde", "cuándo",
    "cómo", "por qué",
})

_EN: frozenset = frozenset({
    # Articles
    "the", "a", "an",
    # Prepositions
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "up",
    "about", "into", "through", "during", "before", "after", "above",
    "below", "between", "out", "off", "over", "under", "against",
    "along", "among", "around", "across", "behind", "beside", "beyond",
    "down", "inside", "near", "outside", "past", "since", "toward",
    "towards", "upon", "within", "without",
    # Conjunctions
    "and", "but", "or", "nor", "so", "yet", "both", "either", "neither",
    "although", "because", "if", "since", "though", "unless", "until",
    "when", "while", "whereas", "whether",
    # Pronouns
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "our", "their", "its",
    "myself", "yourself", "himself", "herself", "itself", "ourselves",
    "themselves", "this", "that", "these", "those", "which", "who",
    "whom", "whose",
    # Auxiliary verbs / be
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall",
    "can", "must", "need", "dare", "ought",
    # Common adverbs / determiners
    "not", "no", "never", "always", "often", "sometimes", "already",
    "still", "just", "only", "even", "also", "too", "very", "much",
    "many", "more", "most", "some", "any", "all", "each", "every",
    "few", "little", "other", "such", "own", "same", "than", "then",
    "there", "here", "where", "when", "how", "what", "why", "as",
    "if", "so", "again", "further", "once",
})

_UK: frozenset = frozenset({
    # Прийменники
    "в", "у", "на", "з", "із", "зі", "до", "від", "за", "під", "над",
    "к", "о", "об", "про", "при", "для", "без", "через", "між", "перед",
    "після", "замість", "навколо", "близько", "серед", "вздовж",
    # Сполучники
    "і", "й", "та", "а", "але", "проте", "однак", "чи", "або", "то",
    "що", "як", "якщо", "хоча", "поки", "коли", "щоб", "тому", "бо",
    "адже", "також", "теж", "при",
    # Частки
    "не", "ні", "б", "би", "же", "ж", "вже", "ще", "навіть", "тільки",
    "лише", "саме", "хіба", "невже",
    # Займенники
    "він", "вона", "воно", "вони", "ми", "ви", "я", "його", "її", "їх",
    "мій", "твій", "наш", "ваш", "свій", "себе", "той", "та", "ті",
    "цей", "це", "ця", "цього", "цієї", "цим", "цих", "такий",
    "такі", "така", "кожний", "кожна", "кожне", "кожного", "весь",
    "вся", "все", "всі", "всього", "сам", "сама", "самі",
    # Допоміжні дієслова
    "бути", "є", "був", "була", "були", "буде", "будуть",
    "стати", "став", "стала", "стали",
    # Прислівники
    "там", "тут", "де", "коли", "потім", "тоді", "дуже", "більше",
    "менше", "зовсім", "просто", "так", "ось", "вже", "ще", "завжди",
    "ніколи", "іноді", "часто", "зараз", "тепер", "раніше", "скоро",
    "можна", "треба", "потрібно", "немає", "ні", "так",
})

# Объединённый словарь по ISO-639-1 кодам
_LANGUAGE_MAP: dict[str, frozenset] = {
    "ru": _RU,
    "es": _ES,
    "en": _EN,
    "uk": _UK,
}

# Все стоп-слова вместе (для авто-детекции)
_ALL: frozenset = _RU | _ES | _EN | _UK

_TOKENIZE_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


class StopWords:
    """Утилита для работы со стоп-словами на нескольких языках.

    Пример использования::

        sw = StopWords()
        sw.is_stop_word("в")          # True
        sw.is_stop_word("the", "en")  # True
        sw.filter_text(["я", "иду"])  # ["иду"]
    """

    # -----------------------------------------------------------------------
    # Публичный API
    # -----------------------------------------------------------------------

    @staticmethod
    def get_stop_words(language: str) -> frozenset:
        """Возвращает frozenset стоп-слов для указанного языка (ISO 639-1).

        Args:
            language: код языка ('ru', 'es', 'en', 'uk').  При неизвестном
                      коде возвращает пустое множество.
        """
        return _LANGUAGE_MAP.get(language.lower().strip(), frozenset())

    @staticmethod
    def is_stop_word(word: str, language: Optional[str] = None) -> bool:
        """Проверяет, является ли слово стоп-словом.

        Args:
            word:     слово для проверки (регистр не важен).
            language: ISO 639-1 код ('ru', 'es', 'en', 'uk').
                      Если не указан — проверяет по всем языкам.
        """
        w = word.lower().strip()
        if language:
            return w in _LANGUAGE_MAP.get(language.lower().strip(), frozenset())
        return w in _ALL

    @staticmethod
    def filter_text(
        words: list[str],
        language: Optional[str] = None,
        min_length: int = 2,
    ) -> list[str]:
        """Удаляет стоп-слова из списка токенов.

        Args:
            words:      список слов.
            language:   ISO 639-1 код. Если не указан — фильтрует по всем языкам.
            min_length: минимальная длина слова (слова короче удаляются).
        """
        stop_set = _LANGUAGE_MAP.get(language.lower().strip(), _ALL) if language else _ALL
        return [w for w in words if w.lower() not in stop_set and len(w) >= min_length]

    @staticmethod
    def supported_languages() -> list[str]:
        """Возвращает список поддерживаемых кодов языков."""
        return list(_LANGUAGE_MAP.keys())
