"""TextDiffAnalyzer — word-level diff между оригинальным и rewritten текстом.

Используется для отображения изменений, внесённых LLM rewriter'ом.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import List


@dataclass
class DiffChange:
    """Одно изменение в word-level diff'е."""

    type: str  # "added" | "removed" | "unchanged"
    text: str  # слово или группа слов
    position: int  # позиция (индекс слова) в соответствующей строке


@dataclass
class TextDiffResult:
    """Результат сравнения двух текстов."""

    changes: List[DiffChange] = field(default_factory=list)
    similarity_ratio: float = 0.0
    words_added: int = 0
    words_removed: int = 0
    words_unchanged: int = 0
    summary: str = ""


class TextDiffAnalyzer:
    """Вычисляет word-level diff между двумя строками.

    Использует difflib.SequenceMatcher для вычисления similarity_ratio
    и построения списка изменений.
    """

    def compute_diff(self, original: str, rewritten: str) -> TextDiffResult:
        """Сравнивает original и rewritten на уровне слов.

        Args:
            original: исходный текст (до LLM rewrite)
            rewritten: итоговый текст (после LLM rewrite)

        Returns:
            TextDiffResult с полным описанием изменений.
        """
        orig_words = (original or "").split()
        new_words = (rewritten or "").split()

        # Similarity ratio на уровне символов для более точной метрики
        char_matcher = difflib.SequenceMatcher(None, original or "", rewritten or "")
        similarity_ratio = round(char_matcher.ratio(), 4)

        # Word-level diff через SequenceMatcher
        word_matcher = difflib.SequenceMatcher(None, orig_words, new_words)
        changes: List[DiffChange] = []
        words_added = 0
        words_removed = 0
        words_unchanged = 0

        for tag, i1, i2, j1, j2 in word_matcher.get_opcodes():
            if tag == "equal":
                for k, word in enumerate(orig_words[i1:i2]):
                    changes.append(DiffChange(type="unchanged", text=word, position=i1 + k))
                words_unchanged += i2 - i1

            elif tag == "replace":
                # Убранные слова (из оригинала)
                for k, word in enumerate(orig_words[i1:i2]):
                    changes.append(DiffChange(type="removed", text=word, position=i1 + k))
                words_removed += i2 - i1
                # Добавленные слова (в rewritten)
                for k, word in enumerate(new_words[j1:j2]):
                    changes.append(DiffChange(type="added", text=word, position=j1 + k))
                words_added += j2 - j1

            elif tag == "delete":
                for k, word in enumerate(orig_words[i1:i2]):
                    changes.append(DiffChange(type="removed", text=word, position=i1 + k))
                words_removed += i2 - i1

            elif tag == "insert":
                for k, word in enumerate(new_words[j1:j2]):
                    changes.append(DiffChange(type="added", text=word, position=j1 + k))
                words_added += j2 - j1

        summary = self._build_summary(words_added, words_removed, words_unchanged, similarity_ratio)

        return TextDiffResult(
            changes=changes,
            similarity_ratio=similarity_ratio,
            words_added=words_added,
            words_removed=words_removed,
            words_unchanged=words_unchanged,
            summary=summary,
        )

    @staticmethod
    def _build_summary(added: int, removed: int, unchanged: int, ratio: float) -> str:
        """Формирует человекочитаемое описание изменений."""
        pct = int(ratio * 100)
        parts = []
        if added:
            parts.append(f"added {added} word{'s' if added != 1 else ''}")
        if removed:
            parts.append(f"removed {removed} word{'s' if removed != 1 else ''}")
        if not parts:
            if unchanged == 0:
                return "no text"
            return f"no changes, {pct}% similar"
        change_str = ", ".join(parts)
        return f"LLM {change_str}, {pct}% similar"
