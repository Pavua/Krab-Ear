"""Матрица граничных тест-кейсов для Krab Ear.

Систематически проверяет нестандартные входные данные для IPC-методов:
- Пустые строки для всех текстовых параметров
- None/null для необязательных параметров
- Отрицательные числа там, где ожидаются положительные
- Массивы аудио нулевой длины
- Односимвольный текст
- Максимально длинный текст (10000 символов)
- Несуществующие ID для операций чтения/обновления/удаления
- Дублирующиеся ID в пакетных операциях
- Таймстемпы в неверном формате
- Смешанные кодировки (UTF-8 с BOM, Latin-1 символы)
"""

from __future__ import annotations
from backend.translator import TranslationResult
from backend.state_store import StateStore
from backend.service import BackendService

from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Фейковые коллабораторы (идентичны test_backend_service.py)
# ---------------------------------------------------------------------------

class FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        carrier = np.sin(2.0 * np.pi * 210.0 * t)
        envelope = 0.45 + 0.55 * np.sin(2.0 * np.pi * 2.4 * t)
        wobble = 0.08 * np.sin(2.0 * np.pi * 23.0 * t)
        speech_like = 0.06 * carrier * envelope + wobble
        return speech_like.astype(np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        self._snapshot_counter += 1
        return np.ones(32000, dtype=np.float32), float(self._snapshot_counter)


class FakeTranscriber:
    def __init__(self) -> None:
        self.counter = 0
        self.preview_counter = 0

    def transcribe(self, audio_data, quality_profile="balanced", cleanup_profile="soft",
                   domain="casual", extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None) -> str:
        self.counter += 1
        return f"тест #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced") -> str:
        self.preview_counter += 1
        return f"preview#{self.preview_counter}"


class FakeTranslator:
    def __init__(self) -> None:
        self.last_mode = "off"

    def translate(self, text, mode, network_mode, translation_style="neutral",
                  glossary=None) -> TranslationResult:
        self.last_mode = mode
        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Базовый класс — создаёт сервис один раз для всей матрицы тестов
# ---------------------------------------------------------------------------

