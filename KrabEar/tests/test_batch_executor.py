"""Тесты пакетного выполнения IPC-методов (метод 'batch')."""

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
# Минимальные фейки
# ---------------------------------------------------------------------------

class FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.sample_rate = 16000

    def start(self):
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        carrier = np.sin(2.0 * np.pi * 210.0 * t)
        envelope = 0.45 + 0.55 * np.sin(2.0 * np.pi * 2.4 * t)
        wobble = 0.08 * np.sin(2.0 * np.pi * 23.0 * t)
        return (0.06 * carrier * envelope + wobble).astype(np.float32), 1.0

    def snapshot_audio(self, max_duration_sec=12.0):
        return np.ones(32000, dtype=np.float32), 1.0


class FakeTranscriber:
    def __init__(self):
        self.counter = 0

    def transcribe(self, audio_data, quality_profile="balanced", cleanup_profile="soft",
                   domain="casual", extra_vocabulary=None, lang_hint=None):
        self.counter += 1
        return f"тестовая строка #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return "preview"


class FakeTranslator:
    def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
        return TranslationResult(
            text="", status="not_requested", source_lang="", target_lang="", mode="off", engine="fake"
        )


# ---------------------------------------------------------------------------
# Вспомогательный базовый класс
# ---------------------------------------------------------------------------

class BatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            translator=FakeTranslator(),
        )
        self.addCleanup(self.service.close)

    def batch(self, requests, request_id="b1"):
        return self.service.handle_request(
            {"id": request_id, "method": "batch", "params": {"requests": requests}}
        )


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestBatchBasic(BatchTestBase):
    """Базовое поведение пакетного исполнителя."""

    def test_empty_batch_returns_zero_counts(self):
        """Пустой список запросов должен вернуть нули."""
        resp = self.batch([])
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["results"], [])

    def test_single_valid_method(self):
        """Один корректный метод — succeeded=1, failed=0."""
        resp = self.batch([{"method": "ping"}])
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["method"], "ping")
        self.assertIn("result", result["results"][0])

    def test_multiple_valid_methods(self):
        """Два корректных метода — succeeded=2."""
        resp = self.batch([
            {"method": "ping"},
            {"method": "get_settings"},
        ])
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["succeeded"], 2)
        self.assertEqual(r["failed"], 0)

    def test_unknown_method_counts_as_failed(self):
        """Неизвестный метод должен попасть в failed, но не прерывать пакет."""
        resp = self.batch([
            {"method": "ping"},
            {"method": "nonexistent_method_xyz"},
            {"method": "get_settings"},
        ])
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["succeeded"], 2)
        self.assertEqual(r["failed"], 1)
        # Второй элемент — ошибка
        self.assertFalse(r["results"][1]["ok"])
        self.assertIn("error", r["results"][1])
        # Первый и третий — успех
        self.assertTrue(r["results"][0]["ok"])
        self.assertTrue(r["results"][2]["ok"])

    def test_error_in_one_does_not_stop_others(self):
        """Ошибка в одном запросе не должна прерывать остальные."""
        resp = self.batch([
            {"method": "nonexistent_a"},
            {"method": "ping"},
            {"method": "nonexistent_b"},
            {"method": "get_settings"},
        ])
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["succeeded"], 2)
        self.assertEqual(r["failed"], 2)

    def test_method_field_preserved_in_results(self):
        """Имя метода должно быть скопировано в каждый результат."""
        resp = self.batch([
            {"method": "ping"},
            {"method": "get_settings"},
        ])
        methods = [e["method"] for e in resp["result"]["results"]]
        self.assertEqual(methods, ["ping", "get_settings"])

    def test_params_passed_through(self):
        """Параметры sub-запроса должны быть переданы в handler."""
        # get_history_page принимает page/page_size
        resp = self.batch([
            {"method": "get_history_page", "params": {"page": 1, "page_size": 5}},
        ])
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["succeeded"], 1)
        inner = r["results"][0]["result"]
        self.assertIn("items", inner)

    def test_missing_params_key_defaults_to_empty_dict(self):
        """Отсутствие ключа params не должно вызывать ошибку."""
        resp = self.batch([{"method": "ping"}])  # нет ключа params
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["succeeded"], 1)

    def test_non_dict_params_treated_as_empty(self):
        """Не-dict значение params должно обрабатываться как пустой dict."""
        resp = self.batch([{"method": "ping", "params": None}])
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["succeeded"], 1)


class TestBatchLimits(BatchTestBase):
    """Проверки граничных условий и лимитов."""

    def test_exactly_50_requests_allowed(self):
        """Ровно 50 запросов должны быть разрешены."""
        reqs = [{"method": "ping"}] * 50
        resp = self.batch(reqs)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["total"], 50)
        self.assertEqual(resp["result"]["succeeded"], 50)

    def test_51_requests_raises_error(self):
        """51 запрос должен вернуть ошибку (лимит превышен)."""
        reqs = [{"method": "ping"}] * 51
        resp = self.batch(reqs)
        self.assertFalse(resp["ok"])
        self.assertIn("error", resp)
        self.assertIn("лимит", resp["error"]["message"].lower())

    def test_non_list_requests_param_returns_error(self):
        """Не-список в params.requests должен вернуть ошибку."""
        resp = self.service.handle_request(
            {"id": "b1", "method": "batch", "params": {"requests": "not_a_list"}}
        )
        self.assertFalse(resp["ok"])

    def test_non_dict_element_counted_as_failed(self):
        """Не-dict элемент в списке requests должен быть помечен как failed."""
        resp = self.batch([
            {"method": "ping"},
            "not_a_dict",
            {"method": "ping"},
        ])
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["succeeded"], 2)
        self.assertEqual(r["failed"], 1)
        self.assertFalse(r["results"][1]["ok"])

    def test_missing_requests_key_returns_error(self):
        """Отсутствие ключа requests в params должно вернуть ошибку."""
        resp = self.service.handle_request(
            {"id": "b1", "method": "batch", "params": {}}
        )
        self.assertFalse(resp["ok"])

    def test_batch_registered_as_ipc_method(self):
        """Метод batch должен быть зарегистрирован и доступен через handle_request."""
        # Если метод неизвестен, вернётся unknown_method.
        resp = self.service.handle_request(
            {"id": "b1", "method": "batch", "params": {"requests": []}}
        )
        # Должен быть ok=True (не unknown_method)
        self.assertTrue(resp["ok"])

    def test_result_structure_keys(self):
        """Ответ batch должен содержать ключи results, total, succeeded, failed."""
        resp = self.batch([{"method": "ping"}])
        self.assertTrue(resp["ok"])
        result = resp["result"]
        for key in ("results", "total", "succeeded", "failed"):
            self.assertIn(key, result, f"Ключ '{key}' отсутствует в ответе")

    def test_succeeded_plus_failed_equals_total(self):
        """succeeded + failed всегда должны равняться total."""
        resp = self.batch([
            {"method": "ping"},
            {"method": "unknown_xyz"},
            {"method": "get_settings"},
            "bad_element",
        ])
        r = resp["result"]
        self.assertEqual(r["succeeded"] + r["failed"], r["total"])


if __name__ == "__main__":
    unittest.main()
