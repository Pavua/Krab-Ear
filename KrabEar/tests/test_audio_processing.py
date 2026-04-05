"""
Тесты для проверки базовой транскрибации и нормализации аудио в Krab Ear.
"""

import unittest
import numpy as np
from core.engine import AudioEngine

class TestAudioProcessing(unittest.TestCase):
    def setUp(self):
        self.engine = AudioEngine()

    def test_normalize_phrase(self):
        """Проверка нормализации текста для сравнения."""
        raw = "Привет, это ТЕСТ!"
        expected = "привет это тест"
        self.assertEqual(self.engine._normalize_phrase(raw), expected)

    def test_same_short_phrase(self):
        """Проверка детектора коротких повторяющихся фраз."""
        self.assertTrue(self.engine._same_short_phrase("Привет", "привет!"))
        self.assertFalse(self.engine._same_short_phrase("Очень длинная фраза, которая не должна считаться короткой", 
                                                        "Очень длинная фраза, которая не должна считаться короткой"))

    def test_cleanup_soft(self):
        """Проверка мягкой очистки от повторов."""
        text = "Я иду домой. Я иду домой."
        # _cleanup_soft должен убрать повтор в конце
        cleaned = self.engine._cleanup_soft(text)
        self.assertEqual(cleaned, "Я иду домой.")

    def test_normalize_audio_file_exists(self):
        """Проверка, что метод корректно обрабатывает отсутствие файла."""
        result = self.engine.normalize_audio("non_existent.wav")
        self.assertEqual(result, "non_existent.wav")

if __name__ == "__main__":
    unittest.main()
