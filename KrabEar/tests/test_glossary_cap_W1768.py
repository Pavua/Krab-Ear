"""W1768 — лимиты глоссария перевода (защита от unbounded-growth DoS).

Проверяет, что TranslationService ограничивает рост translation_glossary:
- число пар не превышает MAX_GLOSSARY_ENTRIES (501-я новая запись отклоняется);
- длина source/target не превышает MAX_TERM_BYTES (в байтах UTF-8);
- обычное добавление и обновление существующего ключа работают как прежде;
- bulk-помощник enforce_glossary_caps (логика import_glossary_csv) усекает
  пакет на лимите и возвращает ошибку переполнения, отклоняя over-long термины.

Fail-before / pass-after: до фикса (без лимитов) 501-я запись и over-long
термин персистились бы; после фикса они отклоняются структурной ошибкой.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.translation_service import TranslationService


# ──────────────────────────────────────────────────────────────
# Helpers / fakes
# ──────────────────────────────────────────────────────────────

def _make_service(
    glossary: dict[str, str] | None = None,
) -> tuple[TranslationService, MagicMock]:
    """TranslationService с мокированным store (fallback-путь, без settings_svc).

    store.save_settings возвращает переданный dict как есть — итоговый размер
    глоссария считается по сохранённому объекту. cached_settings отражает
    текущее состояние глоссария (для проверки is_new_key и лимита размера).
    """
    state: dict[str, Any] = {
        "network_mode": "offline_default",
        "translation_glossary": dict(glossary or {}),
    }

    store = MagicMock()

    def _save(settings: dict[str, Any]) -> dict[str, Any]:
        # Эмулируем персистенцию: обновляем состояние, отражаемое cached_settings.
        state["translation_glossary"] = dict(
            settings.get("translation_glossary", {}) or {}
        )
        return settings

    store.save_settings.side_effect = _save

    def cached_settings() -> dict[str, Any]:
        return dict(state)

    svc = TranslationService(
        translator=MagicMock(),
        store=store,
        cached_settings=cached_settings,
        invalidate_settings_cache=lambda: None,
    )
    return svc, store


# ──────────────────────────────────────────────────────────────
# Entry-count cap
# ──────────────────────────────────────────────────────────────

class GlossaryEntryCapTestCase(unittest.TestCase):
    """MAX_GLOSSARY_ENTRIES — лимит числа пар."""

    def test_501st_new_entry_rejected_and_not_persisted(self) -> None:
        """501-я НОВАЯ пара отклоняется структурной ошибкой и не сохраняется."""
        full = {f"src{i}": f"tgt{i}" for i in range(TranslationService.MAX_GLOSSARY_ENTRIES)}
        self.assertEqual(len(full), 500)
        svc, store = _make_service(glossary=full)

        result = svc.handle_set_translation_glossary_item({
            "source": "overflow_src",
            "target": "overflow_tgt",
        })

        # Ошибка возвращена, updated=False
        self.assertFalse(result["updated"])
        self.assertIn("error", result)
        self.assertIn("entry limit", result["error"])
        # Ничего не персистировано
        store.save_settings.assert_not_called()

    def test_500th_new_entry_still_accepted(self) -> None:
        """Ровно 500-я запись (последняя в пределах лимита) добавляется."""
        almost = {f"src{i}": f"tgt{i}" for i in range(TranslationService.MAX_GLOSSARY_ENTRIES - 1)}
        self.assertEqual(len(almost), 499)
        svc, store = _make_service(glossary=almost)

        result = svc.handle_set_translation_glossary_item({
            "source": "src499",
            "target": "tgt499",
        })

        self.assertTrue(result["updated"])
        self.assertEqual(result["count"], 500)
        store.save_settings.assert_called_once()

    def test_update_existing_key_allowed_at_cap(self) -> None:
        """Обновление существующего ключа на пределе разрешено (размер не растёт)."""
        full = {f"src{i}": f"tgt{i}" for i in range(TranslationService.MAX_GLOSSARY_ENTRIES)}
        svc, store = _make_service(glossary=full)

        result = svc.handle_set_translation_glossary_item({
            "source": "src0",          # уже существует
            "target": "updated_value",
        })

        self.assertTrue(result["updated"])
        self.assertEqual(result["count"], 500)  # размер не изменился
        store.save_settings.assert_called_once()
        saved = store.save_settings.call_args[0][0]
        self.assertEqual(saved["translation_glossary"]["src0"], "updated_value")


# ──────────────────────────────────────────────────────────────
# Term-length cap
# ──────────────────────────────────────────────────────────────

class GlossaryTermLengthCapTestCase(unittest.TestCase):
    """MAX_TERM_BYTES — лимит длины source/target (байты UTF-8)."""

    def test_over_long_source_rejected(self) -> None:
        """source длиннее MAX_TERM_BYTES отклоняется и не сохраняется."""
        svc, store = _make_service()
        long_source = "x" * (TranslationService.MAX_TERM_BYTES + 1)

        result = svc.handle_set_translation_glossary_item({
            "source": long_source,
            "target": "ok",
        })

        self.assertFalse(result["updated"])
        self.assertIn("error", result)
        self.assertIn("source", result["error"])
        store.save_settings.assert_not_called()

    def test_over_long_target_rejected(self) -> None:
        """target длиннее MAX_TERM_BYTES отклоняется и не сохраняется."""
        svc, store = _make_service()
        long_target = "y" * (TranslationService.MAX_TERM_BYTES + 1)

        result = svc.handle_set_translation_glossary_item({
            "source": "ok",
            "target": long_target,
        })

        self.assertFalse(result["updated"])
        self.assertIn("error", result)
        self.assertIn("target", result["error"])
        store.save_settings.assert_not_called()

    def test_cyrillic_length_counted_in_bytes(self) -> None:
        """Кириллица (2 байта/символ) считается по байтам, а не символам.

        100 кириллических символов = 200 байт = ровно на пределе → принимается.
        101 символ = 202 байта → отклоняется.
        """
        # 100 кириллических символов = 200 байт = на границе (разрешено)
        on_edge = "а" * (TranslationService.MAX_TERM_BYTES // 2)
        self.assertEqual(len(on_edge.encode("utf-8")), TranslationService.MAX_TERM_BYTES)
        svc, store = _make_service()
        result_ok = svc.handle_set_translation_glossary_item({
            "source": on_edge,
            "target": "ok",
        })
        self.assertTrue(result_ok["updated"])

        # 101 символ = 202 байта → за пределом (отклонено)
        over = "а" * (TranslationService.MAX_TERM_BYTES // 2 + 1)
        self.assertGreater(len(over.encode("utf-8")), TranslationService.MAX_TERM_BYTES)
        svc2, store2 = _make_service()
        result_over = svc2.handle_set_translation_glossary_item({
            "source": over,
            "target": "ok",
        })
        self.assertFalse(result_over["updated"])
        store2.save_settings.assert_not_called()


# ──────────────────────────────────────────────────────────────
# Normal behaviour preserved
# ──────────────────────────────────────────────────────────────

class GlossaryNormalAddTestCase(unittest.TestCase):
    """Обычное добавление по-прежнему работает (регрессия)."""

    def test_normal_add_succeeds(self) -> None:
        """Короткая пара в пустой глоссарий добавляется и персистится."""
        svc, store = _make_service()

        result = svc.handle_set_translation_glossary_item({
            "source": "Краб",
            "target": "Krab",
        })

        self.assertTrue(result["updated"])
        self.assertEqual(result["count"], 1)
        store.save_settings.assert_called_once()
        saved = store.save_settings.call_args[0][0]
        self.assertEqual(saved["translation_glossary"]["Краб"], "Krab")

    def test_missing_source_still_raises(self) -> None:
        """Пустой source по-прежнему RuntimeError (поведение не изменено)."""
        svc, _ = _make_service()
        with self.assertRaises(RuntimeError):
            svc.handle_set_translation_glossary_item({"source": "", "target": "Krab"})


# ──────────────────────────────────────────────────────────────
# Bulk import enforcement (логика import_glossary_csv)
# ──────────────────────────────────────────────────────────────

class GlossaryBulkCapTestCase(unittest.TestCase):
    """enforce_glossary_caps — массовое добавление усекается на лимите."""

    def test_bulk_truncates_past_entry_cap_with_error(self) -> None:
        """Пакет из 600 пар усекается до MAX_GLOSSARY_ENTRIES, ошибка переполнения."""
        additions = {f"src{i}": f"tgt{i}" for i in range(600)}
        merged, rejected, error = TranslationService.enforce_glossary_caps({}, additions)

        # Принято ровно до лимита
        self.assertEqual(len(merged), TranslationService.MAX_GLOSSARY_ENTRIES)
        # Излишек отклонён
        self.assertEqual(len(rejected), 600 - TranslationService.MAX_GLOSSARY_ENTRIES)
        # Зафиксирована ошибка переполнения
        self.assertIsNotNone(error)
        self.assertIn("entry limit", error)

    def test_bulk_respects_existing_entries(self) -> None:
        """Лимит считается от существующего размера: cap - existing новых влезает."""
        existing = {f"old{i}": f"v{i}" for i in range(490)}
        additions = {f"new{i}": f"w{i}" for i in range(50)}  # влезет только 10
        merged, rejected, error = TranslationService.enforce_glossary_caps(existing, additions)

        self.assertEqual(len(merged), TranslationService.MAX_GLOSSARY_ENTRIES)
        self.assertEqual(len(rejected), 40)  # 50 - 10 принятых
        self.assertIsNotNone(error)

    def test_bulk_rejects_over_long_terms_but_keeps_importing(self) -> None:
        """Over-long пары пропускаются (rejected), остальные импортируются."""
        long_src = "z" * (TranslationService.MAX_TERM_BYTES + 1)
        additions = {
            "good1": "v1",
            long_src: "v2",          # отклонится по длине source
            "good2": "v3",
            "good3": "z" * (TranslationService.MAX_TERM_BYTES + 1),  # длинный target
        }
        merged, rejected, error = TranslationService.enforce_glossary_caps({}, additions)

        # Приняты только короткие пары
        self.assertIn("good1", merged)
        self.assertIn("good2", merged)
        self.assertNotIn(long_src, merged)
        self.assertNotIn("good3", merged)
        self.assertIn(long_src, rejected)
        self.assertIn("good3", rejected)
        # Переполнения числа записей не было
        self.assertIsNone(error)

    def test_bulk_within_limits_accepts_all(self) -> None:
        """Пакет в пределах лимитов принимается полностью, без ошибок."""
        additions = {f"src{i}": f"tgt{i}" for i in range(100)}
        merged, rejected, error = TranslationService.enforce_glossary_caps({}, additions)

        self.assertEqual(len(merged), 100)
        self.assertEqual(rejected, [])
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
