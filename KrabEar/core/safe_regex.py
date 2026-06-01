"""Утилиты безопасной компиляции и выполнения регулярных выражений — Krab Ear.

Módulo de utilidades para compilación y ejecución segura de expresiones regulares.

Назначение / Objetivo
---------------------
Пользователь может передавать произвольные регулярные выражения через IPC
(например, паттерны галлюцинаций, фильтры словаря и т.п.).  Если паттерн
содержит вложенные квантификаторы («catastrophic backtracking»), однопоточный
event-loop бэкенда может зависнуть на секунды или минуты.

Este módulo provee dos capas de defensa:

1. **compile_safe** — отклоняет паттерны с известными структурами катастрофического
   отката *до* компиляции, используя структурный сканер сбалансированных скобок
   (imported from ``core.hallucination_manager``).  Также применяет ограничение
   длины паттерна.

2. **run_with_timeout** / **search_safe** — выполняют уже скомпилированный паттерн
   против текста в отдельном daemon-потоке с ограничением по времени.  Если
   совпадение не найдено за ``timeout_sec``, возвращается ``None`` и в лог
   пишется структурированное предупреждение без содержимого паттерна или текста
   (privacy-safe).

Важно: ``signal.alarm`` не используется — он работает только в главном потоке
и небезопасен в модели IPC «поток-на-клиента» (thread-per-connection).
Вместо этого применяется ``threading.Thread.join(timeout)`` — безопасен
в любом потоке.

Публичный API
-------------
- ``compile_safe(pattern, flags=0, *, max_pattern_len=1000) -> re.Pattern``
- ``search_safe(pattern, text, flags=0, *, timeout_sec=1.0, max_text_len=200_000)``
  ``-> re.Match | None``
- ``run_with_timeout(compiled, text, *, timeout_sec=1.0) -> re.Match | None``
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

logger = logging.getLogger("KrabEar.Core.SafeRegex")

# ── Константы ──────────────────────────────────────────────────────────────────

# Максимальная длина паттерна по умолчанию (символов).
# Длинные паттерны редко легитимны и могут маскировать атаки.
_DEFAULT_MAX_PATTERN_LEN: int = 1000

# Максимальная длина входного текста по умолчанию (символов).
# Транскрипты Krab Ear < 2 КБ; 200 000 — щедрый запас для импортированных файлов.
_DEFAULT_MAX_TEXT_LEN: int = 200_000

# ── Вспомогательные константы для структурного сканера ────────────────────────

# Символы-квантификаторы, следующие за группой.
_QUANTIFIER_CHARS = frozenset("+*{")

# Допустимые символы флага/синтаксиса, следующие за «(?».
_GROUP_FLAG_CHARS = frozenset(":imsxu=<!>")


# ── Структурный сканер вложенных квантификаторов ───────────────────────────────

def _body_has_quantifier(pattern: str, start: int, end: int) -> bool:
    """Проверяет, содержит ли тело группы ``pattern[start:end]`` квантификатор.

    Comprueba si el cuerpo de un grupo ``pattern[start:end]`` contiene un
    cuantificador que hace peligrosa la cuantificación externa.

    Тело считается «повторяемым» (опасным при внешнем квантификаторе) если:

    1. Голый квантификатор (+, *, {) на глубине 0 — e.g. тело ``a+``.
    2. Подгруппа, за которой сразу следует квантификатор на глубине 0 —
       e.g. тело ``(a)+``.
    3. Подгруппа, собственное тело которой само является «повторяемым»
       (рекурсивно) — e.g. тело ``(a+)`` в ``(?:(a+))+``.

    Args:
        pattern: Полная строка паттерна.
        start: Индекс первого символа тела группы.
        end: Исключающий конец (позиция закрывающей скобки).

    Returns:
        True если тело «повторяемое» в одном из трёх смыслов выше.
    """
    depth = 0
    in_char_class = False
    i = start
    while i < end:
        ch = pattern[i]
        if in_char_class:
            if ch == "\\" and i + 1 < end:
                i += 2
                continue
            if ch == "]":
                in_char_class = False
        else:
            if ch == "\\" and i + 1 < end:
                i += 2
                continue
            if ch == "[":
                in_char_class = True
            elif ch == "(":
                depth += 1
                if depth == 1:
                    # Находим закрывающую скобку для этой подгруппы.
                    sub_depth = 1
                    k = i + 1
                    sub_in_cls = False
                    while k < end and sub_depth > 0:
                        c = pattern[k]
                        if sub_in_cls:
                            if c == "\\" and k + 1 < end:
                                k += 2
                                continue
                            if c == "]":
                                sub_in_cls = False
                        else:
                            if c == "\\" and k + 1 < end:
                                k += 2
                                continue
                            if c == "[":
                                sub_in_cls = True
                            elif c == "(":
                                sub_depth += 1
                            elif c == ")":
                                sub_depth -= 1
                        k += 1
                    if sub_depth != 0:
                        i += 1
                        continue
                    sub_close = k - 1  # индекс «)»
                    # Вычисляем начало тела подгруппы (пропускаем (?...)  префикс).
                    sub_body_start = i + 1
                    if sub_body_start < sub_close and pattern[sub_body_start] == "?":
                        sk = sub_body_start + 1
                        while sk < sub_close and pattern[sk] in _GROUP_FLAG_CHARS:
                            sk += 1
                        sub_body_start = sk
                    # Случай 2: подгруппа сразу квантифицирована.
                    after_sub = k
                    if after_sub < end and pattern[after_sub] in _QUANTIFIER_CHARS:
                        return True
                    # Случай 3: тело подгруппы само «повторяемое» (рекурсия).
                    if _body_has_quantifier(pattern, sub_body_start, sub_close):
                        return True
                    i = sub_close
            elif ch == ")":
                if depth > 0:
                    depth -= 1
            elif depth == 0 and ch in _QUANTIFIER_CHARS:
                # Случай 1: голый квантификатор на верхнем уровне тела.
                return True
        i += 1
    return False


def _has_nested_quantifiers(pattern: str) -> bool:
    """Структурный сканер: ищет вложенные квантификаторы в паттерне.

    Escáner estructural: detecta cuantificadores anidados en el patrón.

    Обнаруживает любую группу (захватывающую, незахватывающую ``(?:...)``,
    inline-флаги, lookahead/behind), тело которой содержит квантификатор И
    которая сама квантифицирована.  Правильно работает для:

    - ``(a+)+``          — классический nested plus
    - ``(a*)*``          — nested star
    - ``((?:a)+)+``      — незахватывающая внутренняя группа
    - ``(?:(a+))+``      — незахватывающая внешняя группа
    - ``((?:[a-z])+)+``  — char-class в незахватывающей группе
    - ``((?:a)+){5,}``   — фигурный квантификатор на NC-обёрнутой группе
    - ``((a+)+)``        — глубоко вложенные структуры

    Args:
        pattern: Сырая строка регулярного выражения для проверки.

    Returns:
        True если обнаружена структура катастрофического отката.
    """
    length = len(pattern)
    i = 0
    in_char_class = False

    while i < length:
        ch = pattern[i]

        if in_char_class:
            if ch == "\\" and i + 1 < length:
                i += 2
                continue
            if ch == "]":
                in_char_class = False
            i += 1
            continue

        if ch == "\\" and i + 1 < length:
            i += 2
            continue

        if ch == "[":
            in_char_class = True
            i += 1
            continue

        if ch != "(":
            i += 1
            continue

        # Нашли открывающую скобку — ищем соответствующую закрывающую.
        depth = 1
        j = i + 1
        inner_in_cls = False
        while j < length and depth > 0:
            c = pattern[j]
            if inner_in_cls:
                if c == "\\" and j + 1 < length:
                    j += 2
                    continue
                if c == "]":
                    inner_in_cls = False
            else:
                if c == "\\" and j + 1 < length:
                    j += 2
                    continue
                if c == "[":
                    inner_in_cls = True
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
            j += 1

        if depth != 0:
            i += 1
            continue

        close_paren = j - 1   # индекс «)»
        body_start = i + 1

        # Пропускаем inline-флаги/NC-префикс (?...).
        if body_start < close_paren and pattern[body_start] == "?":
            k = body_start + 1
            while k < close_paren and pattern[k] in _GROUP_FLAG_CHARS:
                k += 1
            body_start = k

        # Проверяем, стоит ли квантификатор после закрывающей скобки.
        after = j
        if after < length and pattern[after] in _QUANTIFIER_CHARS:
            if _body_has_quantifier(pattern, body_start, close_paren):
                return True

        i += 1

    return False


# ── Публичный API ──────────────────────────────────────────────────────────────

def compile_safe(
    pattern: str,
    flags: int = 0,
    *,
    max_pattern_len: int = _DEFAULT_MAX_PATTERN_LEN,
) -> re.Pattern:
    """Безопасно компилирует пользовательский regex, отклоняя опасные паттерны.

    Compila de forma segura una expresión regular provista por el usuario,
    rechazando patrones peligrosos.

    Два уровня защиты:

    1. **Ограничение длины** — паттерны длиннее ``max_pattern_len`` символов
       отклоняются немедленно.
    2. **Структурный сканер** — ищет вложенные квантификаторы (``(a+)+``,
       ``(a*)*``, ``((?:a)+)+`` и аналоги), вызывающие катастрофический откат.
       Использует обход дерева скобок, устойчивый к NC-группам и inline-флагам.

    Эту функцию следует использовать вместо ``re.compile()`` для ЛЮБОГО паттерна,
    поступающего от пользователя или из конфигурации.

    Esta función debe usarse en lugar de ``re.compile()`` para cualquier patrón
    que provenga del usuario o de la configuración.

    Args:
        pattern: Строка регулярного выражения.
        flags: Стандартные флаги ``re`` (``re.IGNORECASE`` и т.п.).
        max_pattern_len: Максимально допустимая длина паттерна в символах.
            По умолчанию 1000.

    Returns:
        Скомпилированный объект ``re.Pattern``.

    Raises:
        ValueError: Если паттерн слишком длинный или содержит вложенные
            квантификаторы (ReDoS).
        re.error: Если паттерн синтаксически некорректен.

    Examples:
        >>> p = compile_safe(r"\\bслово\\b")
        >>> bool(p.search("слово в предложении"))
        True
        >>> compile_safe(r"(a+)+")
        Traceback (most recent call last):
            ...
        ValueError: ...вложенн...
    """
    if not isinstance(pattern, str):
        raise TypeError(f"Паттерн должен быть строкой, получено: {type(pattern).__name__}")

    # Слой 1: ограничение длины.
    if len(pattern) > max_pattern_len:
        raise ValueError(
            f"Паттерн слишком длинный ({len(pattern)} символов, максимум {max_pattern_len}). "
            "Используйте более короткое выражение / "
            "Patrón demasiado largo, use una expresión más corta."
        )

    # Слой 2: структурный сканер вложенных квантификаторов.
    if _has_nested_quantifiers(pattern):
        raise ValueError(
            "Паттерн содержит вложенные квантификаторы, вызывающие катастрофический откат "
            "(ReDoS). Используйте простые выражения без конструкций вида (a+)+, (a*)* и т.п. / "
            "El patrón contiene cuantificadores anidados que causan retroceso catastrófico "
            "(ReDoS). Use expresiones simples sin estructuras como (a+)+."
        )

    # Компилируем — синтаксические ошибки поднимутся как re.error.
    return re.compile(pattern, flags)


def run_with_timeout(
    compiled: re.Pattern,
    text: str,
    *,
    timeout_sec: float = 1.0,
    _max_text_backstop: int = 8192,
) -> "re.Match[str] | None":
    """Запускает ``compiled.search(text)`` с ограничением по времени.

    Ejecuta ``compiled.search(text)`` con un límite de tiempo.

    Реализует двухуровневую защиту:

    1. **Ограничение длины текста (backstop)** — текст обрезается до
       ``_max_text_backstop`` символов *перед* передачей в regex-движок.
       Это надёжная защита: CPython ``re`` держит GIL во время выполнения
       чистых Python-вычислений, поэтому ``Thread.join(timeout)`` не может
       прервать зависший поиск.  Ограничение длины устраняет класс атак,
       основанных на патологически длинном вводе.

    2. **Поточный таймаут (best-effort)** — поиск выполняется в daemon-потоке;
       ``join(timeout_sec)`` ждёт завершения.  Если поток не завершился —
       возвращается ``None`` и пишется структурированное предупреждение.
       Это даёт защиту для экзотических паттернов на коротком вводе, не
       поддающихся GIL-прерыванию (например, если в будущем добавится
       расширение C с releasegil).

    Важно: полное прерывание зависшего потока с CPython-``re`` не гарантируется
    (GIL удерживается).  Первичная защита — ``compile_safe`` (отклонение
    вложенных квантификаторов) + backstop длины текста.

    Importante: no se garantiza la interrupción de un hilo bloqueado con
    CPython-``re`` (GIL retenido). La defensa primaria es ``compile_safe``
    (rechazo de cuantificadores anidados) + límite de longitud del texto.

    Почему не ``signal.alarm``: он работает только в главном потоке Python
    и небезопасен в модели IPC «поток-на-клиента».

    Args:
        compiled: Скомпилированный объект ``re.Pattern`` (из ``compile_safe``
            или ``re.compile``).
        text: Текст для поиска совпадений.
        timeout_sec: Максимальное время ожидания в секундах.  По умолчанию 1.0.
        _max_text_backstop: Максимальная длина текста, передаваемого в regex
            (backstop).  Не предназначен для использования извне.

    Returns:
        Объект совпадения ``re.Match`` или ``None`` (если совпадений нет или
        истёк таймаут).
    """
    # Backstop: обрезаем текст до разумного предела *до* запуска потока.
    # Это надёжная защита от патологически длинных вводов.
    clipped = text[:_max_text_backstop] if len(text) > _max_text_backstop else text

    result_container: list[Any] = [None]
    exc_container: list[BaseException | None] = [None]

    def _worker() -> None:
        try:
            result_container[0] = compiled.search(clipped)
        except Exception as exc:  # pragma: no cover — defensive
            exc_container[0] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout_sec)

    if t.is_alive():
        # Поток всё ещё работает после таймаута (GIL держит CPython re).
        # Мы перестаём ждать — поток «заброшен» как daemon и завершится
        # после выхода из process или когда regex сам допробежит.
        logger.warning(
            "Regex search превысил таймаут (best-effort, GIL ограничивает прерывание)",
            extra={
                "timeout_sec": timeout_sec,
                "text_len": len(clipped),
                "pattern_id": id(compiled),  # только идентификатор, не сам паттерн
            },
        )
        return None

    if exc_container[0] is not None:
        raise exc_container[0]  # type: ignore[misc]

    return result_container[0]


def search_safe(
    pattern: str,
    text: str,
    flags: int = 0,
    *,
    timeout_sec: float = 1.0,
    max_text_len: int = _DEFAULT_MAX_TEXT_LEN,
) -> "re.Match[str] | None":
    """Безопасно компилирует паттерн и запускает поиск с таймаутом.

    Compila el patrón de forma segura y ejecuta la búsqueda con tiempo límite.

    Удобная обёртка: ``compile_safe`` + ``run_with_timeout`` + ограничение
    длины входного текста.

    Args:
        pattern: Строка регулярного выражения.
        text: Текст для поиска.
        flags: Флаги ``re``.
        timeout_sec: Максимальное время ожидания в секундах.
        max_text_len: Текст будет обрезан до этой длины перед поиском.

    Returns:
        ``re.Match`` или ``None``.

    Raises:
        ValueError: Если паттерн слишком длинный или содержит ReDoS-структуру.
        re.error: Если паттерн синтаксически некорректен.
    """
    compiled = compile_safe(pattern, flags)
    clipped = text[:max_text_len] if len(text) > max_text_len else text
    return run_with_timeout(compiled, clipped, timeout_sec=timeout_sec)
