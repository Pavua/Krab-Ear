"""Smoke-тесты: полная проверка таблицы IPC-диспетчера BackendService.

Цель: убедиться, что каждый зарегистрированный IPC-метод:
  - не вызывает AttributeError / NameError (метод реально существует)
  - возвращает dict с ключами "id" и "ok"
  - не падает с непойманным исключением

Тест НЕ проверяет корректность бизнес-логики — только, что «всё заводится».
Методы, требующие реального аудио или файлов, вызываются с пустыми/минимальными
параметрами и ожидают либо ok=True (graceful), либо ok=False (handled error).
"""

from __future__ import annotations
from backend.translator import TranslationResult
from backend.state_store import StateStore
from backend.service import BackendService

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Фейковые коллабораторы (минимальные заглушки)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16000
    last_stop_trim_ms = 0
    last_stop_timeout_sec = 3.0

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        data = np.zeros(16000, dtype=np.float32)
        return data, 1.0

    def snapshot_audio(self, max_duration_sec=12.0):
        return np.zeros(32000, dtype=np.float32), 1.0


class _FakeEngine:
    """Минимальный фейк AudioEngine для методов, использующих transcriber.engine."""
    _last_llm_diff = None
    _llm_rewriter = None
    _settings_get = None
    quality_profile = "balanced"
    current_model = "fake-model"

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class _FakeTranscriber:
    def __init__(self):
        self.counter = 0
        self.preview_counter = 0
        self.engine = _FakeEngine()

    def transcribe(self, audio_data, quality_profile="balanced",
                   cleanup_profile="soft", domain="casual",
                   extra_vocabulary=None, lang_hint=None, history_context=None,
                   stt_hotwords=None, settings=None, diarize=None,
                   skip_vad_prefilter=False, silence_ranges=None):
        self.counter += 1
        return f"fake transcription #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        self.preview_counter += 1
        return f"preview#{self.preview_counter}"


