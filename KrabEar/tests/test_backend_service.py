"""Интеграционные тесты команд backend-сервиса Krab Ear."""

from __future__ import annotations
from KrabEar.__version__ import __version__ as APP_VERSION
from backend.translator import TranslationResult
from backend.state_store import StateStore
from backend.service import BackendService

from pathlib import Path
import itertools
import json
import sys
import tempfile
import time
import threading
import unittest

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeRecorder:
    """Фейковый рекордер для детерминированных тестов сервиса."""

    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000
        self._snapshot_counter = 0
        self._virtual_elapsed_sec = 0.0
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

    # R3 (610f8712): _preview_loop перешёл со скользящего окна на курсор и
    # требует именно эту пару методов — без них он молча уходит в continue
    # («not callable») и превью не обновляется НИКОГДА. Фейк обязан повторять
    # контракт AudioRecorder, а не только имена методов.

    def get_duration_sec(self) -> float:
        """Растущая длительность записи (реальный рекордер меряет от _started_at).

        Каждый опрос двигает виртуальные часы на секунду: тесту не нужно ждать
        настенного времени, а хвост детерминированно перерастает пороги
        _PREVIEW_MIN_TAIL_SEC / _PREVIEW_COMMIT_MIN_SEC.
        """
        if not self.is_recording:
            return 0.0
        self._virtual_elapsed_sec += 1.0
        return self._virtual_elapsed_sec

    def snapshot_range(self, from_sec: float, to_sec: float):
        """Срез буфера по диапазону от начала записи (контракт AudioRecorder).

        Вырожденный диапазон → пустой массив: ровно так ведёт себя реальный
        рекордер, и _preview_loop на это опирается (пустой срез = ждать чанков,
        а не звать STT).
        """
        if to_sec <= from_sec:
            return np.array([], dtype=np.float32)
        return np.ones(int((to_sec - from_sec) * self.sample_rate), dtype=np.float32)


class SilentRecorder(FakeRecorder):
    """Фейковый рекордер, который возвращает тишину для проверки silence-guard."""

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        return np.zeros(32000, dtype=np.float32), 1.0


class LowBackgroundRecorder(FakeRecorder):
    """Фейковый рекордер с низкоуровневым равномерным фоном (похоже на ТВ издалека)."""

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        # Низкая амплитуда + равномерная энергия, чтобы сработал background guard.
        data = np.full(32000, 0.0025, dtype=np.float32)
        return data, 1.0


class LoudUniformBackgroundRecorder(FakeRecorder):
    """Фейковый рекордер с громким, но равномерным фоном (ролик без микропауз)."""

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.last_stop_timeout_sec = timeout_sec
        self.last_stop_trim_ms = trim_tail_ms
        data = np.full(96000, 0.015, dtype=np.float32)
        return data, 6.0


class _FakeEngine:
    """Минимальный stub AudioEngine для _handle_get_diagnostics."""
    quality_profile: str = "balanced"
    current_model: str = "fake-model"

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class FakeTranscriber:
    """Фейковый transcriber, генерирующий последовательные строки."""

    def __init__(self) -> None:
        self.counter = 0
        self.preview_counter = 0
        self.engine = _FakeEngine()

    def transcribe(self, audio_data, quality_profile: str = "balanced", cleanup_profile: str = "soft",
                   domain: str = "casual", extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None, settings=None,
                   diarize=None, skip_vad_prefilter=False, silence_ranges=None) -> str:
        self.counter += 1
        return f"тестовая строка #{self.counter} ({quality_profile}/{cleanup_profile})"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        self.preview_counter += 1
        return f"preview#{self.preview_counter} ({quality_profile})"


class FakeTranslator:
    """Фейковый переводчик для проверки pipeline и офлайн-контракта."""

    def __init__(self) -> None:
        self.last_mode = "off"
        self.last_network_mode = "offline_default"

    def translate(
        self,
        text: str,
        mode: str,
        network_mode: str,
        translation_style: str = "neutral",
        glossary: dict[str, str] | None = None,
    ) -> TranslationResult:
        self.last_mode = mode
        self.last_network_mode = network_mode

        normalized = mode.strip().lower()
        if normalized == "off":
            return TranslationResult(
                text="",
                status="not_requested",
                source_lang="",
                target_lang="",
                mode="off",
                engine="fake",
            )

        if normalized == "ru_to_es":
            return TranslationResult(
                text=f"ES:{text}",
                status="ok",
                source_lang="ru",
                target_lang="es",
                mode="ru_to_es",
                engine="fake",
            )

        if normalized == "es_to_ru":
            return TranslationResult(
                text=f"RU:{text}",
                status="ok",
                source_lang="es",
                target_lang="ru",
                mode="es_to_ru",
                engine="fake",
            )

        if normalized == "en_to_ru":
            return TranslationResult(
                text=f"RU:{text}",
                status="ok",
                source_lang="en",
                target_lang="ru",
                mode="en_to_ru",
                engine="fake",
            )

        if normalized == "bilingual_ru_es":
            return TranslationResult(
                text=f"RU: {text}\nES: ES:{text}",
                status="ok",
                source_lang="ru",
                target_lang="ru+es",
                mode="bilingual_ru_es",
                engine="fake",
            )

        return TranslationResult(
            text="",
            status="cannot_detect_language",
            source_lang="",
            target_lang="",
            mode=normalized,
            engine="fake",
        )


