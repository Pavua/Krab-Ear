"""Каталог LLM-моделей: list_llm_models обязан отдавать ДОСТУПНЫЕ модели.

🔴 Живой инцидент 2026-08-26: dropdown выбора модели показывал только
захардкоженный в Swift список, потому что IPC отдавал ПУСТОЙ каталог при
`error: null`. Замер на машине владельца (LM Studio 0.4.21, 105 моделей на
диске, ни одна не загружена):

    /api/v0/models  → HTTP 200, 105 моделей   (нативный каталог)
    /v1/models      → HTTP 200, 105 моделей   (OpenAI-compat)
    /api/v1/models  → HTTP 200,   0 моделей   ← его и звал backend

То есть «фикс» W68 (PR #415) перевёл вызов на путь, который отвечает 200 с
пустым телом, и функция замолчала навсегда — без ошибки, без пустого лога.
Класс «всегда зелёный монитор»: ошибки нет, данных тоже.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


_V0_PAYLOAD = {
    "data": [
        {"id": "gemma-4-26b-a4b-it@4bit", "type": "vlm", "state": "not-loaded",
         "arch": "gemma4", "quantization": "4bit", "max_context_length": 262144},
        {"id": "qwen3.5-9b@6bit", "type": "llm", "state": "loaded",
         "arch": "qwen35", "quantization": "6bit", "max_context_length": 32768},
        {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded",
         "arch": "nomic", "quantization": "f16", "max_context_length": 2048},
    ]
}


def _resp(status: int, payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def _service(settings: dict | None = None):
    from backend.llm_ops_service import LLMOpsService

    settings_svc = MagicMock()
    settings_svc.cached_settings.return_value = settings or {
        "llm_base_url": "http://127.0.0.1:1234/v1",
        "llm_api_key": "secret-token",
    }
    return LLMOpsService(
        store=MagicMock(), settings_svc=settings_svc, transcriber=MagicMock()
    )


class CatalogEndpointTest(unittest.TestCase):
    def test_uses_native_catalog_endpoint_not_api_v1(self):
        """🔴 /api/v1/models отвечает 200 с пустым списком — он не каталог."""
        svc = _service()
        with patch("requests.get", return_value=_resp(200, _V0_PAYLOAD)) as get:
            svc.handle_list_llm_models({})
        url = get.call_args[0][0] if get.call_args[0] else get.call_args.kwargs["url"]
        self.assertIn("/api/v0/models", url)
        self.assertNotIn("/api/v1/models", url)

    def test_returns_all_downloaded_models_including_vlm(self):
        """VLM тоже пригодна для рерайта: gemma-4-26b-a4b-it@4bit имеет type=vlm,
        и фильтр «только llm» выкинул бы ровно ту модель, которую просил владелец."""
        svc = _service()
        with patch("requests.get", return_value=_resp(200, _V0_PAYLOAD)):
            res = svc.handle_list_llm_models({})
        self.assertIn("gemma-4-26b-a4b-it@4bit", res["models"])
        self.assertIn("qwen3.5-9b@6bit", res["models"])

    def test_excludes_embeddings(self):
        """Эмбеддинг-модели не умеют chat/completions — в списке рерайта им не место."""
        svc = _service()
        with patch("requests.get", return_value=_resp(200, _V0_PAYLOAD)):
            res = svc.handle_list_llm_models({})
        self.assertNotIn("text-embedding-nomic", res["models"])

    def test_exposes_metadata_for_ui(self):
        """UI обязан отличать загруженную модель и знать тип/квантизацию."""
        svc = _service()
        with patch("requests.get", return_value=_resp(200, _V0_PAYLOAD)):
            res = svc.handle_list_llm_models({})
        details = {d["id"]: d for d in res.get("model_details", [])}
        self.assertTrue(details["qwen3.5-9b@6bit"]["loaded"])
        self.assertFalse(details["gemma-4-26b-a4b-it@4bit"]["loaded"])
        self.assertEqual(details["gemma-4-26b-a4b-it@4bit"]["type"], "vlm")

    def test_falls_back_to_openai_endpoint(self):
        """Старый LM Studio без /api/v0: каталог берём из /v1/models."""
        svc = _service()
        calls: list[str] = []

        def _fake_get(url, **kw):
            calls.append(url)
            if "/api/v0/models" in url:
                return _resp(404, {})
            return _resp(200, {"data": [{"id": "legacy-model"}]})

        with patch("requests.get", side_effect=_fake_get):
            res = svc.handle_list_llm_models({})
        self.assertIn("legacy-model", res["models"])
        self.assertTrue(any("/v1/models" in c for c in calls))

    def test_empty_catalog_is_reported_as_error_not_silence(self):
        """🔴 Корень инцидента: 200 + пустой список ≠ «моделей нет».

        Молчаливый пустой ответ невозможно отличить от исправной работы —
        UI показывал хардкод, а пользователь считал, что видит правду.
        """
        svc = _service()
        with patch("requests.get", return_value=_resp(200, {"data": []})):
            res = svc.handle_list_llm_models({})
        self.assertEqual(res["models"], [])
        self.assertIsNotNone(res["error"], "пустой каталог обязан объясняться")

    def test_http_error_is_reported(self):
        svc = _service()
        with patch("requests.get", return_value=_resp(401, {})):
            res = svc.handle_list_llm_models({})
        self.assertIn("401", str(res["error"]))


if __name__ == "__main__":
    unittest.main()