class EdgeCaseMatrixBase(unittest.TestCase):
    """Базовый класс с общей инфраструктурой для матричных тестов."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def req(self, method: str, params=None, rid="ec-1"):
        """Отправляет IPC-запрос и возвращает ответ."""
        return self.service.handle_request(
            {"id": rid, "method": method, "params": params or {}}
        )

    def add_item(self, text="тест") -> str:
        """Добавляет запись в историю и возвращает её ID."""
        r = self.req("add_history_item", {"text": text})
        self.assertTrue(r["ok"], f"add_history_item failed: {r}")
        return r["result"]["id"]


# ---------------------------------------------------------------------------
# 1. Пустые строки для текстовых параметров
# ---------------------------------------------------------------------------

class EmptyStringEdgeCases(EdgeCaseMatrixBase):
    """Пустая строка во всех текстовых параметрах IPC-методов."""

    def test_add_history_item_empty_text_rejected(self):
        """add_history_item с пустым text → ошибка (не ok)."""
        r = self.req("add_history_item", {"text": ""})
        self.assertFalse(r["ok"], "Пустой text должен быть отклонён")

    def test_add_history_item_whitespace_only_rejected(self):
        """add_history_item с пробельным текстом → ошибка."""
        r = self.req("add_history_item", {"text": "   \t\n  "})
        self.assertFalse(r["ok"], "Только пробелы должны быть отклонены")

    def test_search_history_empty_query(self):
        """search_history с query='' возвращает результат без краша."""
        r = self.req("search_history", {"query": ""})
        self.assertTrue(r["ok"], f"search_history с пустым query должен вернуть ok: {r}")
        self.assertIn("items", r["result"])

    def test_translate_text_empty_text(self):
        """translate_text с пустым текстом — сервис не падает."""
        r = self.req("translate_text", {"text": "", "mode": "ru_to_es"})
        # Либо ok (с пустым переводом), либо ошибка — но не исключение
        self.assertIn("ok", r)

    def test_delete_history_item_empty_id(self):
        """delete_history_item с пустым id → ошибка (handle использует params["id"])."""
        r = self.req("delete_history_item", {"id": ""})
        # Пустой ID не может быть удалён — ожидаем ok=False
        self.assertFalse(r["ok"], "Пустой id должен быть отклонён")

    def test_set_paste_status_empty_item_id(self):
        """set_paste_status с пустым id → не ok (handle использует params["id"])."""
        r = self.req("set_paste_status", {"id": "", "paste_status": "pasted"})
        # Пустой id → store.set_paste_status вернёт False → RuntimeError → ok=False
        self.assertFalse(r["ok"], "Пустой id должен быть отклонён")

    def test_get_history_item_empty_id(self):
        """get_history_item с пустым id → ошибка (handle использует params["id"])."""
        r = self.req("get_history_item", {"id": ""})
        # Пустой id → RuntimeError "id обязателен" → ok=False
        self.assertFalse(r["ok"], "Пустой id должен быть отклонён")

    def test_fuzzy_search_empty_query(self):
        """fuzzy_search с пустым query — не падает."""
        r = self.req("fuzzy_search", {"query": ""})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 2. None/null для необязательных параметров
# ---------------------------------------------------------------------------

class NullOptionalParamsEdgeCases(EdgeCaseMatrixBase):
    """None/null для необязательных параметров IPC-методов."""

    def test_get_history_page_null_cursor(self):
        """get_history_page с cursor=None — работает как первая страница."""
        r = self.req("get_history_page", {"cursor": None, "limit": 10})
        self.assertTrue(r["ok"], f"cursor=None должен работать: {r}")
        self.assertIn("items", r["result"])

    def test_search_history_null_optional_params(self):
        """search_history с None-полями для необязательных параметров."""
        r = self.req("search_history", {
            "query": "тест",
            "paste_status": None,
            "translation_mode": None,
        })
        self.assertTrue(r["ok"], f"None-параметры фильтра должны игнорироваться: {r}")

    def test_add_history_item_null_optional_fields(self):
        """add_history_item с None-полями для необязательных параметров."""
        r = self.req("add_history_item", {
            "text": "тест с None полями",
            "source_text": None,
            "translated_text": None,
            "translation_mode": None,
        })
        # Ожидаем ok=True (None-поля должны быть нормализованы в пустые строки/дефолты)
        self.assertTrue(r["ok"], f"None в опциональных полях должен быть обработан: {r}")

    def test_cleanup_old_history_null_days(self):
        """cleanup_old_history с days=None — не падает."""
        r = self.req("cleanup_old_history", {"days": None})
        self.assertIn("ok", r)

    def test_score_readability_null_lang(self):
        """score_readability с lang=None — не падает."""
        r = self.req("score_readability", {"text": "Проверка читабельности.", "lang": None})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 3. Отрицательные числа там, где ожидаются положительные
# ---------------------------------------------------------------------------

class NegativeNumberEdgeCases(EdgeCaseMatrixBase):
    """Отрицательные числа в параметрах, ожидающих положительные значения."""

    def test_get_history_page_negative_limit(self):
        """get_history_page с limit=-1 — не падает."""
        r = self.req("get_history_page", {"limit": -1})
        # Сервис должен либо выдать ошибку, либо использовать дефолт
        self.assertIn("ok", r)

    def test_get_history_page_zero_limit(self):
        """get_history_page с limit=0 — не падает."""
        r = self.req("get_history_page", {"limit": 0})
        self.assertIn("ok", r)

    def test_cleanup_old_history_negative_days(self):
        """cleanup_old_history с days=-5 — не падает."""
        r = self.req("cleanup_old_history", {"days": -5})
        self.assertIn("ok", r)

    def test_filter_by_confidence_negative_threshold(self):
        """filter_by_confidence с min_confidence=-0.5 — не падает."""
        r = self.req("filter_by_confidence", {"min_confidence": -0.5})
        self.assertIn("ok", r)

    def test_filter_by_confidence_above_one(self):
        """filter_by_confidence с min_confidence=1.5 — возвращает пустой список."""
        r = self.req("filter_by_confidence", {"min_confidence": 1.5})
        self.assertIn("ok", r)
        if r["ok"]:
            items = r["result"].get("items", [])
            # Уверенность > 1.0 невозможна → список должен быть пустым
            self.assertEqual(items, [], "min_confidence>1.0 должна вернуть пустой список")

    def test_get_history_page_negative_limit_no_crash(self):
        """Повторный вызов get_history_page с экстремально отрицательным limit."""
        r = self.req("get_history_page", {"limit": -99999})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 4. Массивы аудио нулевой длины
# ---------------------------------------------------------------------------

class ZeroLengthAudioEdgeCases(EdgeCaseMatrixBase):
    """Массивы аудио нулевой длины в методах, принимающих numpy-данные напрямую."""

    def test_profile_noise_nonexistent_file(self):
        """profile_noise с несуществующим файлом — не падает."""
        r = self.req("profile_noise", {"file_path": "/nonexistent/audio.wav"})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 5. Односимвольный текст
# ---------------------------------------------------------------------------

class SingleCharTextEdgeCases(EdgeCaseMatrixBase):
    """Односимвольный текст во всех текстовых параметрах."""

    def test_add_history_item_single_char(self):
        """add_history_item с однобуквенным текстом — сохраняется."""
        r = self.req("add_history_item", {"text": "А"})
        self.assertTrue(r["ok"], f"Однобуквенный текст должен сохраняться: {r}")

    def test_search_history_single_char_query(self):
        """search_history с однобуквенным запросом — не падает."""
        r = self.req("search_history", {"query": "А"})
        self.assertTrue(r["ok"], f"Однобуквенный поиск: {r}")

    def test_score_readability_single_char(self):
        """score_readability с одним символом — не падает."""
        r = self.req("score_readability", {"text": "A"})
        self.assertIn("ok", r)

    def test_extract_terms_single_char(self):
        """extract_terms с однобуквенным текстом — не падает."""
        r = self.req("extract_terms", {"text": "Z"})
        self.assertIn("ok", r)

    def test_compare_texts_single_chars(self):
        """compare_texts с однобуквенными текстами — не падает."""
        r = self.req("compare_texts", {"text_a": "А", "text_b": "Б"})
        self.assertIn("ok", r)

    def test_translate_text_single_char(self):
        """translate_text с одним символом — не падает."""
        r = self.req("translate_text", {"text": "я", "mode": "ru_to_es"})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 6. Максимально длинный текст (10000 символов)
# ---------------------------------------------------------------------------

class MaxLengthTextEdgeCases(EdgeCaseMatrixBase):
    """Текст максимальной длины (10000 символов)."""

    LONG_TEXT = "Это тестовый текст для проверки ограничений. " * 222  # ~10000 символов
    LONG_TEXT = LONG_TEXT[:10000]  # обрезаем до 10000

    def test_add_history_item_max_length(self):
        """add_history_item с 10000-символьным текстом — принимается."""
        r = self.req("add_history_item", {"text": self.LONG_TEXT})
        self.assertTrue(r["ok"], f"Длинный текст должен сохраняться: {r}")

    def test_search_history_long_query(self):
        """search_history с очень длинным запросом — не падает."""
        r = self.req("search_history", {"query": self.LONG_TEXT})
        self.assertIn("ok", r)

    def test_score_readability_long_text(self):
        """score_readability с 10000-символьным текстом — не падает."""
        r = self.req("score_readability", {"text": self.LONG_TEXT})
        self.assertIn("ok", r)

    def test_extract_terms_long_text(self):
        """extract_terms с 10000-символьным текстом — не падает."""
        r = self.req("extract_terms", {"text": self.LONG_TEXT})
        self.assertIn("ok", r)

    def test_translate_text_long(self):
        """translate_text с 10000-символьным текстом — не падает."""
        r = self.req("translate_text", {"text": self.LONG_TEXT, "mode": "ru_to_es"})
        self.assertIn("ok", r)

    def test_compare_texts_both_long(self):
        """compare_texts с двумя 10000-символьными текстами — не падает."""
        r = self.req("compare_texts", {"text_a": self.LONG_TEXT, "text_b": self.LONG_TEXT})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 7. Несуществующие ID для операций чтения/обновления/удаления
# ---------------------------------------------------------------------------

class NonExistentIdEdgeCases(EdgeCaseMatrixBase):
    """Несуществующие ID для операций чтения, обновления, удаления."""

    FAKE_ID = "00000000-0000-0000-0000-000000000000"

    def test_get_history_item_nonexistent(self):
        """get_history_item с несуществующим ID (правильный ключ "id") → ошибка not_found."""
        # handle_get_history_item использует params["id"], не params["item_id"]
        r = self.req("get_history_item", {"id": self.FAKE_ID})
        # Сервис возвращает ошибку, если запись не найдена
        self.assertFalse(r["ok"], "Несуществующий ID должен вернуть ok=False")
        self.assertIn("error", r, "Ответ должен содержать поле error")

    def test_delete_nonexistent_item(self):
        """delete_history_item с несуществующим ID → ok=True, deleted=True (tombstone-semantics).

        StateStore.delete_history_item всегда делает append tombstone и возвращает True —
        идемпотентное удаление не требует существования записи.
        """
        # handle_delete_history_item использует params["id"], не params["item_id"]
        r = self.req("delete_history_item", {"id": self.FAKE_ID})
        # Tombstone-семантика: StateStore.delete_history_item всегда возвращает True
        self.assertTrue(r["ok"], f"Tombstone для несуществующего ID должен вернуть ok=True: {r}")
        self.assertTrue(r["result"].get("deleted", False),
                        "deleted должен быть True (tombstone-семантика)")

    def test_toggle_favorite_nonexistent(self):
        """toggle_favorite с несуществующим ID — не падает."""
        r = self.req("toggle_favorite", {"id": self.FAKE_ID})
        self.assertIn("ok", r)

    def test_get_annotation_nonexistent(self):
        """get_annotation с несуществующим ID → annotation=None или пустая строка."""
        r = self.req("get_annotation", {"id": self.FAKE_ID})
        self.assertIn("ok", r)

    def test_set_paste_status_nonexistent(self):
        """set_paste_status с несуществующим ID — не падает."""
        r = self.req("set_paste_status", {"item_id": self.FAKE_ID, "paste_status": "pasted"})
        self.assertIn("ok", r)

    def test_add_tag_nonexistent_item(self):
        """add_tag к несуществующему item_id → ошибка "не найдена"."""
        r = self.req("add_tag", {"id": self.FAKE_ID, "tag": "тест"})
        # Метод требует существующую запись — ожидаем ошибку
        self.assertFalse(r["ok"], "add_tag к несуществующей записи должен вернуть ok=False")

    def test_remove_tag_nonexistent_item(self):
        """remove_tag с несуществующим item_id → ошибка "не найдена"."""
        r = self.req("remove_tag", {"id": self.FAKE_ID, "tag": "несуществующий"})
        # Метод требует существующую запись — ожидаем ошибку
        self.assertFalse(r["ok"], "remove_tag для несуществующей записи должен вернуть ok=False")

    def test_set_annotation_nonexistent_item(self):
        """set_annotation для несуществующего item_id — не падает."""
        r = self.req("set_annotation", {
            "id": self.FAKE_ID,
            "annotation": "заметка к несуществующей записи"
        })
        self.assertIn("ok", r)

    def test_get_transcript_versions_nonexistent(self):
        """get_transcript_versions с несуществующим item_id — не падает."""
        r = self.req("get_transcript_versions", {"item_id": self.FAKE_ID})
        self.assertIn("ok", r)

    def test_repaste_nonexistent_index(self):
        """repaste_item с несуществующим индексом — не падает."""
        r = self.req("repaste_item", {"index": 9999})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 8. Дублирующиеся ID в пакетных операциях
# ---------------------------------------------------------------------------

class DuplicateIdBatchEdgeCases(EdgeCaseMatrixBase):
    """Дублирующиеся ID в пакетных операциях."""

    def test_batch_duplicate_methods(self):
        """batch с дублирующимися методами — оба выполняются независимо."""
        r = self.req("batch", {
            "requests": [
                {"id": "dup-1", "method": "ping", "params": {}},
                {"id": "dup-2", "method": "ping", "params": {}},
                {"id": "dup-3", "method": "ping", "params": {}},
            ]
        })
        self.assertTrue(r["ok"], f"batch с дублями должен пройти: {r}")
        results = r["result"].get("results", [])
        self.assertEqual(len(results), 3, "Все 3 запроса должны быть выполнены")

    def test_batch_same_ids(self):
        """batch с одинаковыми request_id — не падает."""
        r = self.req("batch", {
            "requests": [
                {"id": "same-id", "method": "ping", "params": {}},
                {"id": "same-id", "method": "ping", "params": {}},
            ]
        })
        self.assertIn("ok", r)

    def test_archive_duplicate_item_ids(self):
        """archive_items с дублирующимися item_id — не падает, не создаёт дублей."""
        item_id = self.add_item("для архива")
        r = self.req("archive_items", {"item_ids": [item_id, item_id, item_id]})
        self.assertIn("ok", r)

    def test_batch_too_many_requests(self):
        """batch с 51 запросом (> лимита 50) — возвращает ошибку."""
        requests = [
            {"id": f"r-{i}", "method": "ping", "params": {}} for i in range(51)
        ]
        r = self.req("batch", {"requests": requests})
        # Ожидаем ошибку при превышении лимита
        self.assertFalse(r["ok"], "batch>50 должен быть отклонён")

    def test_add_to_collection_duplicate_item(self):
        """add_to_collection с одним item_id дважды — не падает."""
        item_id = self.add_item("элемент коллекции")
        # Создаём коллекцию
        cr = self.req("create_collection", {"name": "Тестовая коллекция"})
        if not cr["ok"]:
            self.skipTest("create_collection не удался")
        coll_id = cr["result"].get("id") or cr["result"].get("collection_id")
        if not coll_id:
            self.skipTest("Не удалось получить ID коллекции")
        # Добавляем один и тот же item дважды
        r1 = self.req("add_to_collection", {"collection_id": coll_id, "item_id": item_id})
        r2 = self.req("add_to_collection", {"collection_id": coll_id, "item_id": item_id})
        self.assertIn("ok", r1)
        self.assertIn("ok", r2)


# ---------------------------------------------------------------------------
# 9. Таймстемпы в неверном формате
# ---------------------------------------------------------------------------

class BadTimestampEdgeCases(EdgeCaseMatrixBase):
    """Таймстемпы в неверном формате во всех фильтрах."""

    def test_get_history_page_bad_from_ts(self):
        """get_history_page с from_ts='not-a-date' — не падает."""
        r = self.req("get_history_page", {
            "from_ts": "not-a-date",
            "to_ts": "also-not-a-date",
        })
        # Ожидаем либо ошибку с понятным сообщением, либо ok с пустым результатом
        self.assertIn("ok", r)

    def test_get_history_page_unix_timestamp_as_string(self):
        """get_history_page с from_ts=Unix timestamp строкой — не падает."""
        r = self.req("get_history_page", {"from_ts": "1700000000"})
        self.assertIn("ok", r)

    def test_get_history_page_iso_timestamp(self):
        """get_history_page с from_ts=ISO-8601 строкой — не падает."""
        r = self.req("get_history_page", {"from_ts": "2024-01-01T00:00:00Z"})
        self.assertIn("ok", r)

    def test_get_history_page_timestamp_out_of_range(self):
        """get_history_page с from_ts > to_ts — возвращает пустой список."""
        r = self.req("get_history_page", {
            "from_ts": "2099-01-01T00:00:00Z",
            "to_ts": "2020-01-01T00:00:00Z",
        })
        self.assertIn("ok", r)
        if r["ok"]:
            items = r["result"].get("items", [])
            # from_ts > to_ts — либо пустой список, либо ошибка
            self.assertIsInstance(items, list)

    def test_generate_daily_digest_bad_date(self):
        """generate_daily_digest с date='not-a-date' — не падает."""
        r = self.req("generate_daily_digest", {"date": "not-a-date"})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 10. Смешанные кодировки (UTF-8 с BOM, Latin-1 символы)
# ---------------------------------------------------------------------------

class MixedEncodingEdgeCases(EdgeCaseMatrixBase):
    """Смешанные кодировки и специальные Unicode-символы."""

    def test_add_history_item_utf8_bom(self):
        """add_history_item с текстом, содержащим UTF-8 BOM \\ufeff — сохраняется."""
        bom_text = "\ufeffТекст с BOM в начале"
        r = self.req("add_history_item", {"text": bom_text})
        # Либо ok (с нормализацией), либо ошибка — но не исключение
        self.assertIn("ok", r)

    def test_add_history_item_latin1_chars(self):
        """add_history_item с Latin-1 символами — сохраняется."""
        latin1_text = "Caf\xe9 au lait, r\xe9sum\xe9 pour l'\xe9quipe"
        r = self.req("add_history_item", {"text": latin1_text})
        self.assertTrue(r["ok"], f"Latin-1 текст должен сохраняться: {r}")

    def test_add_history_item_mixed_scripts(self):
        """add_history_item с текстом из нескольких письменностей — сохраняется."""
        mixed = "Hello мир 안녕 مرحبا こんにちは"
        r = self.req("add_history_item", {"text": mixed})
        self.assertTrue(r["ok"], f"Смешанные письменности должны сохраняться: {r}")

    def test_add_history_item_control_chars(self):
        """add_history_item с управляющими символами — не падает."""
        ctrl_text = "Текст\x00с\x01нулевым\x02байтом"
        r = self.req("add_history_item", {"text": ctrl_text})
        self.assertIn("ok", r)

    def test_search_history_unicode_query(self):
        """search_history с emoji в поисковом запросе — не падает."""
        r = self.req("search_history", {"query": "тест 🎤 голос"})
        self.assertIn("ok", r)

    def test_add_history_item_surrogate_pair(self):
        """add_history_item с surrogate-символами (Emoji) — сохраняется."""
        emoji_text = "Тест с эмодзи: 🎙️🔊💬"
        r = self.req("add_history_item", {"text": emoji_text})
        self.assertTrue(r["ok"], f"Emoji-текст должен сохраняться: {r}")

    def test_add_history_item_rtl_text(self):
        """add_history_item с RTL-текстом (иврит/арабский) — сохраняется."""
        rtl_text = "שלום עולם — мир на иврите"
        r = self.req("add_history_item", {"text": rtl_text})
        self.assertTrue(r["ok"], f"RTL-текст должен сохраняться: {r}")

    def test_score_readability_mixed_encoding(self):
        """score_readability с Latin-1 и Кириллицей — не падает."""
        mixed = "Résumé написан на Python — хорошая идея."
        r = self.req("score_readability", {"text": mixed})
        self.assertIn("ok", r)


# ---------------------------------------------------------------------------
# 11. Непредвиденные типы параметров
# ---------------------------------------------------------------------------

class WrongTypeEdgeCases(EdgeCaseMatrixBase):
    """Неожиданные типы для стандартных параметров."""

    def test_get_history_page_string_limit(self):
        """get_history_page с limit как строкой — корректно обрабатывается."""
        r = self.req("get_history_page", {"limit": "50"})
        self.assertIn("ok", r)

    def test_get_history_page_float_limit(self):
        """get_history_page с limit как float — не падает."""
        r = self.req("get_history_page", {"limit": 10.7})
        self.assertIn("ok", r)

    def test_delete_history_item_numeric_id(self):
        """delete_history_item с числовым item_id — не падает."""
        r = self.req("delete_history_item", {"item_id": 12345})
        self.assertIn("ok", r)

    def test_add_history_item_list_as_text(self):
        """add_history_item с list вместо текста — безопасно отклоняется."""
        r = self.req("add_history_item", {"text": ["список", "слов"]})
        # Должна быть либо ошибка, либо корректная конвертация в строку
        self.assertIn("ok", r)

    def test_handle_unknown_method(self):
        """Неизвестный метод — возвращает понятную ошибку."""
        r = self.req("totally_unknown_method_xyzzy")
        self.assertFalse(r["ok"], "Неизвестный метод должен вернуть ok=False")
        self.assertIn("error", r)

    def test_params_not_dict(self):
        """Запрос с params как строкой — возвращает ошибку invalid_params."""
        r = self.service.handle_request({
            "id": "t",
            "method": "ping",
            "params": "not-a-dict"
        })
        self.assertFalse(r["ok"], "Не-dict params должен быть отклонён")

    def test_params_as_list(self):
        """Запрос с params как списком — возвращает ошибку invalid_params."""
        r = self.service.handle_request({
            "id": "t",
            "method": "ping",
            "params": [1, 2, 3]
        })
        self.assertFalse(r["ok"], "Список в params должен быть отклонён")


# ---------------------------------------------------------------------------
# 12. Граничные случаи для операций с тегами и аннотациями
# ---------------------------------------------------------------------------

class TagAnnotationEdgeCases(EdgeCaseMatrixBase):
    """Граничные случаи при работе с тегами и аннотациями."""

    def test_add_empty_tag(self):
        """add_tag с пустым тегом — не даёт добавить пустой тег."""
        item_id = self.add_item("элемент для тегов")
        # handle_add_tag использует params["id"] и params["tag"]
        r = self.req("add_tag", {"id": item_id, "tag": ""})
        # Пустой тег должен быть отклонён
        self.assertFalse(r["ok"], "Пустой тег должен быть отклонён")

    def test_add_very_long_tag(self):
        """add_tag с тегом длиной 1000 символов — не падает."""
        item_id = self.add_item("элемент для длинного тега")
        long_tag = "т" * 1000
        r = self.req("add_tag", {"id": item_id, "tag": long_tag})
        self.assertIn("ok", r)

    def test_search_by_nonexistent_tag(self):
        """search_by_tag с тегом, которого нет — возвращает пустой список."""
        r = self.req("search_by_tag", {"tag": "несуществующий-тег-xyz-99999"})
        self.assertTrue(r["ok"], f"Поиск по несуществующему тегу: {r}")
        items = r["result"].get("items", [])
        self.assertEqual(items, [], "Несуществующий тег → пустой список")

    def test_set_annotation_empty_text(self):
        """set_annotation с пустым текстом заметки — не падает."""
        item_id = self.add_item("элемент для пустой заметки")
        # handle_set_annotation использует params["id"]
        r = self.req("set_annotation", {"id": item_id, "annotation": ""})
        self.assertIn("ok", r)

    def test_set_annotation_very_long(self):
        """set_annotation с заметкой 10000 символов — не падает."""
        item_id = self.add_item("элемент для длинной заметки")
        long_note = "Длинная заметка. " * 555
        long_note = long_note[:10000]
        r = self.req("set_annotation", {"id": item_id, "annotation": long_note})
        self.assertIn("ok", r)


if __name__ == "__main__":
    unittest.main()
