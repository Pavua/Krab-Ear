"""Формы каталога LM Studio: /api/v1/models отдаёт models[].key, а не data[].id.

Живой замер 03.09.2026 (LM Studio 0.4.x, 105 моделей на диске, токен обязателен):

    GET /api/v1/models → 200 {"models": [{"key": "...", "loaded_instances": [...]}]}
    GET /api/v0/models → 200 {"data":   [{"id":  "...", "state": "not-loaded"}]}
    GET /v1/models     → 200 {"data":   [{"id":  "..."}]}                (OpenAI-compat)

Парсер `data[].id` на первой форме всегда даёт пустой список — отсюда вечное
``has_model: false`` в probe_llm_http при реально доступной модели. Ровно так же
молча слепли ``lm_studio_lifecycle.model_loaded`` (всегда None) и список
llm_models в ``rest_server``.

🔴 Отдельный инвариант: наличие в КАТАЛОГЕ ≠ ЗАГРУЖЕНА. Каталог перечисляет всё
скачанное; признак загрузки несут только ``state`` (v0) и ``loaded_instances``
(v1), а OpenAI-совместимая форма его не знает — там ``loaded`` обязан остаться
None («не смог оценить»), а не False.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET = "gigachat3.1-10b-a1.8b-mlx-oq8"

# Реальные фрагменты живых ответов (обрезаны до двух моделей).
V1_NATIVE = {
    "models": [
        {
            "type": "llm",
            "publisher": "deepsweet",
            "key": TARGET,
            "display_name": "GigaChat3.1 10B A1.8B OQ8",
            "architecture": "deepseek_v3",
            "quantization": {"name": "8bit", "bits_per_weight": 8},
            "loaded_instances": [],
            "max_context_length": 262144,
            "format": "mlx",
        },
        {
            "type": "llm",
            "publisher": "Vikhrmodels",
            "key": "vistral-24b-instruct-mlx",
            "display_name": "Vistral 24B Instruct",
            "architecture": "mistral",
            "loaded_instances": [{"identifier": "vistral-24b-instruct-mlx"}],
        },
    ]
}

V0_NATIVE = {
    "object": "list",
    "data": [
        {
            "id": TARGET,
            "object": "model",
            "type": "llm",
            "publisher": "deepsweet",
            "arch": "deepseek_v3",
            "quantization": "8bit",
            "state": "not-loaded",
            "max_context_length": 262144,
        },
        {
            "id": "vistral-24b-instruct-mlx",
            "object": "model",
            "type": "llm",
            "state": "loaded",
        },
    ],
}

OPENAI_COMPAT = {
    "object": "list",
    "data": [
        {"id": TARGET, "object": "model", "owned_by": "organization_owner"},
        {"id": "vistral-24b-instruct-mlx", "object": "model"},
    ],
}


class CatalogParserTestCase(unittest.TestCase):
    """parse_lm_studio_catalog() нормализует все три живые формы."""

    def _parse(self, payload):
        from backend.lm_studio_lifecycle import parse_lm_studio_catalog
        return parse_lm_studio_catalog(payload)

    def test_v1_native_form_yields_ids_from_key(self):
        entries = self._parse(V1_NATIVE)
        self.assertEqual([e["id"] for e in entries], [TARGET, "vistral-24b-instruct-mlx"])

    def test_v1_native_loaded_state_from_loaded_instances(self):
        by_id = {e["id"]: e for e in self._parse(V1_NATIVE)}
        self.assertIs(by_id[TARGET]["loaded"], False)
        self.assertIs(by_id["vistral-24b-instruct-mlx"]["loaded"], True)

    def test_v0_native_form_yields_ids_and_state(self):
        by_id = {e["id"]: e for e in self._parse(V0_NATIVE)}
        self.assertEqual(sorted(by_id), sorted([TARGET, "vistral-24b-instruct-mlx"]))
        self.assertIs(by_id[TARGET]["loaded"], False)
        self.assertIs(by_id["vistral-24b-instruct-mlx"]["loaded"], True)

    def test_openai_compat_has_ids_but_unknown_loaded(self):
        by_id = {e["id"]: e for e in self._parse(OPENAI_COMPAT)}
        self.assertEqual(sorted(by_id), sorted([TARGET, "vistral-24b-instruct-mlx"]))
        # Формa не несёт состояния: None = «не смог оценить», не False.
        self.assertIsNone(by_id[TARGET]["loaded"])

    def test_unrecognised_form_is_none_not_empty(self):
        """🔴 None = «форму не разобрал», [] = «разобрал, каталог пуст»."""
        for junk in ({}, {"data": None}, {"models": "nope"}, [], None, "text"):
            self.assertIsNone(self._parse(junk), msg=repr(junk))

    def test_recognised_but_empty_catalog_is_empty_list(self):
        self.assertEqual(self._parse({"data": [], "object": "list"}), [])
        self.assertEqual(self._parse({"models": []}), [])
        # Форма распознана, элементы мусорные — каталог пуст, но не «не понял».
        self.assertEqual(self._parse({"data": [1, 2]}), [])


class PassiveHealthCheckFormsTestCase(unittest.TestCase):
    """passive_health_check() видит модель во ВСЕХ трёх формах каталога."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model=TARGET,
        )

    def _resp(self, payload, status_code=200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = payload
        return resp

    def test_v1_native_form_is_understood(self):
        """Живой регресс: форма models[].key давала has_model=False при живой модели."""
        with patch.object(self.rewriter._session, "get", return_value=self._resp(V1_NATIVE)):
            self.assertEqual(self.rewriter.passive_health_check(), (True, True))

    def test_v0_native_form_is_understood(self):
        with patch.object(self.rewriter._session, "get", return_value=self._resp(V0_NATIVE)):
            self.assertEqual(self.rewriter.passive_health_check(), (True, True))

    def test_openai_compat_form_is_understood(self):
        with patch.object(self.rewriter._session, "get", return_value=self._resp(OPENAI_COMPAT)):
            self.assertEqual(self.rewriter.passive_health_check(), (True, True))

    def test_model_absent_from_catalog_is_reachable_without_model(self):
        other = {"models": [{"key": "vistral-24b-instruct-mlx"}]}
        with patch.object(self.rewriter._session, "get", return_value=self._resp(other)):
            self.assertEqual(self.rewriter.passive_health_check(), (True, False))

    def test_unparsed_form_falls_back_to_next_endpoint(self):
        """200 с неразобранной формой — молчание: пробуем следующий источник."""
        responses = [self._resp({"weird": "shape"}), self._resp(V0_NATIVE)]
        with patch.object(self.rewriter._session, "get", side_effect=responses) as mock_get:
            self.assertEqual(self.rewriter.passive_health_check(), (True, True))
        self.assertGreaterEqual(mock_get.call_count, 2)

    def test_recognised_empty_catalog_answers_immediately(self):
        """Пустой, но разобранный каталог — честное «модели нет», без перебора."""
        with patch.object(
            self.rewriter._session, "get", return_value=self._resp({"data": []})
        ) as mock_get:
            self.assertEqual(self.rewriter.passive_health_check(), (True, False))
        self.assertEqual(mock_get.call_count, 1)

    def test_non_200_stays_unreachable(self):
        with patch.object(self.rewriter._session, "get", return_value=self._resp({}, status_code=401)):
            self.assertEqual(self.rewriter.passive_health_check(), (False, False))


class ModelLoadedFormsTestCase(unittest.TestCase):
    """model_loaded() — про ЗАГРУЖЕНА, а не про «есть в каталоге»."""

    def _call(self, payload, *, api_key="sk-test", model_id=TARGET):
        from backend import lm_studio_lifecycle as lifecycle
        import json as _json

        captured = []

        class _Resp:
            status = 200

            def __init__(self, body):
                self._body = body

            def read(self):
                return _json.dumps(self._body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        payloads = payload if isinstance(payload, list) else [payload]

        def _open(req, timeout=None):
            captured.append(req)
            return _Resp(payloads[min(len(captured) - 1, len(payloads) - 1)])

        opener = MagicMock()
        opener.open.side_effect = _open
        with patch.object(lifecycle, "_SAFE_OPENER", opener):
            result = lifecycle.model_loaded(
                "http://127.0.0.1:1234/v1", model_id, api_key=api_key
            )
        return result, captured

    def test_catalog_presence_alone_is_not_loaded(self):
        """🔴 Регресс: модель скачана, но не загружена → False, не True."""
        result, _ = self._call(V0_NATIVE)
        self.assertIs(result, False)

    def test_loaded_model_is_true(self):
        result, _ = self._call(V0_NATIVE, model_id="vistral-24b-instruct-mlx")
        self.assertIs(result, True)

    def test_v1_native_loaded_instances_are_understood(self):
        result, _ = self._call(V1_NATIVE, model_id="vistral-24b-instruct-mlx")
        self.assertIs(result, True)

    def test_sends_bearer_token(self):
        """LM Studio владельца требует токен: без заголовка любой ответ — 401."""
        _, captured = self._call(V0_NATIVE)
        self.assertTrue(captured, "запрос не был отправлен")
        header = captured[0].get_header("Authorization")
        self.assertEqual(header, "Bearer sk-test")

    def test_unknown_state_source_returns_none(self):
        """Форма без признака загрузки → None, а не False."""
        result, _ = self._call(OPENAI_COMPAT)
        self.assertIsNone(result)

    def test_recognised_empty_catalog_is_false(self):
        """Каталог разобран и пуст — модели точно нет, это ответ, а не молчание."""
        result, _ = self._call({"data": [], "object": "list"})
        self.assertIs(result, False)


if __name__ == "__main__":
    unittest.main()
