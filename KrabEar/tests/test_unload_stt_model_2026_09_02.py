"""IPC «выгрузить модель STT из памяти» (02.09.2026).

Владелец попросил управлять жизнью модели из панели: когда грузить, сколько
держать в памяти, когда выгружать. Загрузка уже была (`warmup_stt`), выгрузки
не было вовсе — освободить память можно было только перезапуском бэкенда.

Выгрузка обязана быть безопасной: кэш адаптера сбрасывается, следующая
транскрибация поднимает его заново. Поэтому хендлер не «ломает» движок, а лишь
отпускает удерживаемые ресурсы.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.stt_management_service import STTManagementService  # noqa: E402


class UnloadSTTModelTest(unittest.TestCase):
    def _service(self, engine):
        transcriber = MagicMock()
        transcriber.engine = engine
        svc = STTManagementService.__new__(STTManagementService)
        svc._transcriber = transcriber
        return svc

    def test_unload_calls_engine_and_reports_success(self):
        engine = MagicMock()
        svc = self._service(engine)
        result = svc.handle_unload_stt_model({})
        engine.unload_stt_models.assert_called_once_with()
        self.assertTrue(result["unloaded"])
        self.assertIsNone(result["error"])

    def test_missing_engine_is_reported_not_raised(self):
        """Движка нет — это ответ «не выгрузили», а не исключение в IPC."""
        svc = STTManagementService.__new__(STTManagementService)
        svc._transcriber = None
        result = svc.handle_unload_stt_model({})
        self.assertFalse(result["unloaded"])
        self.assertIn("engine", (result["error"] or "").lower())

    def test_engine_error_is_swallowed_into_result(self):
        """Ошибка выгрузки не должна валить IPC: память освободить не вышло,
        но движок обязан продолжать работать, а владелец — увидеть причину."""
        engine = MagicMock()
        engine.unload_stt_models.side_effect = RuntimeError("worker busy")
        svc = self._service(engine)
        result = svc.handle_unload_stt_model({})
        self.assertFalse(result["unloaded"])
        self.assertIn("worker busy", result["error"])

    def test_engine_without_method_is_reported(self):
        """Старый движок без метода — понятный отказ, а не AttributeError."""
        engine = object()
        svc = self._service(engine)
        result = svc.handle_unload_stt_model({})
        self.assertFalse(result["unloaded"])
        self.assertIsNotNone(result["error"])


if __name__ == "__main__":
    unittest.main()