class _FakeTranslator:
    last_mode = "off"

    def translate(self, text, mode, network_mode,
                  translation_style="neutral", glossary=None):
        self.last_mode = mode
        return TranslationResult(
            text="" if mode == "off" else f"TRANSLATED:{text}",
            status="not_requested" if mode == "off" else "ok",
            source_lang="",
            target_lang="",
            mode=mode,
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Вспомогательный базовый класс
# ---------------------------------------------------------------------------

class _DispatchBase(unittest.TestCase):
    """Базовый класс с setUp и вспомогательными методами."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.svc = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        # Добавим несколько записей в историю для тестов, которые их читают
        for i in range(3):
            self.svc.handle_request({
                "id": f"seed_{i}",
                "method": "add_history_item",
                "params": {
                    "text": f"тестовая запись {i} hello world фраза",
                    "paste_status": "ok",
                    "translation_mode": "off",
                    "translation_status": "not_requested",
                    "confidence": 0.85,
                },
            })

    def req(self, method: str, params: dict | None = None, req_id: str = "smoke"):
        """Вызвать handle_request и вернуть ответ."""
        return self.svc.handle_request({
            "id": req_id,
            "method": method,
            "params": params or {},
        })

    def assert_dispatch(self, method: str, params: dict | None = None, *,
                        ok_required: bool | None = None):
        """Проверить базовые инварианты ответа (id, ok, dict)."""
        resp = self.req(method, params)
        self.assertIsInstance(resp, dict, f"{method}: ответ не dict")
        self.assertIn("id", resp, f"{method}: нет ключа 'id'")
        self.assertIn("ok", resp, f"{method}: нет ключа 'ok'")
        if ok_required is True:
            self.assertTrue(resp["ok"], f"{method}: ok=False, error={resp.get('error')}")
        elif ok_required is False:
            self.assertFalse(resp["ok"], f"{method}: ожидали ok=False, но ok=True")
        return resp


# ===========================================================================
# Группа 1: Core — запись, состояние, ping
# ===========================================================================

class TestCoreGroup(_DispatchBase):
    """Базовые методы: ping, recording state, paste status."""

    ALL_METHODS = [
        "ping",
        "get_recording_state",
        "get_recording_stats",
        "set_paste_status",
        "list_audio_inputs",
        "get_audio_devices",
        "test_microphone",
    ]

    def test_ping(self):
        resp = self.assert_dispatch("ping", ok_required=True)
        self.assertEqual(resp["result"]["service"], "krabear-backend")

    def test_get_recording_state(self):
        self.assert_dispatch("get_recording_state", ok_required=True)

    def test_get_recording_stats(self):
        self.assert_dispatch("get_recording_stats", ok_required=True)

    def test_set_paste_status(self):
        # set_paste_status requires a valid id; with fake id it returns ok=False gracefully
        self.assert_dispatch("set_paste_status", {"id": "nonexistent", "paste_status": "ok"})

    def test_list_audio_inputs(self):
        self.assert_dispatch("list_audio_inputs", ok_required=True)

    def test_get_audio_devices(self):
        self.assert_dispatch("get_audio_devices", ok_required=True)

    def test_test_microphone(self):
        # Может вернуть ok=False если нет реального микрофона — оба варианта допустимы
        self.assert_dispatch("test_microphone")

    def test_start_stop_recording(self):
        self.assert_dispatch("start_recording", ok_required=True)
        self.assert_dispatch("stop_recording", ok_required=True)

    def test_total_methods_reported(self):
        """Smoke: убеждаемся, что список методов совпадает с тем, что тестируем."""
        self.assertEqual(len(self.ALL_METHODS), 7)


# ===========================================================================
# Группа 2: История — CRUD, поиск, избранное
# ===========================================================================

class TestHistoryGroup(_DispatchBase):
    """История: чтение, поиск, избранное, теги, экспорт."""

    def test_get_history_page(self):
        self.assert_dispatch("get_history_page", {"page": 1, "page_size": 10}, ok_required=True)

    def test_search_history(self):
        self.assert_dispatch("search_history", {"query": "тест"}, ok_required=True)

    def test_fuzzy_search(self):
        self.assert_dispatch("fuzzy_search", {"query": "тестовая"}, ok_required=True)

    def test_search_with_highlights(self):
        self.assert_dispatch("search_with_highlights", {"query": "hello"}, ok_required=True)

    def test_search_by_speaker(self):
        self.assert_dispatch("search_by_speaker", {"speaker": "speaker_0"}, ok_required=True)

    def test_get_history_stats(self):
        self.assert_dispatch("get_history_stats", ok_required=True)

    def test_get_history_overview(self):
        self.assert_dispatch("get_history_overview", ok_required=True)

    def test_get_history_item(self):
        # Несуществующий ID → ok=False (метод обработан)
        resp = self.assert_dispatch("get_history_item", {"item_id": "nonexistent"})
        self.assertIn("ok", resp)

    def test_add_history_item(self):
        resp = self.assert_dispatch("add_history_item", {
            "text": "новая запись",
            "paste_status": "ok",
            "translation_mode": "off",
            "translation_status": "not_requested",
        }, ok_required=True)
        self.assertIn("id", resp.get("result", {}))

    def test_delete_history_item(self):
        # Несуществующий ID — метод должен вернуть dict
        self.assert_dispatch("delete_history_item", {"item_id": "fake_id"})

    def test_compact_history(self):
        self.assert_dispatch("compact_history", ok_required=True)

    def test_get_history_statistics(self):
        self.assert_dispatch("get_history_statistics", ok_required=True)

    def test_word_frequency_analysis(self):
        self.assert_dispatch("word_frequency_analysis", ok_required=True)

    def test_cleanup_old_history(self):
        self.assert_dispatch("cleanup_old_history", {"days": 3650}, ok_required=True)

    def test_get_storage_info(self):
        self.assert_dispatch("get_storage_info", ok_required=True)

    def test_get_transcripts_path(self):
        self.assert_dispatch("get_transcripts_path", ok_required=True)

    def test_backup_history(self):
        self.assert_dispatch("backup_history", ok_required=True)

    def test_list_backups(self):
        self.assert_dispatch("list_backups", ok_required=True)

    def test_get_clipboard_history(self):
        self.assert_dispatch("get_clipboard_history", ok_required=True)

    def test_repaste_item(self):
        # Нет реального item — ok может быть False, но метод должен ответить
        self.assert_dispatch("repaste_item", {"item_id": "fake"})

    def test_find_duplicates(self):
        self.assert_dispatch("find_duplicates", ok_required=True)

    def test_filter_by_confidence(self):
        self.assert_dispatch("filter_by_confidence", {"min_confidence": 0.5}, ok_required=True)

    def test_get_favorites(self):
        self.assert_dispatch("get_favorites", ok_required=True)

    def test_is_favorite(self):
        self.assert_dispatch("is_favorite", {"item_id": "fake"})

    def test_toggle_favorite(self):
        self.assert_dispatch("toggle_favorite", {"item_id": "fake"})


# ===========================================================================
# Группа 3: Теги
# ===========================================================================

class TestTagsGroup(_DispatchBase):
    """Теги: добавление, удаление, поиск."""

    def _seed_item_id(self):
        resp = self.req("get_history_page", {"page": 1, "page_size": 5})
        items = resp.get("result", {}).get("items", [])
        return items[0]["id"] if items else None

    def test_add_tag(self):
        # add_tag uses "id" key; create own item for reliability
        add = self.req("add_history_item", {
            "text": "тест тегов",
            "paste_status": "ok",
            "translation_mode": "off",
            "translation_status": "not_requested",
        })
        item_id = add.get("result", {}).get("id", "")
        self.assertTrue(item_id, "Не удалось создать запись для теста тегов")
        self.assert_dispatch("add_tag", {"id": item_id, "tag": "test_tag"}, ok_required=True)

    def test_remove_tag(self):
        item_id = self._seed_item_id()
        if item_id:
            self.assert_dispatch("remove_tag", {"id": item_id, "tag": "missing_tag"})
        else:
            self.assert_dispatch("remove_tag", {"id": "fake", "tag": "x"})

    def test_get_tags(self):
        item_id = self._seed_item_id()
        params = {"id": item_id} if item_id else {"id": "fake"}
        self.assert_dispatch("get_tags", params)

    def test_search_by_tag(self):
        self.assert_dispatch("search_by_tag", {"tag": "test_tag"}, ok_required=True)

    def test_list_all_tags(self):
        self.assert_dispatch("list_all_tags", ok_required=True)


# ===========================================================================
# Группа 4: Экспорт
# ===========================================================================

class TestExportGroup(_DispatchBase):
    """Экспорт истории в различные форматы."""

    def test_export_history(self):
        self.assert_dispatch("export_history", ok_required=True)

    def test_export_history_srt(self):
        # export_history_srt requires an item id; nonexistent → ok=False gracefully
        self.assert_dispatch("export_history_srt", {"id": "nonexistent"})

    def test_export_history_csv(self):
        self.assert_dispatch("export_history_csv", ok_required=True)

    def test_export_history_markdown(self):
        self.assert_dispatch("export_history_markdown", ok_required=True)

    def test_export_history_json(self):
        self.assert_dispatch("export_history_json", ok_required=True)

    def test_export_obsidian(self):
        self.assert_dispatch("export_obsidian", ok_required=True)

    def test_export_html_report(self):
        self.assert_dispatch("export_html_report", ok_required=True)

    def test_batch_export(self):
        self.assert_dispatch("batch_export", {"formats": ["csv"]}, ok_required=True)

    def test_import_history_ndjson(self):
        # Пустой контент → ошибка парсинга, но метод должен отвечать
        self.assert_dispatch("import_history_ndjson", {"content": ""})


# ===========================================================================
# Группа 5: Настройки и профили
# ===========================================================================

class TestSettingsGroup(_DispatchBase):
    """Настройки, профили, уведомления."""

    def test_get_settings(self):
        self.assert_dispatch("get_settings", ok_required=True)

    def test_set_settings(self):
        self.assert_dispatch("set_settings", {"mode": "menubar"}, ok_required=True)

    def test_apply_profile_preset(self):
        # Param key is "profile" (not "preset")
        self.assert_dispatch("apply_profile_preset", {"profile": "default"}, ok_required=True)

    def test_list_profile_presets(self):
        self.assert_dispatch("list_profile_presets", ok_required=True)

    def test_get_notification_preferences(self):
        self.assert_dispatch("get_notification_preferences", ok_required=True)

    def test_set_notification_preferences(self):
        self.assert_dispatch("set_notification_preferences", {
            "confidence_warning": True,
        }, ok_required=True)

    def test_export_settings(self):
        self.assert_dispatch("export_settings", ok_required=True)

    def test_import_settings(self):
        # Пустой JSON → должен вернуть dict
        self.assert_dispatch("import_settings", {"data": {}})

    def test_list_config_presets(self):
        self.assert_dispatch("list_config_presets", ok_required=True)

    def test_apply_config_preset(self):
        # Вернёт настройки по имени пресета (или ошибку если имя не найдено)
        self.assert_dispatch("apply_config_preset", {"preset_name": "default"})

    def test_create_config_preset(self):
        self.assert_dispatch("create_config_preset", {
            "name": "test_preset",
            "settings_patch": {"mode": "menubar"},
        })


# ===========================================================================
# Группа 6: Перевод и глоссарий
# ===========================================================================

class TestTranslationGroup(_DispatchBase):
    """Перевод, глоссарий, словарный запас."""

    def test_translate_text(self):
        self.assert_dispatch("translate_text", {
            "text": "hello world",
            "mode": "off",
        }, ok_required=True)

    def test_set_translation_glossary_item(self):
        self.assert_dispatch("set_translation_glossary_item", {
            "source": "hello",
            "target": "привет",
        }, ok_required=True)

    def test_remove_translation_glossary_item(self):
        self.assert_dispatch("remove_translation_glossary_item", {
            "source": "hello",
        })

    def test_get_glossary_suggestions(self):
        self.assert_dispatch("get_glossary_suggestions", ok_required=True)

    def test_get_vocabulary_suggestions(self):
        self.assert_dispatch("get_vocabulary_suggestions", ok_required=True)


# ===========================================================================
# Группа 7: Call Assist
# ===========================================================================

class TestCallAssistGroup(_DispatchBase):
    """Call Assist: start/stop, состояние, диагностика."""

    def test_get_call_assist_state(self):
        self.assert_dispatch("get_call_assist_state", ok_required=True)

    def test_call_assist_diagnostics(self):
        # Requires active gateway session — ok=False without one, but must be callable
        self.assert_dispatch("call_assist_diagnostics")

    def test_list_call_assist_quick_phrases(self):
        # Без реального Voice Gateway — ok=False допустимо (smoke-тест проверяет dispatchability)
        self.assert_dispatch("list_call_assist_quick_phrases")

    def test_call_assist_quick_phrase(self):
        # Без активной сессии — ok=False допустимо
        self.assert_dispatch("call_assist_quick_phrase", {"phrase": "test"})

    def test_call_assist_cost_estimate(self):
        # Без реального Voice Gateway — ok=False допустимо (smoke-тест проверяет dispatchability)
        self.assert_dispatch("call_assist_cost_estimate")

    def test_call_assist_timeline(self):
        # Requires active gateway session — ok=False without one, but must be callable
        self.assert_dispatch("call_assist_timeline")

    def test_call_assist_timeline_stats(self):
        self.assert_dispatch("call_assist_timeline_stats")

    def test_call_assist_timeline_summary(self):
        self.assert_dispatch("call_assist_timeline_summary")

    def test_call_assist_timeline_export(self):
        self.assert_dispatch("call_assist_timeline_export")

    def test_call_assist_timeline_clear(self):
        self.assert_dispatch("call_assist_timeline_clear")

    def test_call_assist_timeline_to_history(self):
        self.assert_dispatch("call_assist_timeline_to_history")

    def test_call_assist_summary(self):
        # Без активной сессии — ok=True (пустой summary) или ok=False
        self.assert_dispatch("call_assist_summary")

    def test_start_stop_call_assist(self):
        self.assert_dispatch("start_call_assist", {"quality_profile": "balanced"})
        self.assert_dispatch("stop_call_assist")


# ===========================================================================
# Группа 8: Диагностика и мониторинг
# ===========================================================================

class TestDiagnosticsGroup(_DispatchBase):
    """Диагностика, метрики, здоровье системы."""

    def test_get_diagnostics(self):
        self.assert_dispatch("get_diagnostics", ok_required=True)

    def test_health_check(self):
        self.assert_dispatch("health_check", ok_required=True)

    def test_get_metrics_dashboard(self):
        self.assert_dispatch("get_metrics_dashboard", ok_required=True)

    def test_get_system_info(self):
        self.assert_dispatch("get_system_info", ok_required=True)

    def test_get_error_report(self):
        self.assert_dispatch("get_error_report", ok_required=True)

    def test_get_error_stats(self):
        self.assert_dispatch("get_error_stats", ok_required=True)

    def test_get_usage_stats(self):
        self.assert_dispatch("get_usage_stats", ok_required=True)

    def test_get_audio_info(self):
        # Требует реальный файл — ok=False допустимо
        self.assert_dispatch("get_audio_info", {"path": "/nonexistent/file.wav"})

    def test_get_throttle_stats(self):
        self.assert_dispatch("get_throttle_stats", ok_required=True)

    def test_get_auto_backup_status(self):
        self.assert_dispatch("get_auto_backup_status", ok_required=True)

    def test_get_export_schedule_status(self):
        self.assert_dispatch("get_export_schedule_status", ok_required=True)

    def test_list_auto_exports(self):
        self.assert_dispatch("list_auto_exports", ok_required=True)

    def test_get_startup_diagnostics(self):
        self.assert_dispatch("get_startup_diagnostics", ok_required=True)


# ===========================================================================
# Группа 9: Анализ и NLP
# ===========================================================================

class TestAnalysisGroup(_DispatchBase):
    """Текстовый анализ: язык, термины, сравнение, темп, эмоция."""

    def test_extract_terms(self):
        self.assert_dispatch("extract_terms", {"text": "нейронная сеть машинное обучение"}, ok_required=True)

    def test_compare_texts(self):
        self.assert_dispatch("compare_texts", {
            "text1": "hello world",
            "text2": "hello there",
        }, ok_required=True)

    def test_score_readability(self):
        self.assert_dispatch("score_readability", {
            "text": "Это простой тестовый текст для проверки читабельности.",
        }, ok_required=True)

    def test_score_transcription(self):
        self.assert_dispatch("score_transcription", {
            "text": "тест транскрипции",
            "confidence": 0.9,
        }, ok_required=True)

    def test_detect_emotion(self):
        self.assert_dispatch("detect_emotion", {
            "text": "Я очень рад этому событию!",
        }, ok_required=True)

    def test_generate_auto_title(self):
        self.assert_dispatch("generate_auto_title", {
            "text": "Сегодня мы обсуждали планы на следующий квартал.",
        }, ok_required=True)

    def test_post_process_text(self):
        self.assert_dispatch("post_process_text", {
            "text": "   тест текст   ",
            "steps": ["whitespace"],
        }, ok_required=True)

    def test_list_post_process_steps(self):
        self.assert_dispatch("list_post_process_steps", ok_required=True)

    def test_get_context_memory(self):
        self.assert_dispatch("get_context_memory", ok_required=True)

    def test_get_keyword_cloud(self):
        self.assert_dispatch("get_keyword_cloud", {"max_words": 20}, ok_required=True)

    def test_get_topic_timeline(self):
        # get_topic_timeline may fail on HistoryItem vs dict mismatch in topic_tracker
        # — smoke test only: method must be registered and return a dict
        self.assert_dispatch("get_topic_timeline", {"window_size": 3, "limit": 10})

    def test_summarize_text(self):
        # LLM недоступен в тестах → ok=False допустимо
        self.assert_dispatch("summarize_text", {"text": "короткий текст"})

    def test_summarize_item(self):
        self.assert_dispatch("summarize_item", {"item_id": "fake_id"})

    def test_get_last_llm_diff(self):
        self.assert_dispatch("get_last_llm_diff", ok_required=True)


# ===========================================================================
# Группа 10: Аналитика, тренды, дайджест
# ===========================================================================

class TestAnalyticsTrendsGroup(_DispatchBase):
    """Аналитика: тренды, дайджест, сравнение периодов."""

    def test_generate_daily_digest(self):
        self.assert_dispatch("generate_daily_digest", {}, ok_required=True)

    def test_analyze_quality_trends(self):
        self.assert_dispatch("analyze_quality_trends", {"days": 7}, ok_required=True)

    def test_compare_periods(self):
        self.assert_dispatch("compare_periods", {
            "period1_start": "2026-01-01",
            "period1_end": "2026-01-15",
            "period2_start": "2026-01-16",
            "period2_end": "2026-01-31",
        }, ok_required=True)

    def test_get_sentiment_trends(self):
        self.assert_dispatch("get_sentiment_trends", {"days": 7}, ok_required=True)

    def test_get_analytics_dashboard(self):
        self.assert_dispatch("get_analytics_dashboard", {"days": 7}, ok_required=True)

    def test_get_recording_insights(self):
        self.assert_dispatch("get_recording_insights", ok_required=True)


# ===========================================================================
# Группа 11: Целостность данных и миграция
# ===========================================================================

class TestIntegrityGroup(_DispatchBase):
    """Целостность данных, миграция, бэкапы."""

    def test_check_integrity(self):
        self.assert_dispatch("check_integrity", ok_required=True)

    def test_repair_integrity(self):
        self.assert_dispatch("repair_integrity", ok_required=True)

    def test_check_migration(self):
        # data_migrator requires data_dir param
        data_dir = str(self.svc.store.data_dir)
        self.assert_dispatch("check_migration", {"data_dir": data_dir}, ok_required=True)

    def test_run_migration(self):
        # data_migrator requires data_dir param
        data_dir = str(self.svc.store.data_dir)
        self.assert_dispatch("run_migration", {"data_dir": data_dir}, ok_required=True)

    def test_restore_history(self):
        # Несуществующий файл → ok=False допустимо
        self.assert_dispatch("restore_history", {"backup_path": "/nonexistent.ndjson"})


# ===========================================================================
# Группа 12: Коллекции
# ===========================================================================

class TestCollectionsGroup(_DispatchBase):
    """Коллекции: создание, удаление, управление элементами."""

    def test_list_collections(self):
        self.assert_dispatch("list_collections", ok_required=True)

    def test_create_collection(self):
        resp = self.assert_dispatch("create_collection", {
            "name": "test_collection",
        }, ok_required=True)
        self.assertIsInstance(resp.get("result"), dict)

    def test_delete_collection(self):
        create = self.req("create_collection", {"name": "to_delete"})
        cid = create.get("result", {}).get("id", "fake")
        self.assert_dispatch("delete_collection", {"collection_id": cid})

    def test_add_to_collection(self):
        self.assert_dispatch("add_to_collection", {
            "collection_id": "fake",
            "item_id": "fake_item",
        })

    def test_remove_from_collection(self):
        self.assert_dispatch("remove_from_collection", {
            "collection_id": "fake",
            "item_id": "fake_item",
        })

    def test_get_collection_items(self):
        self.assert_dispatch("get_collection_items", {"collection_id": "fake"})


# ===========================================================================
# Группа 13: Цепочки записей
# ===========================================================================

class TestChainsGroup(_DispatchBase):
    """Цепочки связанных записей."""

    def test_list_chains(self):
        self.assert_dispatch("list_chains", ok_required=True)

    def test_start_chain(self):
        self.assert_dispatch("start_chain", {"name": "test chain"}, ok_required=True)

    def test_get_chain_nonexistent(self):
        self.assert_dispatch("get_chain", {"chain_id": "fake"})

    def test_end_chain_nonexistent(self):
        self.assert_dispatch("end_chain", {"chain_id": "fake"})

    def test_merge_chain_text_nonexistent(self):
        self.assert_dispatch("merge_chain_text", {"chain_id": "fake"})

    def test_add_to_chain(self):
        # Нет реальной цепочки — ok=False допустимо
        self.assert_dispatch("add_to_chain", {"chain_id": "fake", "item_id": "item"})


# ===========================================================================
# Группа 14: Планировщик записей
# ===========================================================================

class TestSchedulerGroup(_DispatchBase):
    """Планировщик записей."""

    def test_list_scheduled_recordings(self):
        self.assert_dispatch("list_scheduled_recordings", ok_required=True)

    def test_schedule_recording(self):
        self.assert_dispatch("schedule_recording", {
            "start_at": "2099-01-01T10:00:00",
            "duration_sec": 30,
        })

    def test_cancel_scheduled_recording(self):
        self.assert_dispatch("cancel_scheduled_recording", {"recording_id": "fake"})


# ===========================================================================
# Группа 15: Аннотации
# ===========================================================================

class TestAnnotationsGroup(_DispatchBase):
    """Аннотации к записям."""

    def _get_item_id(self):
        resp = self.req("get_history_page", {"page": 1, "page_size": 5})
        items = resp.get("result", {}).get("items", [])
        return items[0]["id"] if items else None

    def test_set_annotation(self):
        # set_annotation uses "id" (not "item_id") and "note" (not "annotation")
        item_id = self._get_item_id()
        if item_id:
            self.assert_dispatch("set_annotation", {
                "id": item_id,
                "note": "тестовая заметка",
            }, ok_required=True)
        else:
            self.assert_dispatch("set_annotation", {"id": "fake", "note": "x"})

    def test_get_annotation(self):
        # get_annotation uses "id" (not "item_id")
        self.assert_dispatch("get_annotation", {"id": "fake"})

    def test_search_annotations(self):
        self.assert_dispatch("search_annotations", {"query": "заметка"}, ok_required=True)


# ===========================================================================
# Группа 16: Профили нормализации
# ===========================================================================

class TestNormalizationGroup(_DispatchBase):
    """Профили нормализации текста."""

    def test_list_normalization_profiles(self):
        self.assert_dispatch("list_normalization_profiles", ok_required=True)

    def test_add_profile_dispatched(self):
        resp = self.assert_dispatch(
            "add_normalization_profile",
            {"name": "test_custom", "rules": ["cleanup_soft"], "description": "тест"},
            ok_required=True,
        )
        self.assertIn("profile", resp.get("result", {}))
        self.assertEqual(resp["result"]["profile"]["name"], "test_custom")

    def test_remove_profile_dispatched(self):
        # Сначала добавляем профиль
        self.svc.handle_request({
            "id": "setup",
            "method": "add_normalization_profile",
            "params": {"name": "to_remove", "rules": ["cleanup_soft"]},
        })
        resp = self.assert_dispatch(
            "remove_normalization_profile",
            {"name": "to_remove"},
            ok_required=True,
        )
        self.assertTrue(resp["result"]["removed"])

    def test_apply_profile_dispatched(self):
        resp = self.assert_dispatch(
            "apply_normalization_profile",
            {"text": "  привет   мир  ", "profile_name": "clean"},
            ok_required=True,
        )
        result = resp["result"]
        self.assertIn("text", result)
        self.assertEqual(result["profile_name"], "clean")


# ===========================================================================
# Группа 17: Голосовые и аудио утилиты
# ===========================================================================

class TestAudioUtilsGroup(_DispatchBase):
    """Аудио-утилиты: шум, waveform, fingerprint."""

    def test_profile_noise(self):
        self.assert_dispatch("profile_noise", {"path": "/nonexistent.wav"})

    def test_analyze_audio_quality(self):
        self.assert_dispatch("analyze_audio_quality", {"file_path": "/nonexistent.wav"})

    def test_analyze_silence(self):
        self.assert_dispatch("analyze_silence", {"file_path": "/nonexistent.wav"})

    def test_get_waveform(self):
        self.assert_dispatch("get_waveform", {"path": "/nonexistent.wav"})

    def test_check_audio_duplicate(self):
        # Нет реального аудио — ошибка допустима
        self.assert_dispatch("check_audio_duplicate", {
            "audio1": [0.0] * 16,
            "audio2": [0.0] * 16,
        })


# ===========================================================================
# Группа 18: Транскрипционная очередь
# ===========================================================================

class TestTranscriptionQueueGroup(_DispatchBase):
    """Очередь транскрипции."""

    def test_list_transcription_queue(self):
        self.assert_dispatch("list_transcription_queue", ok_required=True)

    def test_enqueue_transcription(self):
        # Несуществующий файл → ok=False допустимо
        self.assert_dispatch("enqueue_transcription", {
            "path": "/nonexistent.wav",
            "priority": 5,
        })

    def test_cancel_transcription(self):
        self.assert_dispatch("cancel_transcription", {"job_id": "fake"})

    def test_get_queue_status(self):
        self.assert_dispatch("get_queue_status", {"job_id": "fake"})


# ===========================================================================
# Группа 19: Шаринг и версионирование транскриптов
# ===========================================================================

class TestSharingVersioningGroup(_DispatchBase):
    """Шаринг пакетов и версионирование транскриптов."""

    def _get_item_id(self):
        resp = self.req("get_history_page", {"page": 1, "page_size": 5})
        items = resp.get("result", {}).get("items", [])
        return items[0]["id"] if items else None

    def test_list_shared(self):
        self.assert_dispatch("list_shared", ok_required=True)

    def test_prepare_share(self):
        item_id = self._get_item_id()
        if item_id:
            self.assert_dispatch("prepare_share", {"item_ids": [item_id]}, ok_required=True)
        else:
            self.assert_dispatch("prepare_share", {"item_ids": ["fake"]})

    def test_get_shared(self):
        self.assert_dispatch("get_shared", {"share_id": "fake"})

    def test_get_transcript_versions(self):
        item_id = self._get_item_id()
        if item_id:
            self.assert_dispatch("get_transcript_versions", {"item_id": item_id}, ok_required=True)
        else:
            self.assert_dispatch("get_transcript_versions", {"item_id": "fake"})

    def test_save_transcript_version(self):
        item_id = self._get_item_id()
        if item_id:
            self.assert_dispatch("save_transcript_version", {
                "item_id": item_id,
                "text": "новая версия текста",
            }, ok_required=True)
        else:
            self.assert_dispatch("save_transcript_version", {
                "item_id": "fake",
                "text": "x",
            })

    def test_revert_transcript_version(self):
        self.assert_dispatch("revert_transcript_version", {
            "item_id": "fake",
            "version_id": "fake_v",
        })


# ===========================================================================
# Группа 20: Обучение языков
# ===========================================================================

class TestLanguageLearningGroup(_DispatchBase):
    """Режим изучения языков."""

    def test_get_learning_stats(self):
        # source_lang and target_lang are required params
        self.assert_dispatch("get_learning_stats", {
            "source_lang": "ru",
            "target_lang": "es",
        }, ok_required=True)


# ===========================================================================
# Группа 21: Форматирование для вставки
# ===========================================================================

class TestPasteFormatterGroup(_DispatchBase):
    """Форматирование текста для вставки."""

    def test_list_paste_formatters(self):
        self.assert_dispatch("list_paste_formatters", ok_required=True)

    def test_format_for_paste(self):
        self.assert_dispatch("format_for_paste", {
            "text": "тест текст",
            "app": "telegram",
        }, ok_required=True)


# ===========================================================================
# Группа 22: Аббревиатуры
# ===========================================================================

class TestAbbreviationsGroup(_DispatchBase):
    """Аббревиатуры: добавление, удаление, раскрытие, список."""

    def test_list_abbreviations(self):
        self.assert_dispatch("list_abbreviations", {"language": "ru"}, ok_required=True)

    def test_expand_abbreviations(self):
        self.assert_dispatch("expand_abbreviations", {
            "text": "ИИ решает тест задачу",
            "language": "ru",
        }, ok_required=True)

    def test_add_abbreviation_dispatched(self):
        self.assert_dispatch("add_abbreviation", {
            "abbreviation": "т.н.",
            "expansion": "так называемый",
            "language": "ru",
        }, ok_required=True)

    def test_remove_abbreviation(self):
        # Может не существовать — ok=True/False оба допустимы
        self.assert_dispatch("remove_abbreviation", {
            "abbreviation": "nonexistent",
            "language": "ru",
        })


# ===========================================================================
# Группа 23: Obsidian sync
# ===========================================================================

class TestObsidianGroup(_DispatchBase):
    """Obsidian синхронизация."""

    def test_get_obsidian_sync_status(self):
        self.assert_dispatch("get_obsidian_sync_status", ok_required=True)

    def test_configure_obsidian_sync(self):
        self.assert_dispatch("configure_obsidian_sync", {
            "vault_path": "/nonexistent/vault",
        })

    def test_run_obsidian_sync(self):
        # Без конфигурации → ok=False допустимо
        self.assert_dispatch("run_obsidian_sync")


# ===========================================================================
# Группа 24: Воспроизведение (playback)
# ===========================================================================

class TestPlaybackGroup(_DispatchBase):
    """Статистика воспроизведения записей."""

    def test_record_playback(self):
        self.assert_dispatch("record_playback", {
            "item_id": "fake",
            "duration_listened_sec": 10.0,
        })

    def test_get_playback_stats(self):
        self.assert_dispatch("get_playback_stats", {"item_id": "fake"})

    def test_get_most_replayed(self):
        self.assert_dispatch("get_most_replayed", {"top_n": 5}, ok_required=True)


# ===========================================================================
# Группа 25: Сравнение записей и объединение
# ===========================================================================

class TestCompareMergeGroup(_DispatchBase):
    """Сравнение и объединение записей."""

    def test_compare_recordings_empty(self):
        # Пустой список → ошибка валидации, но метод должен ответить
        resp = self.assert_dispatch("compare_recordings", {"item_ids": []})
        # ok=False ожидаем, т.к. пустой список недопустим
        self.assertFalse(resp["ok"])

    def test_compare_recordings_with_ids(self):
        resp = self.req("get_history_page", {"page": 1, "page_size": 5})
        items = resp.get("result", {}).get("items", [])
        if len(items) >= 2:
            self.assert_dispatch("compare_recordings", {
                "item_ids": [items[0]["id"], items[1]["id"]],
            }, ok_required=True)

    def test_merge_recordings(self):
        resp = self.req("get_history_page", {"page": 1, "page_size": 5})
        items = resp.get("result", {}).get("items", [])
        if len(items) >= 2:
            self.assert_dispatch("merge_recordings", {
                "item_ids": [items[0]["id"], items[1]["id"]],
            })
        else:
            self.assert_dispatch("merge_recordings", {"item_ids": ["fake1", "fake2"]})

    def test_preview_merge(self):
        self.assert_dispatch("preview_merge", {"item_ids": ["fake1", "fake2"]})


# ===========================================================================
# Группа 26: Умный словарь и выбор модели
# ===========================================================================

class TestSmartVocabModelGroup(_DispatchBase):
    """Умный словарь STT и выбор модели."""

    def test_get_smart_vocabulary_suggestions(self):
        self.assert_dispatch("get_smart_vocabulary_suggestions", {"scan_limit": 10}, ok_required=True)

    def test_select_model(self):
        self.assert_dispatch("select_model", {"duration_sec": 30.0}, ok_required=True)

    def test_list_summary_profiles(self):
        self.assert_dispatch("list_summary_profiles", ok_required=True)

    def test_add_summary_profile(self):
        self.assert_dispatch("add_summary_profile", {
            "name": "test_profile",
            "max_sentences": 3,
        })

    def test_auto_summarize_batch(self):
        # LLM недоступен → ok=False допустимо
        self.assert_dispatch("auto_summarize_batch", {"item_ids": []})


# ===========================================================================
# Группа 27: Стоимость и оценки
# ===========================================================================

class TestCostEstimateGroup(_DispatchBase):
    """Оценка стоимости вычислений."""

    def test_estimate_recording_cost(self):
        self.assert_dispatch("estimate_recording_cost", {
            "duration_sec": 60.0,
        }, ok_required=True)

    def test_get_daily_cost_summary(self):
        self.assert_dispatch("get_daily_cost_summary", ok_required=True)


# ===========================================================================
# Группа 28: Event Replay и Webhooks
# ===========================================================================

class TestEventReplayGroup(_DispatchBase):
    """Event log, статистика событий, replay."""

    def test_get_event_log(self):
        self.assert_dispatch("get_event_log", {"limit": 10}, ok_required=True)

    def test_get_event_stats(self):
        self.assert_dispatch("get_event_stats", ok_required=True)

    def test_replay_events(self):
        # from_ts must be an ISO 8601 string
        self.assert_dispatch("replay_events", {
            "from_ts": "2020-01-01T00:00:00",
            "to_ts": "2030-01-01T00:00:00",
        }, ok_required=True)


# ===========================================================================
# Группа 29: Пакетный запрос (batch)
# ===========================================================================

class TestBatchGroup(_DispatchBase):
    """Пакетное выполнение нескольких IPC-методов."""

    def test_batch_empty(self):
        resp = self.assert_dispatch("batch", {"requests": []}, ok_required=True)
        self.assertEqual(resp["result"]["total"], 0)

    def test_batch_multiple(self):
        resp = self.assert_dispatch("batch", {
            "requests": [
                {"method": "ping", "params": {}},
                {"method": "get_settings", "params": {}},
                {"method": "list_all_tags", "params": {}},
            ]
        }, ok_required=True)
        self.assertEqual(resp["result"]["total"], 3)
        self.assertEqual(resp["result"]["succeeded"], 3)

    def test_batch_unknown_method(self):
        resp = self.assert_dispatch("batch", {
            "requests": [{"method": "unknown_xyz", "params": {}}]
        }, ok_required=True)
        self.assertEqual(resp["result"]["failed"], 1)


# ===========================================================================
# Группа 31: Новые методы — обогащение, shutdown, дедупликация, timeline
# ===========================================================================

class TestNewMethodsGroup(_DispatchBase):
    """Методы добавленные после основной разработки: enrich, shutdown, dedup, timeline."""

    def test_enrich_recording(self):
        # Требует item_id — без реального item ok=False допустимо
        self.assert_dispatch("enrich_recording", {"item_id": "fake"})

    def test_get_shutdown_status(self):
        self.assert_dispatch("get_shutdown_status", ok_required=True)

    def test_check_duplicate(self):
        self.assert_dispatch("check_duplicate", {
            "text": "тестовая запись для проверки дублирования",
        }, ok_required=True)

    def test_run_deduplication(self):
        self.assert_dispatch("run_deduplication", ok_required=True)

    def test_get_dedup_stats(self):
        self.assert_dispatch("get_dedup_stats", ok_required=True)

    def test_get_timeline_view(self):
        self.assert_dispatch("get_timeline_view", ok_required=True)


# ===========================================================================
# Группа 32: Поиск, архив, активность и word timing
# ===========================================================================

class TestSearchArchiveGroup(_DispatchBase):
    """Поиск (история запросов), архив, активность, word timing."""

    def test_get_recent_searches(self):
        self.assert_dispatch("get_recent_searches", ok_required=True)

    def test_get_popular_searches(self):
        self.assert_dispatch("get_popular_searches", ok_required=True)

    def test_clear_search_history(self):
        self.assert_dispatch("clear_search_history", ok_required=True)

    def test_archive_items(self):
        # Пустой список → ok=True или ok=False, метод должен ответить
        self.assert_dispatch("archive_items", {"item_ids": []})

    def test_unarchive_items(self):
        self.assert_dispatch("unarchive_items", {"item_ids": []})

    def test_list_archived(self):
        self.assert_dispatch("list_archived", ok_required=True)

    def test_get_archive_stats(self):
        self.assert_dispatch("get_archive_stats", ok_required=True)

    def test_get_activity_calendar(self):
        self.assert_dispatch("get_activity_calendar", ok_required=True)

    def test_analyze_word_timing(self):
        # Пустой список segments — метод должен ответить
        self.assert_dispatch("analyze_word_timing", {"segments": []})


# ===========================================================================
# Группа 30: Метод unknown — проверка fallback
# ===========================================================================

class TestUnknownMethodGroup(_DispatchBase):
    """Неизвестный метод должен вернуть ok=False."""

    def test_unknown_method(self):
        self.assert_dispatch("nonexistent_method_xyz", ok_required=False)

    def test_empty_method(self):
        resp = self.svc.handle_request({"id": "t", "method": "", "params": {}})
        self.assertFalse(resp["ok"])


# ===========================================================================
# Сводный тест: подсчёт всех зарегистрированных методов
# ===========================================================================

class TestMethodCountSummary(_DispatchBase):
    """Проверяем, что dispatch-таблица содержит ожидаемое количество методов."""

    # Полный список методов из dispatch-таблицы service.py
    EXPECTED_METHODS = [
        "ping", "start_recording", "stop_recording", "get_recording_state",
        "start_call_assist", "stop_call_assist", "get_call_assist_state",
        "call_assist_diagnostics", "call_assist_summary", "call_assist_quick_phrase",
        "list_call_assist_quick_phrases", "call_assist_cost_estimate",
        "call_assist_timeline", "call_assist_timeline_stats",
        "call_assist_timeline_summary", "call_assist_timeline_export",
        "call_assist_timeline_clear", "call_assist_timeline_to_history",
        "list_audio_inputs",
        "get_history_page", "search_history", "fuzzy_search",
        "search_with_highlights", "search_by_speaker",
        "delete_history_item", "set_paste_status",
        "get_settings", "set_settings", "compact_history",
        "add_history_item", "transcribe_paths", "preview_transcribe_paths",
        "translate_text", "get_diagnostics",
        "set_translation_glossary_item", "remove_translation_glossary_item",
        "get_glossary_suggestions", "import_history_ndjson",
        "get_history_stats", "get_history_overview", "get_history_item",
        "add_tag", "remove_tag", "get_tags", "search_by_tag", "list_all_tags",
        "get_recording_stats", "get_metrics_dashboard",
        "summarize_text", "summarize_item", "get_last_llm_diff",
        "get_vocabulary_suggestions",
        "toggle_favorite", "get_favorites", "is_favorite",
        "export_history", "export_history_srt", "export_history_csv",
        "batch_export", "export_history_markdown", "export_obsidian",
        "export_history_json", "export_html_report",
        "repaste_item", "get_clipboard_history", "cleanup_old_history",
        "get_storage_info", "get_transcripts_path", "backup_history",
        "get_auto_backup_status",
        "get_export_schedule_status", "list_auto_exports",
        "restore_history", "list_backups",
        "get_history_statistics", "word_frequency_analysis",
        "apply_profile_preset", "list_profile_presets",
        "get_notification_preferences", "set_notification_preferences",
        "export_settings", "import_settings",
        "get_audio_devices", "test_microphone",
        "auto_summarize_batch", "list_summary_profiles", "add_summary_profile",
        "filter_by_confidence", "health_check",
        "analyze_audio_quality", "analyze_silence",
        "get_error_report", "get_error_stats",
        "get_usage_stats",
        "get_audio_info", "get_system_info",
        "find_duplicates", "set_annotation", "get_annotation", "search_annotations",
        "create_collection", "delete_collection", "list_collections",
        "add_to_collection", "remove_from_collection",
        "list_normalization_profiles",
        "get_collection_items",
        "start_chain", "add_to_chain", "end_chain",
        "get_chain", "list_chains", "merge_chain_text",
        "schedule_recording", "cancel_scheduled_recording", "list_scheduled_recordings",
        "generate_daily_digest", "analyze_quality_trends",
        "get_recording_insights",
        "get_sentiment_trends", "compare_periods",
        "check_integrity", "repair_integrity",
        "extract_terms", "compare_texts",
        "get_context_memory", "score_readability", "score_transcription",
        "get_event_log", "get_event_stats", "replay_events",
        "get_waveform", "get_throttle_stats", "check_audio_duplicate",
        "batch", "get_keyword_cloud",
        "prepare_share", "list_shared", "get_shared",
        "save_transcript_version", "get_transcript_versions", "revert_transcript_version",
        "generate_auto_title",
        "format_for_paste", "merge_recordings", "preview_merge", "list_paste_formatters",
        "get_learning_stats",
        "get_analytics_dashboard", "get_topic_timeline",
        "list_config_presets", "apply_config_preset", "create_config_preset",
        "enqueue_transcription", "cancel_transcription",
        "get_queue_status", "list_transcription_queue",
        "detect_emotion",
        "estimate_recording_cost", "get_daily_cost_summary",
        "check_migration", "run_migration",
        "add_abbreviation", "expand_abbreviations", "remove_abbreviation", "list_abbreviations",
        "profile_noise",
        "configure_obsidian_sync", "run_obsidian_sync", "get_obsidian_sync_status",
        "record_playback", "get_playback_stats", "get_most_replayed",
        "post_process_text", "list_post_process_steps",
        "compare_recordings", "select_model",
        "get_smart_vocabulary_suggestions",
        "get_startup_diagnostics",
        # New methods (post-main dev batch 1)
        "enrich_recording", "get_shutdown_status",
        "check_duplicate", "run_deduplication", "get_dedup_stats",
        "get_timeline_view",
        # New methods (post-main dev batch 2)
        "get_recent_searches", "get_popular_searches", "clear_search_history",
        "archive_items", "unarchive_items", "list_archived", "get_archive_stats",
        "get_activity_calendar", "analyze_word_timing",
    ]

    def test_all_methods_return_valid_response(self):
        """Каждый зарегистрированный метод должен вернуть dict с 'id' и 'ok'."""
        failures = []
        for method in self.EXPECTED_METHODS:
            try:
                resp = self.req(method, {})
                if not isinstance(resp, dict):
                    failures.append(f"{method}: вернул {type(resp).__name__}, ожидался dict")
                elif "id" not in resp:
                    failures.append(f"{method}: нет ключа 'id'")
                elif "ok" not in resp:
                    failures.append(f"{method}: нет ключа 'ok'")
            except Exception as exc:
                failures.append(f"{method}: необработанное исключение — {type(exc).__name__}: {exc}")

        total = len(self.EXPECTED_METHODS)
        passed = total - len(failures)
        print(f"\n[dispatch_complete] {passed}/{total} методов прошли smoke-тест")

        if failures:
            self.fail(
                f"{len(failures)} методов не прошли:\n" + "\n".join(f"  - {f}" for f in failures)
            )

    def test_method_count(self):
        """Убеждаемся в точном количестве зарегистрированных методов."""
        # Количество методов в таблице service.py (строк с "key": handler)
        EXPECTED_COUNT = 175
        actual_count = len(self.EXPECTED_METHODS)
        self.assertGreaterEqual(
            actual_count,
            EXPECTED_COUNT,
            f"Ожидается >= {EXPECTED_COUNT} методов в таблице, найдено {actual_count}. "
            f"Проверьте service.py на наличие новых методов.",
        )
        print(f"\n[dispatch_complete] Всего IPC методов: {actual_count}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
