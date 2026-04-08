"""Тесты нормализации имён собственных и формата времени после Whisper."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import TextUtils


class EntityNormalizationTestCase(unittest.TestCase):
    """Whisper пишет бренды кириллицей; постпроцессинг возвращает латиницу."""

    def test_mercadona_cyrillic_to_latin(self) -> None:
        raw = "Сегодня я потратил 47 евро в Меркадонне."
        self.assertIn("Mercadona", TextUtils.cleanup_transcript(raw))

    def test_krab_ear_variants(self) -> None:
        for raw in ("CrabEar", "Краб Ир", "КрабИр"):
            with self.subTest(raw=raw):
                cleaned = TextUtils.cleanup_transcript(f"Запусти {raw} сейчас.")
                self.assertIn("Krab Ear", cleaned)

    def test_antigravity_and_hammerspoon(self) -> None:
        raw = "Запусти через Anti-Gravity Runtime, потому что Hammer Spoon не работает."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("Antigravity", cleaned)
        self.assertIn("Hammerspoon", cleaned)

    def test_pablito_name(self) -> None:
        cleaned = TextUtils.cleanup_transcript("Паблито, сходи в магазин.")
        self.assertTrue(cleaned.startswith("Pablito"))

    def test_time_format_dot_to_colon(self) -> None:
        raw = "Встреча в 15.00, выезд в 14.30."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("15:00", cleaned)
        self.assertIn("14:30", cleaned)

    def test_time_with_spaces_around_separator(self) -> None:
        # Whisper иногда ставит пробел: "15: 00" или "15 . 00"
        for raw in ("запустил в 15: 00", "запустил в 15 :00", "запустил в 15 . 00"):
            with self.subTest(raw=raw):
                self.assertIn("15:00", TextUtils.cleanup_transcript(raw))

    def test_time_ignores_non_time_numbers(self) -> None:
        # "3.14" — не время (час > 23 не бывает, но тут минуты 14 валидны → превратится).
        # Проверяем что явно не-время (например "100.50 евро") не трогаем.
        raw = "Цена 100.50 евро и версия 2.95."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("100.50", cleaned)
        self.assertIn("2.95", cleaned)

    def test_idempotent_on_already_latin(self) -> None:
        raw = "Krab Ear и Mercadona работают."
        self.assertEqual(
            TextUtils.cleanup_transcript(raw).rstrip("."),
            "Krab Ear и Mercadona работают",
        )

    def test_preserves_existing_cleanup(self) -> None:
        # Бренд-нормализация не должна ломать дедуп повторов.
        raw = "Меркадонна открыта. Меркадонна открыта."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("Mercadona", cleaned)
        self.assertEqual(cleaned.count("Mercadona"), 1)


if __name__ == "__main__":
    unittest.main()
