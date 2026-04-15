"""Конфигурируемый конвейер пост-обработки текста для Krab Ear.

TextPostProcessor позволяет выстраивать цепочку трансформаций транскрипций:
нормализация пробелов → пунктуация → сущности → аббревиатуры → анонимизация.

Использование:
    processor = TextPostProcessor()
    result = processor.process("привет как дела т.е. всё ок")
    # result.text — обработанный текст
    # result.steps_applied — список применённых шагов
    # result.changes_count — количество шагов, изменивших текст
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger("KrabEar.TextPostProcessor")

# ── Публичный интерфейс шага ────────────────────────────────────────────────


@runtime_checkable
class PostProcessorStep(Protocol):
    """Протокол одного шага обработки текста.

    Каждый шаг обязан:
      • name  — уникальное строковое имя (используется при выборе steps=).
      • process(text) — чистая трансформация, возвращает новую строку.
    """

    name: str

    def process(self, text: str) -> str:  # noqa: D102
        ...


# ── Датакласс результата ────────────────────────────────────────────────────


@dataclass
class PostProcessResult:
    """Результат прохода через TextPostProcessor.

    Атрибуты:
        text           — финальный текст после всех применённых шагов.
        steps_applied  — имена шагов, которые были выполнены (в порядке выполнения).
        changes_count  — количество шагов, в которых текст фактически изменился.
    """

    text: str
    steps_applied: list[str] = field(default_factory=list)
    changes_count: int = 0


# ── Встроенные шаги ─────────────────────────────────────────────────────────

_MULTI_SPACE_RE = re.compile(r" {2,}")
_LEADING_TRAILING_RE = re.compile(r"^\s+|\s+$")
_MIXED_NEWLINE_RE = re.compile(r"\r\n|\r")


class StripWhitespace:
    """Нормализует пробельные символы: убирает лишние пробелы, выравнивает переносы строк."""

    name: str = "strip_whitespace"

    def process(self, text: str) -> str:
        if not text:
            return text
        # Унификация переносов строк
        result = _MIXED_NEWLINE_RE.sub("\n", text)
        # Убираем множественные пробелы внутри строки (но не переносы)
        result = _MULTI_SPACE_RE.sub(" ", result)
        # Убираем пробелы в начале/конце
        result = result.strip()
        return result


class FixPunctuation:
    """Исправляет пунктуацию через PunctuationFixer.

    Args:
        language: Код языка ("ru", "es", "en"). По умолчанию "ru".
    """

    name: str = "fix_punctuation"

    def __init__(self, language: str = "ru") -> None:
        self._language = language
        # Ленивый импорт — избегаем циклических зависимостей при загрузке модуля.
        self._fixer = None

    def _get_fixer(self):
        if self._fixer is None:
            from core.punctuation_fixer import PunctuationFixer
            self._fixer = PunctuationFixer()
        return self._fixer

    def process(self, text: str) -> str:
        if not text or not text.strip():
            return text
        return self._get_fixer().fix(text, language=self._language)


class ExpandAbbreviations:
    """Разворачивает аббревиатуры через AbbreviationExpander.

    Args:
        language:  Код языка ("ru", "en", "es"). По умолчанию "ru".
        data_dir:  Директория для загрузки пользовательских аббревиатур.
                   Если None — используются только встроенные.
    """

    name: str = "expand_abbreviations"

    def __init__(self, language: str = "ru", data_dir=None) -> None:
        self._language = language
        self._data_dir = data_dir
        self._expander = None

    def _get_expander(self):
        if self._expander is None:
            from core.abbreviation_expander import AbbreviationExpander
            self._expander = AbbreviationExpander(data_dir=self._data_dir)
        return self._expander

    def process(self, text: str) -> str:
        if not text or not text.strip():
            return text
        return self._get_expander().expand(text, language=self._language)


class Anonymize:
    """Редактирует персональные данные через TextAnonymizer.

    Args:
        rules: Список имён правил анонимизации. Если None — применяются все.
    """

    name: str = "anonymize"

    def __init__(self, rules: list[str] | None = None) -> None:
        self._rules = rules
        self._anonymizer = None

    def _get_anonymizer(self):
        if self._anonymizer is None:
            from core.text_anonymizer import TextAnonymizer
            self._anonymizer = TextAnonymizer()
        return self._anonymizer

    def process(self, text: str) -> str:
        if not text:
            return text
        result = self._get_anonymizer().anonymize(text, rules=self._rules)
        return result.anonymized_text


class NormalizeEntities:
    """Канонизирует бренды/имена и нормализует формат времени через TextUtils."""

    name: str = "normalize_entities"

    def process(self, text: str) -> str:
        if not text:
            return text
        from core.utils import TextUtils
        return TextUtils.normalize_entities(text)


# ── Реестр встроенных шагов ─────────────────────────────────────────────────

#: Имя → экземпляр шага для быстрого поиска по строке.
#: Шаги без параметров — синглтоны; параметризованные создаются при необходимости.
_BUILTIN_STEPS: dict[str, PostProcessorStep] = {
    "strip_whitespace": StripWhitespace(),
    "fix_punctuation": FixPunctuation(language="ru"),
    "expand_abbreviations": ExpandAbbreviations(language="ru"),
    "anonymize": Anonymize(),
    "normalize_entities": NormalizeEntities(),
}

# Шаги по умолчанию (выполняются если steps=None)
DEFAULT_CHAIN: list[str] = ["strip_whitespace", "fix_punctuation", "normalize_entities"]


# ── Главный класс ────────────────────────────────────────────────────────────


class TextPostProcessor:
    """Конфигурируемый конвейер пост-обработки текста.

    Поддерживает:
    • Встроенные шаги: strip_whitespace, fix_punctuation, expand_abbreviations,
      anonymize, normalize_entities.
    • Регистрацию произвольных шагов через register_step().
    • Выбор активных шагов через параметр steps= (список имён).
    • Цепочку по умолчанию: strip_whitespace → fix_punctuation → normalize_entities.

    Использование:
        processor = TextPostProcessor()
        result = processor.process("привет как дела т.е. всё ок", steps=["strip_whitespace", "expand_abbreviations"])
        print(result.text)           # "привет как дела то есть всё ок"
        print(result.steps_applied)  # ["strip_whitespace", "expand_abbreviations"]
        print(result.changes_count)  # 1 (только expand_abbreviations изменил текст)
    """

    def __init__(self) -> None:
        # Копируем реестр, чтобы разные экземпляры не делили состояние.
        self._steps: dict[str, PostProcessorStep] = dict(_BUILTIN_STEPS)

    # ── Публичный API ────────────────────────────────────────────────────────

    def register_step(self, step: PostProcessorStep) -> None:
        """Регистрирует новый шаг (или перезаписывает существующий с тем же именем).

        Args:
            step: Объект, реализующий протокол PostProcessorStep.
        """
        if not isinstance(step, PostProcessorStep):
            raise TypeError(
                f"Шаг должен реализовывать PostProcessorStep (name + process). "
                f"Получен: {type(step)!r}"
            )
        self._steps[step.name] = step
        logger.debug("Зарегистрирован шаг постобработки: %r", step.name)

    def list_steps(self) -> list[str]:
        """Возвращает имена всех доступных шагов (встроенных + кастомных)."""
        return list(self._steps.keys())

    def process(
        self,
        text: str,
        steps: list[str] | None = None,
    ) -> PostProcessResult:
        """Прогоняет текст через указанную цепочку шагов.

        Args:
            text:  Исходный текст для обработки.
            steps: Список имён шагов в нужном порядке.
                   Если None — применяется DEFAULT_CHAIN.
                   Неизвестные имена шагов логируются как warning и пропускаются.

        Returns:
            PostProcessResult с финальным текстом, списком применённых шагов
            и числом шагов, изменивших текст.
        """
        if not text:
            return PostProcessResult(text=text, steps_applied=[], changes_count=0)

        active_steps = steps if steps is not None else DEFAULT_CHAIN
        current = text
        steps_applied: list[str] = []
        changes_count = 0

        for step_name in active_steps:
            step = self._steps.get(step_name)
            if step is None:
                logger.warning(
                    "Шаг постобработки %r не найден, пропускается. "
                    "Доступны: %s",
                    step_name,
                    ", ".join(self._steps),
                )
                continue

            try:
                before = current
                current = step.process(current)
                steps_applied.append(step_name)
                if current != before:
                    changes_count += 1
            except Exception as exc:
                logger.exception(
                    "Ошибка в шаге постобработки %r: %s. Текст остаётся без изменений.",
                    step_name,
                    exc,
                )
                # Продолжаем с предыдущим текстом — шаг не применяется.
                steps_applied.append(step_name)

        return PostProcessResult(
            text=current,
            steps_applied=steps_applied,
            changes_count=changes_count,
        )