class BackendServiceTestCase(unittest.TestCase):
    """Проверяет командный контракт сервиса, включая 1000 циклов записи."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService starts background threads that
        # may write to data dir after the test ends → OSError on cleanup in CI.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        # Stop BackendService daemon threads (DiskSpaceMonitor, RecapScheduler,
        # ExportScheduler, LLMHttpProbe) before the test process exits.  Without
        # this, daemon threads may attempt to log to stderr during interpreter
        # shutdown, which triggers the fatal "could not acquire lock for
        # <_io.BufferedWriter name='<stderr>'>" error in chunked CI runs.
        self.service.close()

    def request(self, method: str, params=None, request_id="t1"):
        return self.service.handle_request(
            {"id": request_id, "method": method, "params": params or {}}
        )

    def _stub_preview_stt(self, text: str) -> threading.Event:
        """Подменяет transcribe_preview и сигналит, когда loop ПРОШЁЛ итерацию.

        Событие ставится на ВТОРОМ вызове, а не на первом: к этому моменту loop
        успел полностью обработать результат первого — значит, если бы он всё же
        записывал забракованный текст в превью, запись уже случилась бы и
        проверка на пустоту поймала бы это, а не проскочила по гонке.

        Ждать ``_preview_updated_at`` здесь больше нельзя: после R3 (610f8712)
        забракованный текст сознательно НЕ трогает отображение («иначе был бы
        виден откат текста»), поэтому таймстемп и не обязан двигаться. Тест
        проверяет своё исходное намерение — мусор не попадает в превью.
        """
        ran = threading.Event()
        calls = itertools.count(1)

        def _fake_preview(audio_data, quality_profile: str = "balanced") -> str:
            if next(calls) >= 2:
                ran.set()
            return text

        self.service.transcriber.transcribe_preview = _fake_preview  # type: ignore[method-assign]
        return ran

    def _wait_for_preview_update(self, timeout: float = 5.0) -> bool:
        """Ждёт детерминистически, пока preview loop сделает хотя бы одну итерацию.

        Опрашивает ``service._preview_updated_at``, которое атомарно обновляется
        вместе с ``_preview_text`` внутри ``_preview_lock``.  Использует
        ``threading.Event`` вместо фиксированного ``time.sleep()``, поэтому
        не зависит от нагрузки CI-раннера.
        """
        done = threading.Event()
        snapshot = self.service._preview_updated_at

        def _poll() -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with self.service._preview_lock:
                    current = self.service._preview_updated_at
                if current != snapshot:
                    done.set()
                    return
                time.sleep(0.05)

        t = threading.Thread(target=_poll, daemon=True)
        t.start()
        return done.wait(timeout)

    def test_ping_and_settings(self) -> None:
        ping = self.request("ping")
        self.assertTrue(ping["ok"])
        result = ping["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["service"], "krabear-backend")
        self.assertEqual(result["version"], APP_VERSION)
        self.assertGreaterEqual(result["uptime_sec"], 0)
        self.assertIn("is_recording", result)
        self.assertFalse(result["is_recording"])
        self.assertGreaterEqual(result["history_count"], 0)

        get_settings = self.request("get_settings")
        self.assertTrue(get_settings["ok"])
        self.assertEqual(get_settings["result"]["history_policy"], "unlimited")
        self.assertEqual(get_settings["result"]["translation_mode"], "off")
        self.assertEqual(get_settings["result"]["hotkey_profile"], "default")
        self.assertFalse(get_settings["result"]["translate_and_paste"])
        self.assertEqual(get_settings["result"]["translation_style"], "neutral")
        self.assertEqual(get_settings["result"]["clipboard_mode"], "always_copy")
        self.assertTrue(get_settings["result"]["audio_ducking_enabled"])
        self.assertEqual(get_settings["result"]["audio_ducking_percent"], 50)
        self.assertEqual(get_settings["result"]["overlay_opacity_percent"], 45)
        self.assertEqual(get_settings["result"]["voice_gateway_url"], "http://127.0.0.1:8090")
        self.assertEqual(get_settings["result"]["voice_gateway_api_key"], "")  # empty = not configured, stays empty
        self.assertEqual(get_settings["result"]["update_channel"], "stable")
        self.assertTrue(get_settings["result"]["call_notify_default"])
        self.assertTrue(get_settings["result"]["call_auto_summary"])
        self.assertEqual(get_settings["result"]["capture_source_mode"], "mic")
        self.assertEqual(get_settings["result"]["ui_last_tab"], "history")
        self.assertTrue(get_settings["result"]["history_focus_mode"])
        self.assertEqual(get_settings["result"]["history_text_density"], "normal")
        self.assertEqual(get_settings["result"]["stop_tail_trim_ms"], 180)
        self.assertTrue(get_settings["result"]["silence_guard_enabled"])
        self.assertEqual(get_settings["result"]["silence_guard_rms_threshold"], 0.0020)
        self.assertEqual(get_settings["result"]["silence_guard_peak_threshold"], 0.0120)
        self.assertEqual(get_settings["result"]["silence_guard_active_ratio_threshold"], 0.015)
        self.assertTrue(get_settings["result"]["background_guard_enabled"])
        self.assertEqual(get_settings["result"]["background_guard_min_peak"], 0.025)
        self.assertEqual(get_settings["result"]["background_guard_min_rms"], 0.0040)
        self.assertEqual(get_settings["result"]["background_guard_uniform_frame_threshold"], 0.0060)
        self.assertEqual(get_settings["result"]["background_guard_max_uniform_active_ratio"], 0.92)

        set_settings = self.request(
            "set_settings",
            {
                "mode": "menubar",
                "history_page_size": 77,
                "hotkey_profile": "meeting",
                "translation_mode": "ru_to_es",
                "translate_and_paste": True,
                "translation_style": "formal",
                "clipboard_mode": "copy_on_fail",
                "audio_ducking_enabled": False,
                "audio_ducking_percent": 75,
                "stop_tail_trim_ms": 260,
                "silence_guard_enabled": False,
                "silence_guard_rms_threshold": 0.0035,
                "silence_guard_peak_threshold": 0.0200,
                "silence_guard_active_ratio_threshold": 0.030,
                "background_guard_enabled": False,
                "background_guard_min_peak": 0.03,
                "background_guard_min_rms": 0.005,
                "background_guard_uniform_frame_threshold": 0.007,
                "background_guard_max_uniform_active_ratio": 0.88,
                "overlay_opacity_percent": 60,
                "voice_gateway_url": "http://127.0.0.1:9000",
                "voice_gateway_api_key": "token",
                "update_channel": "beta",
                "call_notify_default": False,
                "call_auto_summary": False,
                "capture_source_mode": "mic_plus_system",
                "ui_last_tab": "live_translation",
                "history_focus_mode": False,
                "history_text_density": "compact",
            },
        )
        self.assertTrue(set_settings["ok"])
        self.assertEqual(set_settings["result"]["mode"], "menubar")
        self.assertEqual(set_settings["result"]["history_page_size"], 77)
        self.assertEqual(set_settings["result"]["hotkey_profile"], "meeting")
        self.assertEqual(set_settings["result"]["translation_mode"], "ru_to_es")
        self.assertTrue(set_settings["result"]["translate_and_paste"])
        self.assertEqual(set_settings["result"]["translation_style"], "formal")
        self.assertEqual(set_settings["result"]["clipboard_mode"], "copy_on_fail")
        self.assertFalse(set_settings["result"]["audio_ducking_enabled"])
        self.assertEqual(set_settings["result"]["audio_ducking_percent"], 75)
        self.assertEqual(set_settings["result"]["stop_tail_trim_ms"], 260)
        self.assertFalse(set_settings["result"]["silence_guard_enabled"])
        self.assertEqual(set_settings["result"]["silence_guard_rms_threshold"], 0.0035)
        self.assertEqual(set_settings["result"]["silence_guard_peak_threshold"], 0.0200)
        self.assertEqual(set_settings["result"]["silence_guard_active_ratio_threshold"], 0.030)
        self.assertFalse(set_settings["result"]["background_guard_enabled"])
        self.assertEqual(set_settings["result"]["background_guard_min_peak"], 0.03)
        self.assertEqual(set_settings["result"]["background_guard_min_rms"], 0.005)
        self.assertEqual(set_settings["result"]["background_guard_uniform_frame_threshold"], 0.007)
        self.assertEqual(set_settings["result"]["background_guard_max_uniform_active_ratio"], 0.88)
        self.assertEqual(set_settings["result"]["overlay_opacity_percent"], 60)
        self.assertEqual(set_settings["result"]["voice_gateway_url"], "http://127.0.0.1:9000")
        self.assertEqual(set_settings["result"]["voice_gateway_api_key"], "REDACTED")  # wave-35: sensitive fields are redacted in responses
        self.assertEqual(set_settings["result"]["update_channel"], "beta")
        self.assertFalse(set_settings["result"]["call_notify_default"])
        self.assertFalse(set_settings["result"]["call_auto_summary"])
        self.assertEqual(set_settings["result"]["capture_source_mode"], "mic_plus_system")
        self.assertEqual(set_settings["result"]["ui_last_tab"], "live_translation")
        self.assertFalse(set_settings["result"]["history_focus_mode"])
        self.assertEqual(set_settings["result"]["history_text_density"], "compact")

    def test_get_history_overview_method(self) -> None:
        self.request(
            "add_history_item",
            {
                "text": "hola",
                "paste_status": "ok",
                "translation_mode": "ru_to_es",
                "translation_status": "ok",
            },
        )
        self.request(
            "add_history_item",
            {
                "text": "ошибка",
                "paste_status": "failed",
                "translation_mode": "ru_to_es",
                "translation_status": "translate_error",
            },
        )
        self.request(
            "add_history_item",
            {
                "text": "без перевода",
                "paste_status": "failed",
                "translation_mode": "off",
                "translation_status": "not_requested",
            },
        )
        response = self.request("get_history_overview")
        self.assertTrue(response["ok"])
        result = response["result"]
        self.assertEqual(result["active_count"], 3)
        self.assertEqual(result["paste_ok"], 1)
        self.assertEqual(result["paste_failed"], 2)
        self.assertEqual(result["translated_ok"], 1)
        self.assertEqual(result["translated_error"], 1)
        self.assertEqual(result["no_translation"], 1)
        self.assertIn("top_modes", result)

    def test_get_recording_stats_empty(self) -> None:
        """get_recording_stats на пустой истории возвращает нулевые значения."""
        resp = self.request("get_recording_stats")
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["total_count"], 0)
        self.assertEqual(r["total_duration_sec"], 0.0)
        self.assertEqual(r["today_count"], 0)
        self.assertEqual(r["avg_duration_sec"], 0.0)
        self.assertEqual(r["most_used_lang"], "")
        self.assertEqual(r["llm_correction_rate"], 0.0)
        self.assertEqual(r["diarization_usage_rate"], 0.0)
        self.assertEqual(r["lang_distribution"], [])

    def test_get_recording_stats_with_data(self) -> None:
        """get_recording_stats корректно агрегирует длительность, язык, LLM, диаризацию."""
        store = self.service.store
        # 3 записи: 2 с длительностью, 1 без; 2 ru, 1 es; 1 llm; 1 diarization
        store.add_history_item(
            text="первая запись",
            paste_status="ok",
            source_lang="ru",
            audio_duration_sec=10.5,
            llm_applied=True,
            llm_latency_ms=120,
        )
        store.add_history_item(
            text="segunda entrada",
            paste_status="ok",
            source_lang="es",
            audio_duration_sec=5.25,
            diarization={"enabled": True, "speakers_count": 2, "speaker_turns": []},
        )
        store.add_history_item(
            text="третья запись",
            paste_status="failed",
            source_lang="ru",
            audio_duration_sec=None,
        )
        resp = self.request("get_recording_stats")
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["total_count"], 3)
        self.assertEqual(r["total_duration_sec"], 15.75)
        self.assertEqual(r["today_count"], 3)
        self.assertAlmostEqual(r["avg_duration_sec"], 5.25, places=2)
        self.assertEqual(r["most_used_lang"], "ru")
        # lang_distribution: ru=2, es=1
        langs = {d["lang"]: d["count"] for d in r["lang_distribution"]}
        self.assertEqual(langs["ru"], 2)
        self.assertEqual(langs["es"], 1)
        # LLM: 1 out of 3
        self.assertEqual(r["llm_applied_count"], 1)
        self.assertAlmostEqual(r["llm_correction_rate"], 1 / 3, places=4)
        # Diarization: 1 out of 3
        self.assertEqual(r["diarization_used_count"], 1)
        self.assertAlmostEqual(r["diarization_usage_rate"], 1 / 3, places=4)
        # Week count should equal today count (all items are from today)
        self.assertEqual(r["week_count"], 3)

    def test_glossary_management(self) -> None:
        add = self.request(
            "set_translation_glossary_item",
            {"source": "cliente", "target": "клиент"},
        )
        self.assertTrue(add["ok"])
        settings = self.request("get_settings")
        self.assertEqual(settings["result"]["translation_glossary"].get("cliente"), "клиент")

        remove = self.request(
            "remove_translation_glossary_item",
            {"source": "cliente"},
        )
        self.assertTrue(remove["ok"])
        settings_after = self.request("get_settings")
        self.assertNotIn("cliente", settings_after["result"]["translation_glossary"])

    def test_settings_normalize_translation_mode(self) -> None:
        response = self.request("set_settings", {"translation_mode": "broken_mode"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["translation_mode"], "off")

        bilingual = self.request("set_settings", {"translation_mode": "bilingual_ru_es"})
        self.assertTrue(bilingual["ok"])
        self.assertEqual(bilingual["result"]["translation_mode"], "bilingual_ru_es")

        auto_to_ru = self.request("set_settings", {"translation_mode": "auto_to_ru"})
        self.assertTrue(auto_to_ru["ok"])
        self.assertEqual(auto_to_ru["result"]["translation_mode"], "auto_to_ru")

    def test_settings_normalize_call_assist_fields(self) -> None:
        response = self.request(
            "set_settings",
            {
                "capture_source_mode": "wrong",
                "ui_last_tab": "wrong",
                "voice_gateway_url": "  https://gateway.example.com  ",
                "voice_gateway_api_key": "  key  ",
                "call_auto_summary": "off",
                "hotkey_profile": "unknown_profile",
                "update_channel": "unknown",
                "text_templates": "not_dict",
                "history_focus_mode": "invalid_bool",
                "history_text_density": "invalid_density",
            },
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["capture_source_mode"], "mic")
        self.assertEqual(response["result"]["ui_last_tab"], "history")
        self.assertEqual(response["result"]["voice_gateway_url"], "https://gateway.example.com")
        self.assertEqual(response["result"]["voice_gateway_api_key"], "REDACTED")  # wave-35: non-empty sensitive fields redacted
        self.assertFalse(response["result"]["call_auto_summary"])
        self.assertEqual(response["result"]["hotkey_profile"], "default")
        self.assertEqual(response["result"]["update_channel"], "stable")
        self.assertIsInstance(response["result"]["text_templates"], dict)
        self.assertTrue(response["result"]["history_focus_mode"])
        self.assertEqual(response["result"]["history_text_density"], "normal")

    def test_settings_voice_gateway_url_whitelist(self) -> None:
        # Допустимые URL
        for valid_url in ["http://localhost:8090", "http://127.0.0.1:8090", "https://gw.example.com"]:
            resp = self.request("set_settings", {"voice_gateway_url": valid_url})
            self.assertTrue(resp["ok"], f"Expected ok for {valid_url}")
            self.assertEqual(resp["result"]["voice_gateway_url"], valid_url)
        # Недопустимый URL — должен вернуть ошибку
        bad_resp = self.request("set_settings", {"voice_gateway_url": "http://evil.internal/steal"})
        self.assertFalse(bad_resp["ok"], "Expected rejection of non-localhost HTTP URL")

    def test_summarize_text_method(self) -> None:
        summary_short = self.request(
            "summarize_text",
            {
                "text": "Первое предложение. Второе предложение! Третье предложение?",
                "mode": "summary_short",
                "max_points": 2,
            },
        )
        self.assertTrue(summary_short["ok"])
        self.assertEqual(summary_short["result"]["mode"], "summary_short")
        self.assertTrue(summary_short["result"]["summary"])
        self.assertGreaterEqual(len(summary_short["result"]["bullets"]), 1)

        summary_detailed = self.request(
            "summarize_text",
            {
                "text": "Один. Два. Три. Четыре.",
                "mode": "summary_detailed",
                "max_points": 3,
            },
        )
        self.assertTrue(summary_detailed["ok"])
        self.assertEqual(summary_detailed["result"]["mode"], "summary_detailed")
        self.assertGreaterEqual(len(summary_detailed["result"]["bullets"]), 2)

    def test_settings_normalize_audio_ducking_percent(self) -> None:
        too_high = self.request("set_settings", {"audio_ducking_percent": 999})
        self.assertTrue(too_high["ok"])
        self.assertEqual(too_high["result"]["audio_ducking_percent"], 100)
        bad_type = self.request("set_settings", {"audio_ducking_percent": "oops"})
        self.assertTrue(bad_type["ok"])
        self.assertEqual(bad_type["result"]["audio_ducking_percent"], 50)

    def test_settings_normalize_stop_tail_trim_ms(self) -> None:
        too_high = self.request("set_settings", {"stop_tail_trim_ms": 9999})
        self.assertTrue(too_high["ok"])
        self.assertEqual(too_high["result"]["stop_tail_trim_ms"], 1200)
        bad_type = self.request("set_settings", {"stop_tail_trim_ms": "oops"})
        self.assertTrue(bad_type["ok"])
        self.assertEqual(bad_type["result"]["stop_tail_trim_ms"], 180)

    def test_settings_normalize_silence_guard_thresholds(self) -> None:
        invalid = self.request(
            "set_settings",
            {
                "silence_guard_rms_threshold": "oops",
                "silence_guard_peak_threshold": "oops",
                "silence_guard_active_ratio_threshold": "oops",
            },
        )
        self.assertTrue(invalid["ok"])
        self.assertEqual(invalid["result"]["silence_guard_rms_threshold"], 0.0020)
        self.assertEqual(invalid["result"]["silence_guard_peak_threshold"], 0.0120)
        self.assertEqual(invalid["result"]["silence_guard_active_ratio_threshold"], 0.015)

        too_high = self.request(
            "set_settings",
            {
                "silence_guard_rms_threshold": 9.0,
                "silence_guard_peak_threshold": 9.0,
                "silence_guard_active_ratio_threshold": 9.0,
            },
        )
        self.assertTrue(too_high["ok"])
        self.assertEqual(too_high["result"]["silence_guard_rms_threshold"], 0.05)
        self.assertEqual(too_high["result"]["silence_guard_peak_threshold"], 0.2)
        self.assertEqual(too_high["result"]["silence_guard_active_ratio_threshold"], 0.30)

    def test_settings_normalize_background_guard_thresholds(self) -> None:
        invalid = self.request(
            "set_settings",
            {
                "background_guard_min_peak": "oops",
                "background_guard_min_rms": "oops",
                "background_guard_uniform_frame_threshold": "oops",
                "background_guard_max_uniform_active_ratio": "oops",
            },
        )
        self.assertTrue(invalid["ok"])
        self.assertEqual(invalid["result"]["background_guard_min_peak"], 0.025)
        self.assertEqual(invalid["result"]["background_guard_min_rms"], 0.0040)
        self.assertEqual(invalid["result"]["background_guard_uniform_frame_threshold"], 0.0060)
        self.assertEqual(invalid["result"]["background_guard_max_uniform_active_ratio"], 0.92)

        too_high = self.request(
            "set_settings",
            {
                "background_guard_min_peak": 9.0,
                "background_guard_min_rms": 9.0,
                "background_guard_uniform_frame_threshold": 9.0,
                "background_guard_max_uniform_active_ratio": 9.0,
            },
        )
        self.assertTrue(too_high["ok"])
        self.assertEqual(too_high["result"]["background_guard_min_peak"], 0.25)
        self.assertEqual(too_high["result"]["background_guard_min_rms"], 0.08)
        self.assertEqual(too_high["result"]["background_guard_uniform_frame_threshold"], 0.2)
        self.assertEqual(too_high["result"]["background_guard_max_uniform_active_ratio"], 0.99)

    def test_settings_normalize_overlay_opacity_percent(self) -> None:
        too_low = self.request("set_settings", {"overlay_opacity_percent": 1})
        self.assertTrue(too_low["ok"])
        self.assertEqual(too_low["result"]["overlay_opacity_percent"], 15)
        too_high = self.request("set_settings", {"overlay_opacity_percent": 999})
        self.assertTrue(too_high["ok"])
        self.assertEqual(too_high["result"]["overlay_opacity_percent"], 90)

    def test_recording_flow(self) -> None:
        self.assertTrue(self.request("start_recording")["ok"])
        # is_recording flag обновляется синхронно в handle_request → sleep не нужен
        state = self.request("get_recording_state")
        self.assertTrue(state["ok"])
        self.assertTrue(state["result"]["is_recording"])
        self.assertIn("preview_text", state["result"])
        stop = self.request("stop_recording", {"quality_profile": "max"})
        self.assertTrue(stop["ok"])
        self.assertTrue(stop["result"]["text"].lower().startswith("тестовая строка"))
        self.assertIsNotNone(stop["result"]["history_id"])
        self.assertEqual(stop["result"]["cleanup_profile"], "soft")
        self.assertEqual(stop["result"]["stop_tail_trim_ms"], 180)
        self.assertEqual(self.service.recorder.last_stop_trim_ms, 180)

    def test_recording_flow_accepts_stop_tail_trim_override(self) -> None:
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "max", "stop_tail_trim_ms": 320})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["stop_tail_trim_ms"], 320)
        self.assertEqual(self.service.recorder.last_stop_trim_ms, 320)

    def test_stop_recording_silence_guard_skips_transcription(self) -> None:
        self.service.recorder = SilentRecorder()
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "empty_audio")
        self.assertTrue(stop["result"]["silence_detected"])
        self.assertEqual(self.service.transcriber.counter, 0)

    def test_stop_recording_background_guard_skips_distant_audio(self) -> None:
        self.service.recorder = LowBackgroundRecorder()
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "empty_audio")
        self.assertTrue(
            bool(stop["result"].get("background_guard_rejected"))
            or bool(stop["result"].get("silence_detected"))
        )
        self.assertEqual(self.service.transcriber.counter, 0)

    def test_stop_recording_background_guard_skips_loud_uniform_background(self) -> None:
        self.service.recorder = LoudUniformBackgroundRecorder()
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "empty_audio")
        self.assertTrue(bool(stop["result"].get("background_guard_rejected")))
        self.assertEqual(self.service.transcriber.counter, 0)

    def test_stop_recording_postprocesses_punctuation_and_case(self) -> None:
        self.service.transcriber.transcribe = (  # type: ignore[method-assign]
            lambda audio_data, quality_profile="balanced", cleanup_profile="soft", **kw: {
                "text": "это быстрый тест без пауз и запятых",
                "status": "ok",
                "engine": "fake",
            }
        )
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "ok")
        self.assertEqual(stop["result"]["original_text"], "Это быстрый тест без пауз и запятых.")

    def test_stop_recording_drops_repeated_prompt_artifact(self) -> None:
        self.service.transcriber.transcribe = (  # type: ignore[method-assign]
            lambda audio_data, quality_profile="balanced", cleanup_profile="soft", **kw: {
                "text": (
                    "Сохраняй смысл, ставь корректную пунктуацию, "
                    "сохраняй смысл, ставь корректную пунктуацию, "
                    "сохраняй смысл, ставь корректную пунктуацию."
                ),
                "status": "ok",
                "engine": "fake",
            }
        )
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "empty_text")

    def test_stop_recording_drops_prompt_echo_inside_longer_phrase(self) -> None:
        self.service.transcriber.transcribe = (  # type: ignore[method-assign]
            lambda audio_data, quality_profile="balanced", cleanup_profile="soft", **kw: {
                "text": (
                    "Ну а вот это было с хмыком без речи, "
                    "сохраняй смысл, ставь корректную пункту, "
                    "сохраняй смысл, ставь корректную пункту."
                ),
                "status": "ok",
                "engine": "fake",
            }
        )
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "empty_text")

    def test_stop_recording_accepts_dict_transcriber_payload(self) -> None:
        self.service.transcriber.transcribe = (  # type: ignore[method-assign]
            lambda audio_data, quality_profile="balanced", cleanup_profile="soft", **kw: {
                "text": f"dict payload ({quality_profile}/{cleanup_profile})",
                "status": "ok",
                "engine": "fake",
            }
        )
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced", "cleanup_profile": "soft"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["status"], "ok")
        self.assertTrue(stop["result"]["text"].lower().startswith("dict payload"))
        self.assertIsNotNone(stop["result"]["history_id"])

    def test_preview_accepts_dict_transcriber_payload(self) -> None:
        self.service.transcriber.transcribe_preview = (  # type: ignore[method-assign]
            lambda audio_data, quality_profile="balanced": {"text": f"preview-dict ({quality_profile})"}
        )
        self.assertTrue(self.request("start_recording")["ok"])
        self.assertTrue(self._wait_for_preview_update(timeout=5.0), "preview не обновился за 5s")
        state = self.request("get_recording_state")
        self.assertTrue(state["ok"])
        self.assertIn("preview-dict", state["result"]["preview_text"])
        self.assertTrue(self.request("stop_recording", {"quality_profile": "balanced"})["ok"])

    def test_preview_drops_prompt_echo_and_clears_state(self) -> None:
        stt_ran = self._stub_preview_stt(
            "Сохраняй смысл, ставь корректную пунктуацию, "
            "сохраняй смысл, ставь корректную пунктуацию."
        )
        self.assertTrue(self.request("start_recording")["ok"])
        self.assertTrue(stt_ran.wait(timeout=5.0), "preview loop не прогнал STT за 5s")
        state = self.request("get_recording_state")
        self.assertTrue(state["ok"])
        self.assertEqual(state["result"]["preview_text"], "")
        self.assertTrue(self.request("stop_recording", {"quality_profile": "balanced"})["ok"])

    def test_preview_drops_looping_noise(self) -> None:
        stt_ran = self._stub_preview_stt("ой ой ой ой ой ой ой ой")
        self.assertTrue(self.request("start_recording")["ok"])
        self.assertTrue(stt_ran.wait(timeout=5.0), "preview loop не прогнал STT за 5s")
        state = self.request("get_recording_state")
        self.assertTrue(state["ok"])
        self.assertEqual(state["result"]["preview_text"], "")
        self.assertTrue(self.request("stop_recording", {"quality_profile": "balanced"})["ok"])

    def test_close_stops_recording_workers_before_store_cleanup(self) -> None:
        """close() завершает recording-workers до удаления их StateStore."""
        settings = self.request(
            "set_settings",
            {
                "realtime_silence_filter_enabled": True,
                "rt_silence_check_sec": 0.5,
            },
        )
        self.assertTrue(settings["ok"])
        self.assertTrue(self.request("start_recording")["ok"])
        recording_core = self.service._recording_core_svc
        # Страховки регистрируются до проверок живости: любой ранний RED всё
        # равно остановит worker-ы раньше TemporaryDirectory.cleanup.
        self.addCleanup(self.service.recorder.stop)
        self.addCleanup(recording_core._stop_preview_worker)
        preview_thread = recording_core._preview_thread
        rt_partial = recording_core._rt_partial
        rsf = recording_core._rsf
        if rt_partial is not None:
            self.addCleanup(rt_partial.stop)
        if rsf is not None:
            self.addCleanup(rsf.stop)
        self.assertIsNotNone(preview_thread)
        self.assertIsNotNone(rt_partial)
        self.assertIsNotNone(rsf)
        self.assertTrue(preview_thread.is_alive())
        self.assertTrue(rt_partial.is_running)
        with rsf._lock:
            rsf_thread = rsf._thread
        self.assertIsNotNone(rsf_thread)
        self.assertTrue(rsf_thread.is_alive())

        close_returned = threading.Event()
        store_access_after_close = threading.Event()
        original_load_settings = self.service.store.load_settings

        def tracked_load_settings():
            if close_returned.is_set():
                store_access_after_close.set()
            return original_load_settings()

        self.service.store.load_settings = tracked_load_settings  # type: ignore[method-assign]
        self.service._settings_svc._cache_ttl = 0.0

        self.assertTrue(self.service.close())
        close_returned.set()

        self.assertFalse(
            store_access_after_close.wait(timeout=0.8),
            "preview worker обратился к StateStore после возврата close()",
        )
        self.assertFalse(
            preview_thread.is_alive(),
            "preview worker остался жив после BackendService.close()",
        )
        self.assertFalse(rt_partial.is_running)
        rsf_thread.join(timeout=1.0)
        self.assertFalse(rsf_thread.is_alive())
        self.assertIsNone(recording_core._rt_partial)
        self.assertIsNone(recording_core._rsf)
        self.assertFalse(self.service.recorder.is_recording)

    def test_close_isolates_preview_stop_error_before_other_teardown(self) -> None:
        """Ошибка preview-stop не мешает закрытию остальных фоновых сервисов."""
        from unittest.mock import MagicMock, patch

        calls: list[str] = []
        recording_core = self.service._recording_core_svc
        rt_partial = MagicMock()
        rsf = MagicMock()
        with recording_core._rt_lock:
            recording_core._rt_partial = rt_partial
            recording_core._rsf = rsf
        self.service.recorder.start()

        def fail_preview_stop() -> None:
            calls.append("preview")
            raise RuntimeError("preview stop failed")

        rt_partial.stop.side_effect = (
            lambda **_kwargs: calls.append("rt_partial") or True
        )
        rsf.stop.side_effect = lambda **_kwargs: calls.append("rsf") or []
        rsf.is_running = False

        def track_recorder_stop() -> None:
            calls.append("recorder")
            self.service.recorder.is_recording = False

        def track_disk_stop() -> None:
            calls.append("disk")

        with patch.object(
            recording_core,
            "_stop_preview_worker",
            side_effect=fail_preview_stop,
        ), patch.object(
            self.service.recorder,
            "stop",
            side_effect=track_recorder_stop,
        ), patch.object(
            self.service._disk_monitor,
            "stop",
            side_effect=track_disk_stop,
        ):
            self.assertFalse(self.service.close())

        self.assertEqual(
            calls,
            ["preview", "rt_partial", "rsf", "recorder", "disk"],
        )

    def test_start_recording_is_idempotent(self) -> None:
        first = self.request("start_recording")
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["status"], "recording")

        second = self.request("start_recording")
        self.assertTrue(second["ok"])
        self.assertEqual(second["result"]["status"], "already_recording")
        self.assertTrue(second["result"]["is_recording"])

    def test_stop_recording_is_idempotent(self) -> None:
        first = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["status"], "already_stopped")
        self.assertFalse(first["result"]["is_recording"])

        self.assertTrue(self.request("start_recording")["ok"])
        second = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(second["ok"])
        self.assertEqual(second["result"]["status"], "ok")

    def test_translation_pipeline_and_inserted_text(self) -> None:
        self.assertTrue(
            self.request(
                "set_settings",
                {
                    "translation_mode": "ru_to_es",
                    "translate_and_paste": True,
                    "network_mode": "offline_default",
                },
            )["ok"]
        )
        self.assertTrue(self.request("start_recording")["ok"])
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["translation_mode"], "ru_to_es")
        self.assertEqual(stop["result"]["translation_status"], "ok")
        self.assertTrue(stop["result"]["translated_text"].startswith("ES:"))
        self.assertEqual(stop["result"]["text"], stop["result"]["translated_text"])
        self.assertTrue(stop["result"]["original_text"].lower().startswith("тестовая строка"))

    def test_translate_text_method(self) -> None:
        response = self.request(
            "translate_text",
            {
                "text": "привет",
                "translation_mode": "ru_to_es",
            },
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["status"], "ok")
        self.assertEqual(response["result"]["translation_mode"], "ru_to_es")
        self.assertTrue(response["result"]["text"].startswith("ES:"))

    def test_call_assist_flow_and_list_audio_inputs(self) -> None:
        class _MockGW:
            def start_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "session_id": "gw-session-1"}

            def stop_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True}

            def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "payload": {}}

            def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "payload": {}}

            def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "payload": {}}
        self.service._call_assist.gateway = _MockGW()  # type: ignore[assignment]
        self.service._list_audio_inputs = lambda: [  # type: ignore[method-assign]
            {
                "id": 3,
                "name": "BlackHole 2ch",
                "is_default": True,
                "tags": ["loopback"],
            }
        ]
        self.assertTrue(self.request("set_settings", {"call_auto_summary": False})["ok"])

        start = self.request(
            "start_call_assist",
            {
                "translation_mode": "auto_to_ru",
                "notify_mode": "auto_off",
                "tts_mode": "hybrid",
            },
        )
        self.assertTrue(start["ok"])
        self.assertTrue(start["result"]["active"])
        self.assertEqual(start["result"]["status"], "running")
        self.assertEqual(start["result"]["gateway_session_id"], "gw-session-1")
        self.assertEqual(start["result"]["notify_mode"], "auto_off")
        self.assertEqual(start["result"]["translation_mode"], "auto_to_ru")

        state = self.request("get_call_assist_state")
        self.assertTrue(state["ok"])
        self.assertTrue(state["result"]["active"])

        inputs = self.request("list_audio_inputs")
        self.assertTrue(inputs["ok"])
        self.assertEqual(inputs["result"]["count"], 1)
        self.assertEqual(inputs["result"]["default_input_id"], 3)

        stop = self.request("stop_call_assist", {"auto_summary": False})
        self.assertTrue(stop["ok"])
        self.assertFalse(stop["result"]["active"])
        self.assertEqual(stop["result"]["status"], "stopped")
        self.assertEqual(stop["result"]["summary_status"], "skipped")

    def test_call_assist_stop_auto_summary_saves_to_history(self) -> None:
        called_summary_paths: list[str] = []
        called_summary_payloads: list[dict[str, object]] = []

        class _MockGW:
            def start_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "session_id": "gw-session-summary-1"}

            def stop_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True}

            def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "payload": {}}

            def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                # Support both positional (url, api_key, path, payload) and keyword args
                path = str(args[2] if len(args) > 2 else kwargs.get("path", ""))
                payload = args[3] if len(args) > 3 else kwargs.get("payload", {})
                if path.endswith("/summary"):
                    called_summary_paths.append(path)
                    called_summary_payloads.append(dict(payload) if isinstance(payload, dict) else {})
                    return {
                        "ok": True,
                        "payload": {
                            "summary": "Обсудили доставку и документы.",
                            "tasks": ["Отправить копию паспорта", "Подтвердить адрес"],
                        },
                    }
                return {"ok": True, "payload": {"ok": True}}

            def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "payload": {}}

        self.service._call_assist.gateway = _MockGW()  # type: ignore[assignment]

        start = self.request("start_call_assist", {"translation_mode": "auto_to_ru"})
        self.assertTrue(start["ok"])
        self.assertEqual(start["result"]["gateway_session_id"], "gw-session-summary-1")

        stop = self.request("stop_call_assist", {"auto_summary": True, "summary_max_items": 12})
        self.assertTrue(stop["ok"])
        self.assertEqual(stop["result"]["summary_status"], "ok")
        self.assertIn("summary", stop["result"])
        self.assertTrue(stop["result"].get("summary_history_id"))
        self.assertEqual(called_summary_paths, ["/v1/sessions/gw-session-summary-1/summary"])
        self.assertEqual(called_summary_payloads[0]["max_items"], 12)

        page = self.request("get_history_page", {"cursor": None, "limit": 20})
        self.assertTrue(page["ok"])
        texts = [item["text"] for item in page["result"]["items"]]
        self.assertTrue(any("[Call Summary]" in text for text in texts))

    def test_call_assist_gateway_tools(self) -> None:
        get_paths: list[str] = []
        post_paths: list[str] = []
        delete_paths: list[str] = []

        class _MockGW:
            def start_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True, "session_id": "gw-session-77"}

            def stop_session(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                return {"ok": True}

            def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                path = str(args[2] if len(args) > 2 else kwargs.get("path", ""))
                get_paths.append(path)
                if path.startswith("/v1/telephony/cost/estimate"):
                    return {
                        "ok": True,
                        "payload": {
                            "ok": True,
                            "country": "ES",
                            "rates_source": "manual",
                            "telephony_usd": {"total": 1.3},
                            "ai_usd": {"total": 0.8},
                            "total_usd": 2.1,
                        },
                    }
                if "/timeline/summary" in path:
                    return {
                        "ok": True,
                        "payload": {
                            "ok": True,
                            "summary": "Краткая сводка звонка",
                            "tasks": ["Отправить договор", "Перезвонить завтра"],
                            "stats": {"count": 4},
                            "items_used": 4,
                        },
                    }
                if "/timeline/stats" in path:
                    return {
                        "ok": True,
                        "payload": {
                            "ok": True,
                            "stats": {
                                "count": 4,
                                "text_chars": 40,
                                "first_ts": "2026-02-12T10:00:00+00:00",
                                "last_ts": "2026-02-12T10:00:10+00:00",
                                "by_kind": {"stt.partial": 2, "translation.partial": 2},
                            },
                        },
                    }
                if "/timeline/export" in path:
                    return {"ok": True, "payload": {"ok": True, "format": "md", "content": "# timeline\nentry"}}
                if "/timeline" in path:
                    return {"ok": True, "payload": {"ok": True, "count": 2, "items": [{"kind": "stt.partial", "text": "x"}]}}
                return {"ok": True, "payload": {"ok": True, "count": 2, "items": [{"source_text": "x"}]}}

            def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                path = str(args[2] if len(args) > 2 else kwargs.get("path", ""))
                post_paths.append(path)
                return {"ok": True, "payload": {"ok": True, "summary": "sum", "tasks": [], "translated_text": "hola"}}

            def delete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                path = str(args[2] if len(args) > 2 else kwargs.get("path", ""))
                delete_paths.append(path)
                return {"ok": True, "payload": {"ok": True, "before": 10, "after": 1, "keep_last": 1}}

        self.service._call_assist.gateway = _MockGW()  # type: ignore[assignment]

        start = self.request("start_call_assist", {"translation_mode": "auto_to_ru"})
        self.assertTrue(start["ok"])
        self.assertEqual(start["result"]["gateway_session_id"], "gw-session-77")

        diag = self.request("call_assist_diagnostics", {"include_why": True})
        self.assertTrue(diag["ok"])
        self.assertEqual(diag["result"]["gateway_session_id"], "gw-session-77")
        self.assertIn("diagnostics", diag["result"])

        summary = self.request("call_assist_summary", {"max_items": 40})
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["result"]["gateway_session_id"], "gw-session-77")
        self.assertIn("summary", summary["result"])

        quick = self.request(
            "call_assist_quick_phrase",
            {"text": "Говорите медленнее", "source_lang": "ru", "target_lang": "es"},
        )
        self.assertTrue(quick["ok"])
        self.assertEqual(quick["result"]["gateway_session_id"], "gw-session-77")
        self.assertIn("quick_phrase", quick["result"])

        phrases = self.request(
            "list_call_assist_quick_phrases",
            {"source_lang": "ru", "target_lang": "es", "limit": 5},
        )
        self.assertTrue(phrases["ok"])
        self.assertEqual(phrases["result"]["ok"], True)

        cost = self.request(
            "call_assist_cost_estimate",
            {
                "country": "ES",
                "use_live_pricing": False,
                "minutes_inbound": 10,
                "minutes_outbound_landline": 20,
                "minutes_outbound_mobile": 30,
                "minutes_media_stream": 60,
                "inbound_rate_override": 0.01,
                "outbound_landline_rate_override": 0.02,
                "outbound_mobile_rate_override": 0.03,
            },
        )
        self.assertTrue(cost["ok"])
        self.assertEqual(cost["result"]["country"], "ES")
        self.assertGreater(cost["result"]["total_usd"], 0)

        timeline = self.request(
            "call_assist_timeline",
            {"limit": 25, "kind": "translation.partial", "contains": "hola"},
        )
        self.assertTrue(timeline["ok"])
        self.assertEqual(timeline["result"]["count"], 2)

        timeline_export = self.request(
            "call_assist_timeline_export",
            {"format": "md", "limit": 100},
        )
        self.assertTrue(timeline_export["ok"])
        self.assertEqual(timeline_export["result"]["format"], "md")
        self.assertIn("content", timeline_export["result"])

        timeline_stats = self.request(
            "call_assist_timeline_stats",
            {"limit": 1000},
        )
        self.assertTrue(timeline_stats["ok"])
        self.assertEqual(timeline_stats["result"]["stats"]["count"], 4)

        timeline_summary = self.request(
            "call_assist_timeline_summary",
            {"limit": 500, "max_tasks": 5},
        )
        self.assertTrue(timeline_summary["ok"])
        self.assertIn("summary", timeline_summary["result"])
        self.assertGreaterEqual(len(timeline_summary["result"].get("tasks", [])), 1)

        timeline_clear = self.request(
            "call_assist_timeline_clear",
            {"keep_last": 1},
        )
        self.assertTrue(timeline_clear["ok"])
        self.assertEqual(timeline_clear["result"]["keep_last"], 1)

        timeline_to_history = self.request(
            "call_assist_timeline_to_history",
            {"format": "md", "limit": 120},
        )
        self.assertTrue(timeline_to_history["ok"])
        self.assertEqual(timeline_to_history["result"]["format"], "md")
        self.assertTrue(timeline_to_history["result"]["history_id"])
        self.assertTrue(timeline_to_history["result"]["summary_included"])
        self.assertTrue(timeline_to_history["result"]["stats_included"])

        self.assertIn("/v1/sessions/gw-session-77/summary", post_paths)
        self.assertIn("/v1/sessions/gw-session-77/quick-phrase", post_paths)
        self.assertTrue(
            any(path.startswith("/v1/sessions/gw-session-77/timeline?limit=25") for path in get_paths)
        )
        self.assertIn("/v1/sessions/gw-session-77/timeline/stats?limit=1000", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/summary?limit=500&max_tasks=5", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/summary?limit=120&max_tasks=8", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/stats?limit=120", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/export?format=md&limit=100", get_paths)
        self.assertIn("/v1/sessions/gw-session-77/timeline/export?format=md&limit=120", get_paths)
        self.assertTrue(any(path.startswith("/v1/telephony/cost/estimate?country=ES") for path in get_paths))
        self.assertIn("/v1/sessions/gw-session-77/timeline?keep_last=1", delete_paths)

    def test_preview_updates_even_when_snapshot_size_constant(self) -> None:
        self.assertTrue(self.request("start_recording")["ok"])
        # Ждём, пока preview loop выполнит хотя бы одну итерацию с новым текстом
        self.assertTrue(self._wait_for_preview_update(timeout=5.0), "preview не обновился за 5s")
        state = self.request("get_recording_state")
        self.assertTrue(state["ok"])
        preview_text = state["result"]["preview_text"]
        self.assertIn("preview#", preview_text)
        self.assertRegex(preview_text, r"preview#\d+")
        stop = self.request("stop_recording", {"quality_profile": "balanced"})
        self.assertTrue(stop["ok"])

    def test_history_methods(self) -> None:
        added = self.request("add_history_item", {"text": "alpha", "paste_status": "failed"})
        self.assertTrue(added["ok"])
        item_id = added["result"]["id"]

        status = self.request("set_paste_status", {"id": item_id, "paste_status": "ok"})
        self.assertTrue(status["ok"])

        page = self.request("get_history_page", {"cursor": None, "limit": 50})
        self.assertTrue(page["ok"])
        self.assertEqual(page["result"]["items"][0]["paste_status"], "ok")

        search = self.request("search_history", {"query": "alpha", "cursor": None, "limit": 50})
        self.assertTrue(search["ok"])
        self.assertEqual(len(search["result"]["items"]), 1)

        deleted = self.request("delete_history_item", {"id": item_id})
        self.assertTrue(deleted["ok"])

        page_after = self.request("get_history_page", {"cursor": None, "limit": 50})
        self.assertEqual(page_after["result"]["items"], [])

    def test_history_filters_via_service(self) -> None:
        add_ok = self.request(
            "add_history_item",
            {
                "text": "uno",
                "paste_status": "ok",
                "translation_mode": "ru_to_es",
                "translation_status": "ok",
            },
        )
        self.assertTrue(add_ok["ok"])
        add_failed = self.request(
            "add_history_item",
            {
                "text": "dos",
                "paste_status": "failed",
                "translation_mode": "off",
            },
        )
        self.assertTrue(add_failed["ok"])

        page = self.request(
            "get_history_page",
            {
                "cursor": None,
                "limit": 50,
                "paste_status": "ok",
            },
        )
        self.assertTrue(page["ok"])
        self.assertEqual(len(page["result"]["items"]), 1)
        self.assertEqual(page["result"]["items"][0]["paste_status"], "ok")

        search = self.request(
            "search_history",
            {
                "query": "",
                "cursor": None,
                "limit": 50,
                "translation_mode": "ru_to_es",
            },
        )
        self.assertTrue(search["ok"])
        self.assertEqual(len(search["result"]["items"]), 1)
        self.assertEqual(search["result"]["items"][0]["translation_mode"], "ru_to_es")

        search_status = self.request(
            "search_history",
            {
                "query": "",
                "cursor": None,
                "limit": 50,
                "translation_status": "ok",
            },
        )
        self.assertTrue(search_status["ok"])
        self.assertEqual(len(search_status["result"]["items"]), 1)

        today = search_status["result"]["items"][0]["ts"][:10]
        page_today = self.request(
            "get_history_page",
            {
                "cursor": None,
                "limit": 50,
                "from_ts": today,
                "to_ts": today,
            },
        )
        self.assertTrue(page_today["ok"])
        self.assertGreaterEqual(len(page_today["result"]["items"]), 1)

    def test_import_history_ndjson_method(self) -> None:
        self.request(
            "add_history_item",
            {"text": "alpha", "paste_status": "failed"},
        )
        page = self.request("get_history_page", {"cursor": None, "limit": 10})
        existing_id = page["result"]["items"][0]["id"]

        ndjson_path = Path(self.tmp.name) / "external_history.ndjson"
        payloads = [
            {
                "id": existing_id,
                "ts": "2026-02-11T10:00:00",
                "text": "duplicate",
                "paste_status": "failed",
            },
            {
                "id": "ext-imported-1",
                "ts": "2026-02-11T10:00:01",
                "text": "external text",
                "paste_status": "ok",
                "translation_mode": "off",
            },
        ]
        ndjson_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in payloads) + "\n", encoding="utf-8")

        imported = self.request(
            "import_history_ndjson",
            {"path": str(ndjson_path)},
        )
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["result"]["imported"], 1)
        self.assertEqual(imported["result"]["skipped"], 1)

    @pytest.mark.slow
    def test_integration_1000_cycles(self) -> None:
        # W1748: use 50 cycles under xdist (-n 2) to stay within worker heartbeat
        # window and avoid OOM on CI runners.  The original 1000-cycle loop takes
        # ~18 s locally and caused worker crashes on the Ubuntu CI runner when
        # 2+ workers accumulated MLX / LM-Studio background threads simultaneously.
        # 50 cycles still exercises the full start/stop/history/compact pipeline.
        # In solo runs (no xdist) the full 1000 cycles run as before.
        import os
        n_cycles = 50 if os.environ.get("PYTEST_XDIST_WORKER") else 1000
        for idx in range(n_cycles):
            start = self.request("start_recording", request_id=f"s{idx}")
            self.assertTrue(start["ok"])
            stop = self.request("stop_recording", {"quality_profile": "balanced"}, request_id=f"e{idx}")
            self.assertTrue(stop["ok"])
            self.assertTrue(stop["result"]["history_id"])

        # Always use limit=50 (first page); with 1000 cycles this returns 50 items,
        # with 50 cycles it also returns 50 items (all items = one full page).
        page = self.request("get_history_page", {"cursor": None, "limit": 50})
        self.assertEqual(len(page["result"]["items"]), 50)

        compacted = self.request("compact_history")
        self.assertTrue(compacted["ok"])

    def test_transcribe_paths(self) -> None:
        src_dir = Path(self.tmp.name) / "audio_src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "a.wav").write_bytes(b"stub")
        (src_dir / "b.mp3").write_bytes(b"stub")
        (src_dir / "ignore.txt").write_text("x", encoding="utf-8")

        response = self.request(
            "transcribe_paths",
            {"paths": [str(src_dir)], "quality_profile": "balanced"},
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["processed"], 2)
        self.assertEqual(len(response["result"]["items"]), 2)

    def test_preview_transcribe_paths(self) -> None:
        src_dir = Path(self.tmp.name) / "audio_preview"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "a.wav").write_bytes(b"stub")
        (src_dir / "b.mp3").write_bytes(b"stub")
        (src_dir / "ignore.txt").write_text("x", encoding="utf-8")

        response = self.request(
            "preview_transcribe_paths",
            {"paths": [str(src_dir)], "sample_limit": 2},
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["audio_count"], 2)
        self.assertEqual(len(response["result"]["sample"]), 2)
        self.assertEqual(response["result"]["by_ext"].get(".wav"), 1)
        self.assertEqual(response["result"]["by_ext"].get(".mp3"), 1)
        self.assertGreater(response["result"]["total_bytes"], 0)

    def test_preview_transcribe_paths_empty(self) -> None:
        response = self.request(
            "preview_transcribe_paths",
            {"paths": [str(Path(self.tmp.name) / "missing_dir")]},
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["audio_count"], 0)

    def test_transcribe_paths_with_translation(self) -> None:
        src_dir = Path(self.tmp.name) / "audio_src_translate"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "clip.wav").write_bytes(b"stub")

        self.assertTrue(
            self.request(
                "set_settings",
                {
                    "translation_mode": "ru_to_es",
                    "translate_and_paste": True,
                    "network_mode": "offline_default",
                },
            )["ok"]
        )

        response = self.request(
            "transcribe_paths",
            {
                "paths": [str(src_dir)],
                "quality_profile": "balanced",
                "translation_mode": "ru_to_es",
                "translate_and_paste": True,
            },
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["processed"], 1)
        item = response["result"]["items"][0]
        self.assertEqual(item["translation_mode"], "ru_to_es")
        self.assertEqual(item["translation_status"], "ok")
        self.assertTrue(item["translated_text"].startswith("ES:"))
        self.assertEqual(item["text"], item["translated_text"])

    def test_compact_and_history_stats(self) -> None:
        item = self.request("add_history_item", {"text": "one", "paste_status": "failed"})
        self.assertTrue(item["ok"])
        item_id = item["result"]["id"]
        self.assertTrue(self.request("set_paste_status", {"id": item_id, "paste_status": "ok"})["ok"])
        self.assertTrue(self.request("delete_history_item", {"id": item_id})["ok"])

        compacted = self.request("compact_history")
        self.assertTrue(compacted["ok"])
        self.assertIn("before_total_bytes", compacted["result"])
        self.assertIn("after_total_bytes", compacted["result"])
        self.assertIn("reclaimed_bytes", compacted["result"])

        stats = self.request("get_history_stats")
        self.assertTrue(stats["ok"])
        self.assertIn("active_count", stats["result"])
        self.assertIn("total_bytes", stats["result"])

    def test_diagnostics_includes_stt_last_engine_field(self) -> None:
        """get_diagnostics stt section должна содержать last_engine (None до первой транскрибации)."""
        diag = self.request("get_diagnostics")
        self.assertTrue(diag["ok"])
        stt = diag["result"]["stt"]
        self.assertIn("last_engine", stt, "stt section должна иметь last_engine поле")
        # До первой записи — None (не было транскрибаций).
        self.assertIsNone(stt["last_engine"])

    def test_diagnostics_stt_last_engine_updates_after_stop_recording(self) -> None:
        """После stop_recording last_engine должен обновиться если transcriber вернул engine поле."""

        class EngineCapturingTranscriber(FakeTranscriber):
            """FakeTranscriber, возвращающий словарь с engine полем."""
            def transcribe(self, audio_data, quality_profile="balanced", cleanup_profile="soft",
                           domain="casual", extra_vocabulary=None, lang_hint=None,
                           history_context=None, stt_hotwords=None, settings=None,
                           diarize=None, skip_vad_prefilter=False, silence_ranges=None):
                self.counter += 1
                return {
                    "text": f"тестовая строка #{self.counter}",
                    "engine": "gigaam-rnnt",
                    "confidence": 0.9,
                }
            # engine attribute требуется _handle_get_diagnostics

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            from backend.state_store import StateStore
            store = StateStore(Path(tmp) / "data")
            svc = BackendService(
                store=store,
                recorder=FakeRecorder(),
                transcriber=EngineCapturingTranscriber(),
                translator=FakeTranslator(),
            )
            try:
                def req(method, params=None):
                    return svc.handle_request({"id": "t1", "method": method, "params": params or {}})

                # До записи — None.
                pre = req("get_diagnostics")
                self.assertIsNone(pre["result"]["stt"]["last_engine"])

                # Одна запись.
                req("start_recording")
                req("stop_recording", {"quality_profile": "balanced"})

                # После — должен быть заполнен.
                post = req("get_diagnostics")
                self.assertEqual(post["result"]["stt"]["last_engine"], "gigaam-rnnt")
            finally:
                svc.close()

    def test_diagnostics_stt_last_engine_stays_none_for_string_transcriber(self) -> None:
        """Если transcriber вернул строку (без engine), last_engine остаётся None."""
        # FakeTranscriber по умолчанию возвращает строку — engine не кэшируется.
        self.request("start_recording")
        self.request("stop_recording", {"quality_profile": "balanced"})
        diag = self.request("get_diagnostics")
        self.assertIsNone(diag["result"]["stt"]["last_engine"])


class BackendServiceLLMInitializationTestCase(unittest.TestCase):
    """Тесты что BackendService правильно инициализирует LLMRewriter когда LLM_ENABLED."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from backend.state_store import StateStore
        self.tmpdir = tempfile.mkdtemp()
        self.store = StateStore(data_dir=Path(self.tmpdir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_llm_rewriter_none_when_admin_disabled(self):
        """settings.LLM_ENABLED=False → _llm_rewriter is None.

        transcriber=FakeTranscriber(): this test only exercises _llm_rewriter
        init, which is constructed BEFORE the transcriber branch in
        BackendService.__init__ and is unaffected by it. Omitting the fake
        used to build a REAL Transcriber/AudioEngine, which — when this dev
        machine's real settings.json has stt_gigaam_enabled=true (see
        test_construction_does_not_leak_gigaam_subprocess below) — leaked an
        orphaned gigaam_worker.py subprocess.
        """
        from unittest.mock import patch
        import core.config as _cfg
        with patch.object(_cfg.settings, "LLM_ENABLED", False):
            from backend.service import BackendService
            service = BackendService(store=self.store, transcriber=FakeTranscriber())
            try:
                self.assertIsNone(service._llm_rewriter)
            finally:
                service.close()

    @pytest.mark.llm_network_live
    def test_llm_rewriter_created_when_admin_enabled(self):
        """settings.LLM_ENABLED=True → _llm_rewriter is LLMRewriter instance.

        2026-08-04: раньше патчился `backend.llm_rewriter.requests.get` — модульный
        уровень, никак не связанный с реальным вызовом. `LLMRewriter.ping()` ходит
        через `self._session.get(...)` (инстанс `requests.Session()`, созданный в
        `__init__`), а не через модульную функцию `requests.get`. Мок никогда не
        перехватывал вызов (0 обращений при `assert_called_once()`), и тест уходил
        в РЕАЛЬНЫЙ `GET http://localhost:1234/api/v1/models` — на CI просто быстро
        падал по connection-refused (тест зелёный не благодаря моку, а благодаря
        тому, что `ping()` глотает любое исключение и `_llm_rewriter` возвращается
        независимо от результата ping), а на машине владельца с реально запущенным
        LM Studio — бил в него по-настоящему при каждом прогоне юнит-тестов.
        Патчим правильный МЕТОД КЛАССА `requests.Session.get` — тот, что реально
        резолвится для `self._session.get(...)` (тот же класс TDD-урока, что и
        сиблинг-баги MRO в CLAUDE.md: патчить нужно то, откуда объект РЕАЛЬНО
        берёт метод).
        """
        from unittest.mock import patch
        import core.config as _cfg
        with patch.object(_cfg.settings, "LLM_ENABLED", True), \
                patch("requests.Session.get") as mock_get:
            mock_get.return_value.status_code = 200
            from backend.service import BackendService
            from backend.llm_rewriter import LLMRewriter
            # transcriber=FakeTranscriber(): see test_llm_rewriter_none_when_admin_disabled
            # above — avoids constructing a real Transcriber/AudioEngine (and thus a real
            # GigaAM subprocess attempt) which this test has no need for.
            service = BackendService(store=self.store, transcriber=FakeTranscriber())
            try:
                self.assertIsInstance(service._llm_rewriter, LLMRewriter)
                # Доказываем, что мок РЕАЛЬНО перехватил вызов — не декоративен.
                mock_get.assert_called_once()
                called_url = mock_get.call_args.args[0]
                self.assertIn("/api/v1/models", called_url)
            finally:
                service.close()

    def test_construction_does_not_leak_gigaam_subprocess(self):
        """Regression: this class only exercises _llm_rewriter init and has no
        need for a real STT engine, but omitting ``transcriber=`` used to build
        a REAL Transcriber/AudioEngine. On a dev machine whose real
        ``~/Library/Application Support/KrabEar/settings.json`` has
        ``stt_gigaam_enabled: true`` (the actual production default — read by
        ``core.config`` at import time regardless of test isolation), that
        real AudioEngine spawns a background 'GigaAM-warmup' thread that
        Popen()s ``core/workers/gigaam_worker.py``. Under this harness venv
        the load handshake always fails, and the spawned subprocess leaked as
        an orphan (PPID=1) — caught by ``scripts/pre_merge_py312_check.sh``.

        Injecting FakeTranscriber (like every other test class in this file)
        keeps this test hermetic and asserts no gigaam subprocess is ever
        attempted, regardless of the runtime settings.json on whatever
        machine the suite runs on.
        """
        from unittest.mock import patch
        import core.config as _cfg
        with patch.object(_cfg.settings, "LLM_ENABLED", False), \
                patch("subprocess.Popen") as mock_popen:
            from backend.service import BackendService
            service = BackendService(store=self.store, transcriber=FakeTranscriber())
            try:
                pass
            finally:
                service.close()
        for call in mock_popen.call_args_list:
            args = call.args[0] if call.args else call.kwargs.get("args")
            joined = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
            self.assertNotIn(
                "gigaam_worker", joined,
                "BackendService(store=...) must not spawn a real GigaAM subprocess "
                "when a FakeTranscriber is injected",
            )


class VocabularyCapturingTranscriber(FakeTranscriber):
    """FakeTranscriber, который запоминает переданный extra_vocabulary."""

    def __init__(self) -> None:
        super().__init__()
        self.last_extra_vocabulary: list[str] | None = None

    def transcribe(self, audio_data, quality_profile: str = "balanced", cleanup_profile: str = "soft",
                   domain: str = "casual", extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None, settings=None,
                   diarize=None, skip_vad_prefilter=False, silence_ranges=None) -> str:
        self.last_extra_vocabulary = extra_vocabulary
        return super().transcribe(audio_data, quality_profile, cleanup_profile, domain, extra_vocabulary, lang_hint,
                                  history_context=history_context, stt_hotwords=stt_hotwords,
                                  silence_ranges=silence_ranges)


class VocabularySuggestionsTestCase(unittest.TestCase):
    """Тесты IPC-метода get_vocabulary_suggestions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=self.store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def request(self, method: str, params=None, request_id="t1"):
        return self.service.handle_request(
            {"id": request_id, "method": method, "params": params or {}}
        )

    def test_empty_history_returns_empty_suggestions(self) -> None:
        """Без истории — пустые suggestions."""
        resp = self.request("get_vocabulary_suggestions")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["suggestions"], [])
        self.assertEqual(resp["result"]["scanned_items"], 0)

    def test_suggestions_from_history(self) -> None:
        """Слова с частотой >= 3 попадают в suggestions."""
        # Добавляем 5 записей, где "Telegram" и "Python" встречаются 4 раза
        for i in range(4):
            self.store.add_history_item(
                text=f"Запустил Telegram и Python на сервере #{i}",
                paste_status="ok",
            )
        # Добавляем запись без этих слов
        self.store.add_history_item(text="Просто тест", paste_status="ok")

        resp = self.request("get_vocabulary_suggestions", {"min_count": 3})
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertEqual(result["scanned_items"], 5)
        words = [s["word"] for s in result["suggestions"]]
        self.assertIn("Telegram", words)
        self.assertIn("Python", words)

    def test_suggestions_filter_short_words(self) -> None:
        """Слова короче min_word_len не попадают в suggestions."""
        for _ in range(5):
            self.store.add_history_item(text="да нет тут вот ок", paste_status="ok")

        resp = self.request("get_vocabulary_suggestions", {"min_count": 3, "min_word_len": 4})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["suggestions"], [])

    def test_suggestions_filter_stop_words(self) -> None:
        """Стоп-слова не попадают в suggestions."""
        for _ in range(5):
            self.store.add_history_item(
                text="может быть просто нужно тогда потом",
                paste_status="ok",
            )

        resp = self.request("get_vocabulary_suggestions", {"min_count": 3, "min_word_len": 4})
        self.assertTrue(resp["ok"])
        words = [s["word"] for s in resp["result"]["suggestions"]]
        self.assertNotIn("может", words)
        self.assertNotIn("просто", words)
        self.assertNotIn("нужно", words)
        self.assertNotIn("потом", words)

    def test_suggestions_exclude_existing_vocabulary(self) -> None:
        """Слова уже в vocabulary.json не попадают в suggestions."""
        self.service.vocabulary.save(["Telegram"])
        for _ in range(5):
            self.store.add_history_item(
                text="Telegram Python Claude",
                paste_status="ok",
            )

        resp = self.request("get_vocabulary_suggestions", {"min_count": 3})
        self.assertTrue(resp["ok"])
        words = [s["word"] for s in resp["result"]["suggestions"]]
        self.assertNotIn("Telegram", words)
        self.assertIn("Python", words)
        self.assertIn("Claude", words)
        self.assertEqual(resp["result"]["current_vocabulary_size"], 1)

    def test_suggestions_top_k_limit(self) -> None:
        """top_k ограничивает количество результатов."""
        for i in range(10):
            self.store.add_history_item(
                text=f"Word{i:02d}xx Alpha Beta Gamma Delta Epsilon Zeta",
                paste_status="ok",
            )

        resp = self.request("get_vocabulary_suggestions", {"min_count": 3, "top_k": 5})
        self.assertTrue(resp["ok"])
        self.assertLessEqual(len(resp["result"]["suggestions"]), 5)

    def test_suggestions_uses_source_text_when_available(self) -> None:
        """Если source_text заполнен, анализируется он, а не text (до перевода)."""
        for _ in range(4):
            self.store.add_history_item(
                text="ES:Telegram",
                paste_status="ok",
                source_text="Telegram работает хорошо",
            )

        resp = self.request("get_vocabulary_suggestions", {"min_count": 3})
        self.assertTrue(resp["ok"])
        words = [s["word"] for s in resp["result"]["suggestions"]]
        self.assertIn("Telegram", words)
        self.assertIn("работает", words)


class VocabularyPassthroughTestCase(unittest.TestCase):
    """Проверяет, что user vocabulary передаётся в Whisper prompt."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.transcriber = VocabularyCapturingTranscriber()
        self.service = BackendService(
            store=self.store,
            recorder=FakeRecorder(),
            transcriber=self.transcriber,
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def request(self, method: str, params=None, request_id="t1"):
        return self.service.handle_request(
            {"id": request_id, "method": method, "params": params or {}}
        )

    def test_stop_recording_passes_vocabulary(self) -> None:
        """stop_recording передаёт vocabulary из VocabularyStore в transcriber."""
        self.service.vocabulary.save(["Telegram", "Claude", "Hammerspoon"])

        self.request("start_recording")
        resp = self.request("stop_recording")
        self.assertTrue(resp["ok"])
        self.assertIsNotNone(self.transcriber.last_extra_vocabulary)
        self.assertEqual(
            sorted(self.transcriber.last_extra_vocabulary),
            ["Claude", "Hammerspoon", "Telegram"],
        )

    def test_stop_recording_no_vocabulary_passes_none(self) -> None:
        """Если vocabulary пуст, передаётся None (без лишнего prompt-раздувания)."""
        # Vocabulary файла нет — load_vocabulary вернёт []
        self.request("start_recording")
        resp = self.request("stop_recording")
        self.assertTrue(resp["ok"])
        self.assertIsNone(self.transcriber.last_extra_vocabulary)


class GlossarySuggestionsTestCase(unittest.TestCase):
    """Тесты IPC-метода get_glossary_suggestions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=self.store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def request(self, method: str, params=None, request_id="t1"):
        return self.service.handle_request(
            {"id": request_id, "method": method, "params": params or {}}
        )

    def _add_item(self, source_text: str, translated_text: str) -> None:
        self.store.add_history_item(
            text=translated_text,
            paste_status="ok",
            source_text=source_text,
            translated_text=translated_text,
        )

    def test_empty_history_returns_brand_suggestions(self) -> None:
        """Без истории переводов возвращаются только бренды из BRAND_REPLACEMENTS."""
        resp = self.request("get_glossary_suggestions")
        self.assertTrue(resp["ok"])
        result = resp["result"]
        origins = {s["origin"] for s in result["suggestions"]}
        # Должны присутствовать brand_replacement кандидаты
        self.assertIn("brand_replacement", origins)
        self.assertEqual(result["scanned_items"], 0)

    def test_history_pair_detected(self) -> None:
        """Заглавное слово из истории появляется среди кандидатов."""
        # Используем слово не из BRAND_REPLACEMENTS, чтобы оно шло через capitalized_term
        for _ in range(3):
            self._add_item(
                source_text="Открой Zabbix и проверь мониторинг",
                translated_text="Open Zabbix and check monitoring",
            )
        resp = self.request("get_glossary_suggestions", {"min_count": 2, "top_k": 50})
        self.assertTrue(resp["ok"])
        suggestions = resp["result"]["suggestions"]
        sources = [s["source"] for s in suggestions]
        self.assertIn("Zabbix", sources)

    def test_existing_glossary_filtered(self) -> None:
        """Слова уже в глоссарии не возвращаются."""
        # Записываем Zabbix в глоссарий напрямую, избегая мутации DEFAULT_SETTINGS
        existing = self.store.load_settings()
        existing["translation_glossary"] = dict(existing.get("translation_glossary") or {})
        existing["translation_glossary"]["Zabbix"] = "Zabbix"
        self.store.save_settings(existing)
        self.service._invalidate_settings_cache()

        for _ in range(3):
            self._add_item(
                source_text="Открой Zabbix срочно",
                translated_text="Open Zabbix urgently",
            )
        resp = self.request("get_glossary_suggestions", {"min_count": 2, "top_k": 50})
        self.assertTrue(resp["ok"])
        sources = [s["source"] for s in resp["result"]["suggestions"]]
        self.assertNotIn("Zabbix", sources)

    def test_low_frequency_terms_excluded(self) -> None:
        """Слова с частотой ниже min_count не попадают в history_pair/capitalized_term."""
        # Добавляем только 1 запись — частота = 1 (Zabbix не в BRAND_REPLACEMENTS)
        self._add_item(
            source_text="Открой Zabbix один раз",
            translated_text="Open Zabbix once",
        )
        resp = self.request("get_glossary_suggestions", {"min_count": 3, "top_k": 50})
        self.assertTrue(resp["ok"])
        # Zabbix с count=1 не должен быть среди history_pair/capitalized_term
        non_brand = [
            s for s in resp["result"]["suggestions"]
            if s["origin"] != "brand_replacement"
        ]
        sources = [s["source"] for s in non_brand]
        self.assertNotIn("Zabbix", sources)

    def test_top_k_limits_results(self) -> None:
        """top_k ограничивает количество результатов."""
        resp = self.request("get_glossary_suggestions", {"top_k": 5})
        self.assertTrue(resp["ok"])
        self.assertLessEqual(len(resp["result"]["suggestions"]), 5)

    def test_result_schema(self) -> None:
        """Ответ содержит все обязательные поля."""
        resp = self.request("get_glossary_suggestions")
        self.assertTrue(resp["ok"])
        result = resp["result"]
        for key in ("suggestions", "total_candidates", "scanned_items", "current_glossary_size"):
            self.assertIn(key, result)
        for s in result["suggestions"]:
            for field in ("source", "target", "count", "origin"):
                self.assertIn(field, s)


class BackendServiceInitTestCase(unittest.TestCase):
    """Проверяет корректную инициализацию BackendService и таблицу диспетчеризации."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService starts background threads that
        # may write to data dir after the test ends → OSError on cleanup in CI.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        # W1749: patch sounddevice.rec/wait so test_microphone does not block
        # on real hardware or hang when two xdist workers call sd.rec()
        # simultaneously (no microphone available in CI).
        # Using patch() on the sounddevice module is safe even if the module
        # is a real install — rec/wait are module-level functions that can
        # always be replaced for the duration of the test class.
        # If sounddevice itself is absent (ImportError inside handler), the
        # handler already catches it gracefully, so the patch is a no-op
        # in that scenario.
        import types as _types
        import unittest.mock as _mock
        _sd = sys.modules.get("sounddevice")
        if isinstance(_sd, _types.ModuleType) and hasattr(_sd, "rec"):
            # Real sounddevice available — patch out ALL blocking / hardware
            # calls reachable from the IPC methods this test class exercises.
            # W1751: test_dispatch_table_has_all_methods probes "test_microphone"
            # (sd.rec/sd.wait) AND "get_audio_devices"/"list_audio_inputs"
            # (sd.query_devices).  Real PortAudio device enumeration under
            # concurrent pytest-xdist workers on a headless macOS runner can
            # crash the worker ("node down: Not properly terminated").  Patching
            # query_devices/InputStream too keeps the worker stable.
            _np_zeros = np.zeros((32000, 1), dtype=np.float32)
            for _attr, _kw in (
                ("rec", {"return_value": _np_zeros}),
                ("wait", {"return_value": None}),
                ("query_devices", {"return_value": []}),
                ("InputStream", {"return_value": _mock.MagicMock()}),
            ):
                if hasattr(_sd, _attr):
                    _p = _mock.patch.object(_sd, _attr, **_kw)
                    _p.start()
                    self.addCleanup(_p.stop)
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def request(self, method: str, params=None, request_id="t1"):
        return self.service.handle_request(
            {"id": request_id, "method": method, "params": params or {}}
        )

    def test_check_mic_noise_returns_profile(self):
        """check_mic_noise: RMS/peak + вложенный профиль шума под ключом noise.

        sd.rec замокан на np.zeros (тишина) → NoiseProfiler отрабатывает по
        in-memory массиву без временного файла и без реального микрофона.
        """
        resp = self.request("check_mic_noise", {"duration_sec": 1})
        self.assertTrue(resp.get("ok"), msg=f"ожидали ok=True, получили {resp}")
        result = resp.get("result", {})
        self.assertTrue(result.get("ok"))
        self.assertIn("rms", result)
        self.assertIn("peak", result)
        # Профиль шума вложен под "noise" со всеми полями NoiseProfile.to_dict().
        noise = result.get("noise")
        self.assertIsInstance(noise, dict)
        for key in ("noise_type", "noise_level_db", "snr_db",
                    "frequency_profile", "recommendations", "suitable_for_stt"):
            self.assertIn(key, noise, msg=f"поле {key} отсутствует в noise")
        self.assertIsInstance(noise["suitable_for_stt"], bool)
        self.assertIsInstance(noise["recommendations"], list)

    def test_dispatch_table_has_all_methods(self):
        """Все IPC-методы присутствуют в таблице диспетчеризации."""
        # Собираем список доступных методов через ping + unknown probe
        expected_methods = [
            "ping",
            "start_recording",
            "stop_recording",
            "get_recording_state",
            "start_call_assist",
            "stop_call_assist",
            "get_call_assist_state",
            "get_history_page",
            "search_history",
            "delete_history_item",
            "get_settings",
            "set_settings",
            "translate_text",
            "get_diagnostics",
            "summarize_text",
            "get_clipboard_history",
            "get_audio_devices",
            "test_microphone",
            "list_profile_presets",
            "apply_profile_preset",
            "get_storage_info",
            "cleanup_old_history",
        ]
        for method in expected_methods:
            resp = self.request(method)
            self.assertNotEqual(
                resp.get("result", {}).get("error") if not resp.get("ok") else None,
                "unknown_method",
                msg=f"Метод '{method}' отсутствует в таблице диспетчеризации",
            )
            # unknown_method error means method not in dispatch table
            if not resp.get("ok"):
                error_code = resp.get("error", {}).get("code", "")
                self.assertNotEqual(
                    error_code,
                    "unknown_method",
                    msg=f"Метод '{method}' отсутствует в таблице диспетчеризации",
                )

    def test_call_assist_service_wired(self):
        """CallAssistService инициализирован и доступен через _call_assist."""
        self.assertTrue(
            hasattr(self.service, "_call_assist"),
            "_call_assist должен быть атрибутом BackendService",
        )
        call_assist = self.service._call_assist
        self.assertTrue(
            hasattr(call_assist, "handle_start"),
            "CallAssistService должен иметь метод handle_start",
        )

    def test_start_time_set(self):
        """_start_time устанавливается при инициализации для расчёта uptime."""
        self.assertTrue(
            hasattr(self.service, "_start_time"),
            "_start_time должен быть атрибутом BackendService",
        )
        self.assertIsInstance(self.service._start_time, float)
        self.assertGreater(self.service._start_time, 0.0)
        # uptime должен быть неотрицательным
        resp = self.request("ping")
        self.assertTrue(resp["ok"])
        self.assertGreaterEqual(resp["result"]["uptime_sec"], 0.0)


class BackendServiceErrorHandlingTestCase(unittest.TestCase):
    """Test graceful error handling for edge cases."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService starts background threads that
        # may write to data dir after the test ends → OSError on cleanup in CI.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def _is_valid_response(self, resp) -> bool:
        """Проверяет, что ответ является валидным словарём с полем ok."""
        self.assertIsInstance(resp, dict, "Ответ должен быть словарём")
        self.assertIn("ok", resp, "Ответ должен содержать поле ok")
        # Должна быть возможность сериализовать ответ в JSON без исключений
        json.dumps(resp)
        return True

    def test_handle_request_missing_method(self) -> None:
        """Запрос без ключа method должен вернуть валидный ответ (unknown_method), а не упасть."""
        resp = self.service.handle_request({"id": "e1", "params": {}})
        self._is_valid_response(resp)
        # method="" приведёт к unknown_method
        self.assertFalse(resp["ok"])
        self.assertIn("error", resp)

    def test_handle_request_null_params(self) -> None:
        """Метод с params=None должен вернуть ошибку invalid_params, не крашиться."""
        resp = self.service.handle_request({"id": "e2", "method": "ping", "params": None})
        self._is_valid_response(resp)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_params")

    def test_handle_request_empty_params(self) -> None:
        """Метод ping с params={} должен работать нормально."""
        resp = self.service.handle_request({"id": "e3", "method": "ping", "params": {}})
        self._is_valid_response(resp)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["status"], "ok")

    def test_handle_request_invalid_json_type(self) -> None:
        """params в виде строки вместо dict должен вернуть ошибку invalid_params."""
        resp = self.service.handle_request({"id": "e4", "method": "ping", "params": "not_a_dict"})
        self._is_valid_response(resp)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_params")

    def test_handle_request_very_long_method(self) -> None:
        """Метод с именем >1000 символов должен вернуть unknown_method, а не упасть."""
        long_method = "x" * 1001
        resp = self.service.handle_request({"id": "e5", "method": long_method, "params": {}})
        self._is_valid_response(resp)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "unknown_method")

    def test_concurrent_requests(self) -> None:
        """10 параллельных запросов ping не должны вызывать краш или data race."""
        import threading

        results: list[dict] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def do_request(i: int) -> None:
            try:
                resp = self.service.handle_request(
                    {"id": f"c{i}", "method": "ping", "params": {}}
                )
                with lock:
                    results.append(resp)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=do_request, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Исключения в потоках: {errors}")
        self.assertEqual(len(results), 10, "Должно быть ровно 10 ответов")
        for resp in results:
            self._is_valid_response(resp)
            self.assertTrue(resp["ok"])

    def test_ipc_resilience_unknown_method(self) -> None:
        """Неизвестный method возвращает error response с кодом unknown_method, не крашится."""
        resp = self.service.handle_request({"id": "r1", "method": "nonexistent_method", "params": {}})
        self._is_valid_response(resp)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "unknown_method")
        self.assertIn("nonexistent_method", resp["error"]["message"])

    def test_ipc_resilience_params_list_instead_of_dict(self) -> None:
        """params в виде списка вместо dict возвращает error response, не крашится."""
        resp = self.service.handle_request({"id": "r2", "method": "ping", "params": [1, 2, 3]})
        self._is_valid_response(resp)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_params")
        self.assertIn("params", resp["error"]["message"].lower())

    def test_ipc_resilience_params_number_instead_of_dict(self) -> None:
        """params в виде числа вместо dict возвращает error response, не крашится."""
        resp = self.service.handle_request({"id": "r3", "method": "ping", "params": 42})
        self._is_valid_response(resp)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "invalid_params")


class SynthesizeSpeechIPCTestCase(unittest.TestCase):
    """Тесты IPC-метода synthesize_speech через BackendService.handle_request."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService starts background threads that
        # may write to data dir after the test ends → OSError on cleanup in CI.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def _req(self, params: dict) -> dict:
        return self.service.handle_request(
            {"id": "tts1", "method": "synthesize_speech", "params": params}
        )

    def test_synthesize_speech_empty_text_returns_error(self) -> None:
        """synthesize_speech с пустым text -> result содержит ok=False или error."""
        from unittest.mock import patch
        with patch("backend.tts_service.settings") as mock_s:
            mock_s.TTS_ENABLED = False
            mock_s.TTS_FALLBACK_SAY = False
            mock_s.TTS_SILERO_MODEL = "v4_ru"
            mock_s.TTS_SILERO_VOICE = "baya"
            mock_s.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
            mock_s.SAY_VOICE = ""
            resp = self._req({"text": "", "language": "en"})
        # IPC layer wraps handler result in {"ok": True, "result": <handler_result>}
        # The handler itself returns {"ok": False, "error": "..."} for empty text
        result = resp.get("result", {})
        self.assertFalse(result.get("ok", True))

    def test_synthesize_speech_method_registered(self) -> None:
        """Метод synthesize_speech должен быть зарегистрирован в handlers."""
        from unittest.mock import patch
        with patch("backend.tts_service.settings") as mock_s:
            mock_s.TTS_ENABLED = False
            mock_s.TTS_FALLBACK_SAY = False
            mock_s.TTS_SILERO_MODEL = "v4_ru"
            mock_s.TTS_SILERO_VOICE = "baya"
            mock_s.TTS_KOKORO_MODEL = "hexgrad/Kokoro-82M"
            mock_s.SAY_VOICE = ""
            resp = self.service.handle_request(
                {"id": "tts2", "method": "synthesize_speech", "params": {"text": "hi"}}
            )
        # Не должен вернуть unknown_method
        if not resp.get("ok", True):
            err = resp.get("error", {})
            self.assertNotEqual(err.get("code"), "unknown_method")


class MetricsDashboardPreviewLoopTestCase(unittest.TestCase):
    """Тесты поля preview_loop в get_metrics_dashboard (C2)."""

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService starts background threads that
        # may write to data dir after the test ends → OSError on cleanup in CI.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def _dashboard(self) -> dict:
        resp = self.service.handle_request(
            {"id": "d1", "method": "get_metrics_dashboard", "params": {}}
        )
        self.assertTrue(resp.get("ok"), f"dashboard failed: {resp}")
        return resp["result"]

    def test_dashboard_has_preview_loop_field(self) -> None:
        """get_metrics_dashboard должен содержать ключ preview_loop."""
        result = self._dashboard()
        self.assertIn("preview_loop", result)

    def test_preview_loop_initial_state(self) -> None:
        """Изначально error_count=0, last_reset_ts=None."""
        pl = self._dashboard()["preview_loop"]
        self.assertEqual(pl["error_count"], 0)
        self.assertIsNone(pl["last_reset_ts"])

    def test_preview_loop_reflects_error_count(self) -> None:
        """После ручного инкремента error_count отражается в dashboard."""
        self.service._preview_error_count = 7
        pl = self._dashboard()["preview_loop"]
        self.assertEqual(pl["error_count"], 7)

    def test_preview_loop_reflects_last_reset_ts(self) -> None:
        """last_reset_ts возвращается когда задан."""
        ts = time.time()
        self.service._preview_error_last_reset_ts = ts
        pl = self._dashboard()["preview_loop"]
        self.assertAlmostEqual(pl["last_reset_ts"], ts, places=3)

    def test_preview_loop_reset_updates_timestamp(self) -> None:
        """После сброса ошибок _preview_error_last_reset_ts обновляется."""
        self.service._preview_error_count = 3
        before = time.time()
        # Симулируем успешный снапшот (тот же код из preview loop)
        if self.service._preview_error_count > 0:
            self.service._preview_error_last_reset_ts = time.time()
        self.service._preview_error_count = 0
        after = time.time()

        pl = self._dashboard()["preview_loop"]
        self.assertEqual(pl["error_count"], 0)
        self.assertIsNotNone(pl["last_reset_ts"])
        self.assertGreaterEqual(pl["last_reset_ts"], before)
        self.assertLessEqual(pl["last_reset_ts"], after)


class ListLlmModelsTestCase(unittest.TestCase):
    """Тесты IPC-метода list_llm_models (динамический список LM Studio моделей)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=self.store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()

    def _req(self, params=None):
        return self.service.handle_request(
            {"id": "llm_models_1", "method": "list_llm_models", "params": params or {}}
        )

    def test_method_registered(self) -> None:
        """list_llm_models зарегистрирован и не возвращает unknown_method."""
        from unittest.mock import patch, MagicMock

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
        with patch("requests.get", return_value=fake_resp):
            resp = self._req()

        # Must not be unknown_method — handler is registered
        self.assertTrue(resp.get("ok"), f"unexpected: {resp}")
        result = resp.get("result", {})
        # Result shape sanity
        self.assertIn("models", result)

    def test_returns_sorted_model_list(self) -> None:
        """При успешном HTTP 200 возвращает отсортированный список model id."""
        from unittest.mock import patch, MagicMock

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "data": [
                {"id": "zzz-model"},
                {"id": "aaa-model"},
                {"id": "mmm-model"},
            ]
        }
        # list_llm_models routes through the live LLMOpsService via the dispatch table;
        # only the outbound HTTP call needs mocking (#47: dead in-class copy deleted).
        with patch("requests.get", return_value=fake_resp):
            resp = self._req()

        self.assertTrue(resp.get("ok"), f"unexpected: {resp}")
        result = resp["result"]
        self.assertIn("models", result)
        self.assertEqual(result["models"], sorted(["zzz-model", "aaa-model", "mmm-model"]))
        self.assertIsNone(result["error"])

    def test_http_error_returns_empty_list(self) -> None:
        """HTTP ошибка (например 401) → models=[], error содержит описание."""
        from unittest.mock import patch, MagicMock

        fake_resp = MagicMock()
        fake_resp.status_code = 401
        with patch("requests.get", return_value=fake_resp):
            resp = self._req()

        self.assertTrue(resp.get("ok"), f"unexpected: {resp}")
        result = resp["result"]
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn("401", result["error"])

    def test_connection_error_returns_empty_list(self) -> None:
        """При сетевой ошибке (LM Studio недоступен) → models=[], error непустой."""
        from unittest.mock import patch
        import requests

        with patch("requests.get", side_effect=requests.ConnectionError("refused")):
            resp = self._req()

        self.assertTrue(resp.get("ok"), f"unexpected: {resp}")
        result = resp["result"]
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result["error"])
        self.assertGreater(len(result["error"]), 0)

    def test_skips_items_without_id(self) -> None:
        """Элементы без поля id пропускаются."""
        from unittest.mock import patch, MagicMock

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "data": [
                {"id": "good-model"},
                {"name": "no-id-here"},
                {},
                {"id": "another-good"},
            ]
        }
        with patch("requests.get", return_value=fake_resp):
            resp = self._req()

        self.assertTrue(resp.get("ok"), f"unexpected: {resp}")
        models = resp["result"]["models"]
        self.assertIn("good-model", models)
        self.assertIn("another-good", models)
        self.assertEqual(len(models), 2)


class FeatureFlagsInitOrderTestCase(unittest.TestCase):
    """W1481 N4 HIGH: _feature_flags must be initialised before _llm_rewriter injection.

    Guard against regression where someone adds
    ``self._llm_rewriter._feature_flags = self._feature_flags``
    in service.__init__ BEFORE ``self._feature_flags = FeatureFlags(...)`` is executed,
    which raises AttributeError on every direct BackendService() instantiation.
    """

    def _make_service(self) -> "BackendService":
        import shutil
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        store = StateStore(Path(tmp) / "data")
        service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        # Зарегистрирован после rmtree, поэтому LIFO сначала закроет worker-ы.
        self.addCleanup(service.close)
        return service

    def test_backend_service_initializes_without_attribute_error(self) -> None:
        """BackendService() must not raise AttributeError on instantiation.

        Regression guard for W1481 N4: if _feature_flags wiring is placed before
        FeatureFlags() construction the interpreter raises AttributeError because
        self._feature_flags does not exist yet.
        """
        try:
            svc = self._make_service()
        except AttributeError as exc:
            self.fail(
                f"BackendService.__init__ raised AttributeError — likely "
                f"_feature_flags referenced before it was initialised: {exc}"
            )
        self.assertTrue(hasattr(svc, "_feature_flags"), "_feature_flags must exist after __init__")

    def test_feature_flags_injected_into_llm_rewriter(self) -> None:
        """After init, _llm_rewriter._feature_flags must point to self._feature_flags.

        Validates W979 F4 wiring: rewrite() reads _feature_flags via getattr; the
        attribute must be the FeatureFlags instance owned by BackendService so that
        IPC set_feature_flag changes are reflected immediately during rewrite().
        """
        svc = self._make_service()
        if svc._llm_rewriter is None:
            self.skipTest("LLM rewriter disabled (LLM_ENABLED=False) — skip wiring check")
        self.assertIs(
            svc._llm_rewriter._feature_flags,
            svc._feature_flags,
            "_llm_rewriter._feature_flags must be the same object as _feature_flags",
        )

    def test_feature_flags_assigned_before_llm_rewriter_inject(self) -> None:
        """AST-level regression: _feature_flags must be assigned before the injection line.

        Parses service.py and checks that the ``self._feature_flags = FeatureFlags(...)``
        assignment appears at a lower line number than any
        ``self._llm_rewriter._feature_flags = ...`` assignment, catching copy-paste
        mistakes before they reach production (W1481 N4 regression guard).
        """
        import ast

        service_path = Path(__file__).resolve().parents[1] / "backend" / "service.py"
        source = service_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Walk the __init__ method body only
        init_body = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "BackendService":
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
                        init_body = item
                        break

        self.assertIsNotNone(init_body, "BackendService.__init__ not found in service.py")

        ff_init_line = None
        ff_inject_line = None

        for node in ast.walk(init_body):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    # self._feature_flags = FeatureFlags(...)
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "_feature_flags"
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        ff_init_line = node.lineno
                    # self._llm_rewriter._feature_flags = ...
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "_feature_flags"
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "_llm_rewriter"
                    ):
                        ff_inject_line = node.lineno

        # If there is no injection line, wiring is absent — nothing to order-check
        if ff_inject_line is None:
            return

        self.assertIsNotNone(
            ff_init_line,
            "self._feature_flags = FeatureFlags(...) assignment not found in __init__",
        )
        self.assertLess(
            ff_init_line,
            ff_inject_line,
            f"self._feature_flags initialised at line {ff_init_line} but "
            f"self._llm_rewriter._feature_flags injection is at line {ff_inject_line} — "
            "injection must come AFTER initialisation (W1481 N4 regression)",
        )


# ---------------------------------------------------------------------------
# Живой инцидент 2026-08-04 — BackendService.close() обязан закрыть transcriber
#
# Гейт pre_merge_py312_check.sh обнаружил висящий gigaam_worker.py после
# прогона этого файла. Корень: реальный Transcriber (сконструированный, когда
# BackendService(store=...) вызван БЕЗ transcriber= — см.
# BackendServiceLLMInitializationTestCase выше) держит background-warmup'нутый
# GigaAM subprocess-воркер; close() никогда его не закрывал. Цепочка фикса:
# Transcriber.close() → AudioEngine.close() → STTRouter.close() (см.
# test_engine_transcriber_close_lifecycle_2026_08_04.py,
# test_stt_router.py::TestSTTRouterClose).
# ---------------------------------------------------------------------------

class BackendServiceCloseTranscriberTestCase(unittest.TestCase):
    """close() обязан закрыть transcriber, но не падать на fake без close()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    def test_close_calls_transcriber_close_when_present(self):
        from unittest.mock import MagicMock

        service = BackendService(
            store=self.store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        mock_close = MagicMock()
        service.transcriber.close = mock_close  # инжектируем close() поверх FakeTranscriber

        service.close()

        mock_close.assert_called_once()

    def test_close_does_not_raise_when_transcriber_has_no_close(self):
        """FakeTranscriber (как определён в этом файле) НЕ имеет close() —
        duck-typed guard в BackendService.close() обязан тихо это пропустить."""
        service = BackendService(
            store=self.store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        self.assertFalse(hasattr(service.transcriber, "close"))

        service.close()  # не должен бросить AttributeError


if __name__ == "__main__":
    unittest.main()
