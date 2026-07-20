"""Интеграционные тесты STT hotwords системы Krab Ear.

Тесты покрывают:
- add_stt_hotword / remove_stt_hotword / list_stt_hotwords IPC-методы
- Персистентность через SettingsService + StateStore
- Идемпотентность добавления и удаления
- Валидация входных данных (пустая строка, whitespace)
- Передача hotwords в initial_prompt Whisper через FakeTranscriber
- Флаг stt_hotwords_enabled
- Unicode-слова (кириллица, эмодзи, CJK)
- Обрезка списка при превышении лимита _STT_HOTWORDS_MAX
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.service import BackendService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def _speech_like_audio(duration_sec: float = 2.0, sample_rate: int = 16000):
    """Возвращает синусоидальное аудио с достаточным уровнем чтобы пройти silence guard."""
    import numpy as np
    n = int(duration_sec * sample_rate)
    t = np.linspace(0.0, duration_sec, n, endpoint=False, dtype=np.float32)
    carrier = np.sin(2.0 * np.pi * 200.0 * t)
    envelope = 0.45 + 0.55 * np.sin(2.0 * np.pi * 2.5 * t)
    wobble = 0.08 * np.sin(2.0 * np.pi * 25.0 * t)
    return (0.06 * carrier * envelope + wobble).astype(np.float32)


class FakeRecorder:
    is_recording = False
    sample_rate = 16000
    last_stop_trim_ms = 0
    last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        audio = _speech_like_audio(duration_sec=2.0)
        return audio, 2.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        return _speech_like_audio(duration_sec=2.0), 2.0


class CapturingTranscriber:
    """Записывает аргументы каждого вызова transcribe для проверки initial_prompt."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.counter = 0

    def transcribe(
        self,
        audio_data,
        quality_profile: str = "balanced",
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary=None,
        lang_hint=None,
        history_context=None,
        stt_hotwords=None,
        settings=None,  # W1707: accept settings kwarg added by recording_core_service
        diarize=None,
        silence_ranges=None,
        **kwargs,
    ) -> str:
        self.counter += 1
        self.calls.append(
            {
                "stt_hotwords": stt_hotwords,
                "quality_profile": quality_profile,
                "cleanup_profile": cleanup_profile,
            }
        )
        return f"тест#{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class FakeTranscriber:
    counter = 0

    def transcribe(self, audio_data, **kwargs) -> str:
        self.counter += 1
        return f"тест#{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class FakeTranslator:
    def translate(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict | None = None,
    ) -> TranslationResult:
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_service(test_case, tmp_dir: str, transcriber=None) -> BackendService:
    """Создать сервис и зарегистрировать закрытие раньше очистки каталога."""
    store = StateStore(Path(tmp_dir) / "data")
    service = BackendService(
        store=store,
        recorder=FakeRecorder(),
        transcriber=transcriber or FakeTranscriber(),
        translator=FakeTranslator(),
    )
    test_case.addCleanup(service.close)
    return service


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class SttHotwordsIntegrationTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = make_service(self, self.tmp.name)

    def req(self, method: str, params: dict | None = None) -> dict:
        return self.svc.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    # ------------------------------------------------------------------
    # 1. add -> list
    # ------------------------------------------------------------------
    def test_add_stt_hotword_persists(self):
        """Добавленное слово отображается в list_stt_hotwords."""
        add_resp = self.req("add_stt_hotword", {"word": "Антропик"})
        self.assertTrue(add_resp["ok"], add_resp)

        list_resp = self.req("list_stt_hotwords")
        self.assertTrue(list_resp["ok"])
        hotwords = list_resp["result"]["hotwords"]
        self.assertIn("Антропик", hotwords)

    # ------------------------------------------------------------------
    # 2. add -> remove -> list
    # ------------------------------------------------------------------
    def test_remove_stt_hotword_persists(self):
        """После remove слово исчезает из list."""
        self.req("add_stt_hotword", {"word": "Краб"})
        remove_resp = self.req("remove_stt_hotword", {"word": "Краб"})
        self.assertTrue(remove_resp["ok"], remove_resp)

        list_resp = self.req("list_stt_hotwords")
        hotwords = list_resp["result"]["hotwords"]
        self.assertNotIn("Краб", hotwords)

    # ------------------------------------------------------------------
    # 3. sorted order
    # ------------------------------------------------------------------
    def test_list_stt_hotwords_returns_sorted(self):
        """list_stt_hotwords возвращает слова в алфавитном порядке."""
        for w in ["Zebra", "Alpha", "Mango"]:
            self.req("add_stt_hotword", {"word": w})

        hotwords = self.req("list_stt_hotwords")["result"]["hotwords"]
        # Отфильтруем только наши три слова
        subset = [w for w in hotwords if w in {"Zebra", "Alpha", "Mango"}]
        self.assertEqual(sorted(subset), subset, f"Ожидали сортировку, получили: {subset}")

    # ------------------------------------------------------------------
    # 4. idempotent add
    # ------------------------------------------------------------------
    def test_add_duplicate_is_idempotent(self):
        """Повторное добавление того же слова не создаёт дублей."""
        self.req("add_stt_hotword", {"word": "Дубль"})
        self.req("add_stt_hotword", {"word": "Дубль"})
        self.req("add_stt_hotword", {"word": "Дубль"})

        hotwords = self.req("list_stt_hotwords")["result"]["hotwords"]
        count = hotwords.count("Дубль")
        self.assertEqual(count, 1, f"Ожидали ровно 1 вхождение, получили: {count}")

    # ------------------------------------------------------------------
    # 5. idempotent remove
    # ------------------------------------------------------------------
    def test_remove_missing_is_idempotent(self):
        """Удаление несуществующего слова возвращает ok=True без ошибки."""
        resp = self.req("remove_stt_hotword", {"word": "НеСуществует"})
        self.assertTrue(resp["ok"], resp)

    # ------------------------------------------------------------------
    # 6. empty string -> error
    # ------------------------------------------------------------------
    def test_add_empty_string_returns_error(self):
        """add_stt_hotword с пустой строкой возвращает ok=False."""
        resp = self.req("add_stt_hotword", {"word": ""})
        self.assertFalse(resp["ok"], f"Ожидали ошибку, получили: {resp}")

    # ------------------------------------------------------------------
    # 7. whitespace-only -> error
    # ------------------------------------------------------------------
    def test_add_whitespace_only_returns_error(self):
        """add_stt_hotword с whitespace-only строкой возвращает ok=False."""
        resp = self.req("add_stt_hotword", {"word": "   "})
        self.assertFalse(resp["ok"], f"Ожидали ошибку на whitespace, получили: {resp}")

    # ------------------------------------------------------------------
    # 8. persist across service restart
    # ------------------------------------------------------------------
    def test_hotwords_persist_across_service_restart(self):
        """Hotwords сохраняются в settings.json и доступны после пересоздания сервиса."""
        self.req("add_stt_hotword", {"word": "Персистент"})

        # Создаём НОВЫЙ сервис с той же директорией данных
        svc2 = make_service(self, self.tmp.name)
        resp = svc2.handle_request(
            {"id": "t2", "method": "list_stt_hotwords", "params": {}}
        )
        self.assertTrue(resp["ok"])
        hotwords = resp["result"]["hotwords"]
        self.assertIn("Персистент", hotwords, f"После рестарта: {hotwords}")

    # ------------------------------------------------------------------
    # 9. hotwords passed to transcriber as stt_hotwords
    # ------------------------------------------------------------------
    def test_hotwords_passed_to_whisper_initial_prompt(self):
        """Добавленные hotwords передаются в transcriber.transcribe() как stt_hotwords."""
        capturing = CapturingTranscriber()
        svc = make_service(self, self.tmp.name + "_cap", transcriber=capturing)

        # Добавляем hotword
        svc.handle_request(
            {"id": "a", "method": "add_stt_hotword", "params": {"word": "ПодсказкаSTT"}}
        )

        # Эмулируем запись: start + stop
        svc.handle_request({"id": "b", "method": "start_recording", "params": {}})
        svc.handle_request({"id": "c", "method": "stop_recording", "params": {}})

        # Должен быть хотя бы один вызов transcribe
        self.assertGreater(len(capturing.calls), 0, "transcribe не был вызван")

        # Последний вызов должен содержать наш hotword в stt_hotwords
        last_call = capturing.calls[-1]
        hw = last_call.get("stt_hotwords") or []
        self.assertIn(
            "ПодсказкаSTT", hw,
            f"'ПодсказкаSTT' не найден в stt_hotwords={hw}",
        )

    # ------------------------------------------------------------------
    # 10. hotwords cleared when disabled via stt_hotwords_enabled=False
    # ------------------------------------------------------------------
    def test_hotwords_cleared_when_disabled(self):
        """list_stt_hotwords возвращает [] когда stt_hotwords_enabled=False."""
        self.req("add_stt_hotword", {"word": "ВидимоеСлово"})

        # Убеждаемся что слово добавлено
        hotwords_before = self.req("list_stt_hotwords")["result"]["hotwords"]
        self.assertIn("ВидимоеСлово", hotwords_before)

        # Отключаем hotwords
        set_resp = self.req("set_settings", {"stt_hotwords_enabled": False})
        self.assertTrue(set_resp["ok"], set_resp)

        # Теперь list должен вернуть пустой список
        list_resp = self.req("list_stt_hotwords")
        self.assertTrue(list_resp["ok"])
        result = list_resp["result"]
        self.assertEqual(result["hotwords"], [], f"Ожидали [], получили: {result['hotwords']}")
        self.assertFalse(result.get("enabled", True), "enabled должен быть False")

    # ------------------------------------------------------------------
    # 11. unicode hotwords: Cyrillic, emoji, Chinese
    # ------------------------------------------------------------------
    def test_unicode_hotwords_supported(self):
        """Кириллица, эмодзи и китайские символы сохраняются корректно."""
        unicode_words = ["Привет", "🐙", "你好世界", "señor", "ñoño"]
        for w in unicode_words:
            resp = self.req("add_stt_hotword", {"word": w})
            self.assertTrue(resp["ok"], f"Не удалось добавить '{w}': {resp}")

        hotwords = self.req("list_stt_hotwords")["result"]["hotwords"]
        for w in unicode_words:
            self.assertIn(w, hotwords, f"'{w}' не найден после добавления")

    # ------------------------------------------------------------------
    # 12. long hotword list truncated to _STT_HOTWORDS_MAX
    # ------------------------------------------------------------------
    def test_long_hotword_list_truncated_to_whisper_limit(self):
        """При добавлении >_STT_HOTWORDS_MAX слов список обрезается до лимита,
        oldest entries удаляются, в ответе truncated=True."""
        # Constant moved to STTManagementService after service extraction (Wave 392)
        from backend.stt_management_service import _STT_HOTWORDS_MAX
        max_limit = _STT_HOTWORDS_MAX

        # Добавляем max_limit слов
        for i in range(max_limit):
            self.req("add_stt_hotword", {"word": f"word_{i:04d}"})

        # Все max_limit слов должны присутствовать
        hotwords = self.req("list_stt_hotwords")["result"]["hotwords"]
        self.assertLessEqual(len(hotwords), max_limit)

        # Добавляем одно слово сверх лимита
        resp = self.req("add_stt_hotword", {"word": "OVERFLOW_WORD"})
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]

        # Список должен быть обрезан до лимита
        self.assertLessEqual(
            len(result["hotwords"]),
            max_limit,
            f"Ожидали <= {max_limit} слов, получили: {len(result['hotwords'])}",
        )

        # truncated=True когда был overflow
        self.assertTrue(
            result.get("truncated", False),
            f"Ожидали truncated=True, получили result={result}",
        )

        # Последнее добавленное слово (OVERFLOW_WORD) присутствует
        self.assertIn(
            "OVERFLOW_WORD",
            result["hotwords"],
            "Новое слово должно быть в списке после усечения",
        )

        # Первые слова (word_0000, ...) были вытеснены (oldest dropped)
        self.assertNotIn(
            "word_0000",
            result["hotwords"],
            "Старейшее слово должно быть вытеснено при overflow",
        )

    # ------------------------------------------------------------------
    # Bonus: list returns enabled=True when hotwords_enabled=True (default)
    # ------------------------------------------------------------------
    def test_list_returns_enabled_true_by_default(self):
        """list_stt_hotwords включает поле enabled=True по умолчанию."""
        self.req("add_stt_hotword", {"word": "ТестДефолт"})
        result = self.req("list_stt_hotwords")["result"]
        self.assertTrue(
            result.get("enabled", True),
            f"enabled должен быть True по умолчанию, result={result}",
        )

    # ------------------------------------------------------------------
    # Bonus: re-enable hotwords after disable restores words
    # ------------------------------------------------------------------
    def test_hotwords_restored_after_re_enable(self):
        """После re-enable (stt_hotwords_enabled=True) слова снова видны."""
        self.req("add_stt_hotword", {"word": "ВосстановленноеСлово"})
        self.req("set_settings", {"stt_hotwords_enabled": False})
        self.req("set_settings", {"stt_hotwords_enabled": True})

        hotwords = self.req("list_stt_hotwords")["result"]["hotwords"]
        self.assertIn(
            "ВосстановленноеСлово",
            hotwords,
            f"Слово должно вернуться после re-enable: {hotwords}",
        )

    # ------------------------------------------------------------------
    # Bonus: whitespace stripped from word on add
    # ------------------------------------------------------------------
    def test_add_strips_leading_trailing_whitespace(self):
        """Слово с пробелами по краям сохраняется без пробелов."""
        resp = self.req("add_stt_hotword", {"word": "  Обрезка  "})
        self.assertTrue(resp["ok"])
        hotwords = self.req("list_stt_hotwords")["result"]["hotwords"]
        self.assertIn("Обрезка", hotwords)
        self.assertNotIn("  Обрезка  ", hotwords)


if __name__ == "__main__":
    unittest.main(verbosity=2)
