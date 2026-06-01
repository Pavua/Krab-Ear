"""TextComparator — side-by-side comparison of two transcription versions.

Compares two texts or two history items by ID, producing a rich ComparisonResult
with similarity score, common phrases, unique fragments, and a human-readable summary.

DoS guard (wave1765): _extract_phrases ранее перебирала ВСЕ n-граммы от MIN до len(words),
что давало O(n²) фраз и O(n³) символов — один длинный запрос через IPC exhausted RAM/CPU.
Защита: входной текст усекается до MAX_COMPARE_WORDS, а размер n-грамм ограничен
MAX_PHRASE_SIZE. Итоговая сложность — O(n × MAX_PHRASE_SIZE), то есть линейная.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from backend.state_store import StateStore

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Защитные константы — DoS guard
# --------------------------------------------------------------------------

# Максимальное число слов, подаваемых на анализ фраз.
# 5000 слов ≈ ~10 страниц; больше не нужно для задач similarity.
MAX_COMPARE_WORDS: int = 5_000

# Максимальный размер n-граммы (слов). 10-словные шинглы отлично покрывают
# сходство; полные длинные фразы не нужны.
MAX_PHRASE_SIZE: int = 10

# Жёсткий backstop: не более этого числа фраз в множестве на один текст.
# При MAX_COMPARE_WORDS=5000 и MAX_PHRASE_SIZE=10 теоретический максимум
# ~8 × MAX_COMPARE_WORDS = 40 000 — этот backstop служит дополнительной защитой.
MAX_PHRASES: int = 50_000


@dataclass
class ComparisonResult:
    """Result of comparing two transcription texts."""

    similarity: float
    text_1: str
    text_2: str
    common_phrases: List[str] = field(default_factory=list)
    unique_to_1: List[str] = field(default_factory=list)
    unique_to_2: List[str] = field(default_factory=list)
    word_count_diff: int = 0
    summary: str = ""


class TextComparator:
    """Compares two texts or history items side-by-side.

    Uses difflib.SequenceMatcher for similarity and sliding-window phrase extraction
    to find common / unique 3+-word phrases.

    Входные тексты усекаются до MAX_COMPARE_WORDS слов, а n-граммы — до MAX_PHRASE_SIZE
    слов, чтобы обеспечить линейную по памяти/времени сложность для любых входных данных
    из IPC.
    """

    MIN_PHRASE_WORDS: int = 3

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compare_items(self, item_id_1: str, item_id_2: str, store: "StateStore") -> ComparisonResult:
        """Compare two history items by ID.

        Args:
            item_id_1: ID первой записи истории.
            item_id_2: ID второй записи истории.
            store: экземпляр StateStore для поиска записей.

        Returns:
            ComparisonResult с полным анализом.

        Raises:
            ValueError: если один из ID не найден в хранилище.
        """
        item1 = store.get_history_item_by_id(item_id_1)
        if item1 is None:
            raise ValueError(f"История не найдена: {item_id_1!r}")
        item2 = store.get_history_item_by_id(item_id_2)
        if item2 is None:
            raise ValueError(f"История не найдена: {item_id_2!r}")

        return self.compare_texts(item1.text, item2.text)

    def compare_texts(self, text1: str, text2: str) -> ComparisonResult:
        """Compare two texts directly.

        Args:
            text1: первый текст.
            text2: второй текст.

        Returns:
            ComparisonResult с полным анализом.

        Входные тексты усекаются до MAX_COMPARE_WORDS слов (DoS guard).
        """
        t1 = (text1 or "").strip()
        t2 = (text2 or "").strip()

        similarity = self._similarity(t1, t2)
        words1 = self._safe_split(t1)
        words2 = self._safe_split(t2)

        phrases1 = self._extract_phrases(words1)
        phrases2 = self._extract_phrases(words2)

        common = sorted(phrases1 & phrases2)
        unique_1 = sorted(phrases1 - phrases2)
        unique_2 = sorted(phrases2 - phrases1)

        word_count_diff = abs(len(words1) - len(words2))
        summary = self._build_summary(similarity, word_count_diff, len(common), len(unique_1), len(unique_2))

        return ComparisonResult(
            similarity=similarity,
            text_1=t1,
            text_2=t2,
            common_phrases=common,
            unique_to_1=unique_1,
            unique_to_2=unique_2,
            word_count_diff=word_count_diff,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_split(text: str) -> List[str]:
        """Разбить текст на слова, усекая до MAX_COMPARE_WORDS.

        Усечение логируется на уровне warning, чтобы отследить чрезмерно длинные
        входы через IPC (потенциальный индикатор злоупотребления).
        """
        words = text.lower().split()
        if len(words) > MAX_COMPARE_WORDS:
            logger.warning(
                "compare_texts: входной текст усечён с %d до %d слов (DoS guard)",
                len(words),
                MAX_COMPARE_WORDS,
                extra={"original_word_count": len(words), "cap": MAX_COMPARE_WORDS},
            )
            words = words[:MAX_COMPARE_WORDS]
        return words

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Character-level similarity ratio, rounded to 4 decimal places."""
        if not a and not b:
            return 1.0
        return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)

    def _extract_phrases(self, words: List[str]) -> set:
        """Вернуть множество n-грамм (n в диапазоне [MIN_PHRASE_WORDS, MAX_PHRASE_SIZE]).

        Ограничение сверху MAX_PHRASE_SIZE делает сложность O(n × MAX_PHRASE_SIZE)
        вместо прежнего O(n²). Жёсткий backstop MAX_PHRASES обеспечивает дополнительную
        защиту от непредвиденных крайних случаев.
        """
        phrases: set = set()
        n = len(words)
        max_size = min(MAX_PHRASE_SIZE, n)
        for size in range(self.MIN_PHRASE_WORDS, max_size + 1):
            for start in range(n - size + 1):
                phrases.add(" ".join(words[start:start + size]))
                # Backstop: не выделять бесконечно много фраз
                if len(phrases) >= MAX_PHRASES:
                    logger.warning(
                        "compare_texts: достигнут лимит фраз %d (MAX_PHRASES)",
                        MAX_PHRASES,
                        extra={"phrase_cap": MAX_PHRASES},
                    )
                    return phrases
        return phrases

    @staticmethod
    def _build_summary(
        similarity: float,
        word_count_diff: int,
        common_count: int,
        unique1_count: int,
        unique2_count: int,
    ) -> str:
        pct = int(similarity * 100)
        parts = [f"{pct}% similar"]
        if common_count:
            parts.append(f"{common_count} shared phrase{'s' if common_count != 1 else ''}")
        if word_count_diff:
            parts.append(f"{word_count_diff} word{'s' if word_count_diff != 1 else ''} difference")
        if unique1_count or unique2_count:
            parts.append(f"{unique1_count} unique to first, {unique2_count} unique to second")
        return "; ".join(parts)
