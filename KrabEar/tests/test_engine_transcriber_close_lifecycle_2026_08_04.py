"""AudioEngine/Transcriber.close() — живой инцидент 2026-08-04.

Гейт `pre_merge_py312_check.sh` обнаружил висящий процесс `gigaam_worker.py`
после прогона `test_backend_service.py`. Корень: `AudioEngine.__init__`
безусловно спавнит background-тред `GigaAM-warmup` (если
`STT_GIGAAM_ENABLED=True` и `skip_gigaam_warmup=False`, дефолт), который
конструирует реальный `_GigaAMSubprocessSession` (реальный `subprocess.Popen`
на `gigaam_worker.py`) через `STTRouter.get_gigaam_adapter()`. Ни у
`AudioEngine`, ни у `Transcriber`, ни у `BackendService.close()` не было пути
это закрыть — `STTRouter._close_cached_gigaam_adapter()` вызывался только
реактивно, при смене конфига.

Цепочка фикса: `STTRouter.close()` (публичная точка входа, см.
test_stt_router.py) → `AudioEngine.close()` → `Transcriber.close()` →
`BackendService.close()` (см. test_backend_service.py / service.py).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_engine_transcriber_close_lifecycle_2026_08_04.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine
from backend.transcriber import Transcriber


class AudioEngineCloseTests(unittest.TestCase):
    """AudioEngine.close() обязан закрыть кэшированный GigaAM-адаптер."""

    def setUp(self) -> None:
        # skip_gigaam_warmup=True — не спавним реальный background-тред/subprocess
        # в этом тесте; закрытие проверяем на ВРУЧНУЮ подставленном mock-роутере.
        self.engine = AudioEngine(skip_gigaam_warmup=True)
        self.engine._router = MagicMock()

    def test_close_delegates_to_router(self):
        self.engine.close()

        self.engine._router.close.assert_called_once()

    def test_close_never_raises_when_router_close_fails(self):
        self.engine._router.close.side_effect = RuntimeError("subprocess wait failed")

        self.engine.close()  # daemon-совместимый контракт — не должен бросить


class TranscriberCloseTests(unittest.TestCase):
    """Transcriber.close() делегирует в engine.close(), если тот есть."""

    def test_close_delegates_to_engine(self):
        fake_engine = MagicMock()
        transcriber = Transcriber(engine=fake_engine)

        transcriber.close()

        fake_engine.close.assert_called_once()

    def test_close_noop_when_engine_has_no_close(self):
        """Duck-typed guard: engine без close() (например legacy-фейк) не роняет вызов."""
        class _EngineWithoutClose:
            pass

        transcriber = Transcriber(engine=_EngineWithoutClose())

        transcriber.close()  # не должен бросить AttributeError


if __name__ == "__main__":
    unittest.main()
