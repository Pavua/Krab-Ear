"""Реактивное освобождение памяти при mlx.oom.

До этой волны выгрузка модели была ТОЛЬКО проактивной — на старте записи
(`llm_brain_unload_on_recording`). На факт реальной нехватки памяти система не
реагировала никак: рождался код `mlx.oom`, владельцу показывался тост, и всё.
Механизм выгрузки при этом существовал и работал.

🔴 Главное ограничение слоя: `EventBus.add_listener` вызывает колбэк СИНХРОННО
внутри `emit()`, в потоке эмиттера — то есть в STT-пайплайне. Документация шины
прямо требует неблокирующий колбэк. Блокирующая работа здесь заморозила бы
распознавание — freeze-класс из кодекса. Поэтому обработчик обязан вернуть
управление немедленно, а всю работу (чтение лизы через flock, HTTP к LM Studio)
делать в отдельном потоке.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.oom_auto_relief import OomAutoRelief  # noqa: E402


def _settings(**overrides):
    base = {
        "mlx_oom_auto_unload_enabled": True,
        "mlx_oom_auto_unload_cooldown_sec": 600.0,
        "llm_brain_model": "qwen/qwen3.6-27b",
        # Реальный рантайм-ключ (SettingsService/settings.json) — нижний регистр,
        # как везде в проекте (llm_ops_service.py, recording_core_service.py,
        # rest_server.py). НЕ "LLM_BASE_URL" — та Pydantic-константа живёт
        # только в core/config.py::Settings, а не в этом словаре.
        "llm_base_url": "http://localhost:1234/v1",
    }
    base.update(overrides)
    svc = MagicMock()
    svc.cached_settings.return_value = base
    return svc


def _oom_event():
    return "krab_error", {"code": "mlx.oom", "message_user": "Не хватило памяти"}


class OomAutoReliefTest(unittest.TestCase):
    def test_oom_triggers_unload(self):
        relief = OomAutoRelief(settings_service=_settings())
        with patch("backend.oom_auto_relief.unload_model_async") as unload, \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=None):
            relief.handle_event(*_oom_event())
            relief.wait_for_idle(timeout=5.0)
        unload.assert_called_once()

    def test_other_error_codes_are_ignored(self):
        relief = OomAutoRelief(settings_service=_settings())
        with patch("backend.oom_auto_relief.unload_model_async") as unload, \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=None):
            relief.handle_event("krab_error", {"code": "stt.timeout"})
            relief.wait_for_idle(timeout=5.0)
        unload.assert_not_called()

    def test_handler_returns_immediately_never_blocks_emitter(self):
        """🔴 Колбэк живёт в потоке STT-пайплайна — блокировка заморозит распознавание."""
        slow_started = threading.Event()

        def _slow_unload(*_a, **_kw):
            slow_started.set()
            time.sleep(2.0)

        relief = OomAutoRelief(settings_service=_settings())
        with patch("backend.oom_auto_relief.unload_model_async", side_effect=_slow_unload), \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=None):
            t0 = time.monotonic()
            relief.handle_event(*_oom_event())
            elapsed = time.monotonic() - t0
            self.assertTrue(slow_started.wait(timeout=5.0), "работа не начиналась вовсе")
            relief.wait_for_idle(timeout=10.0)
        self.assertLess(elapsed, 0.3, f"handle_event блокировал эмиттер {elapsed:.2f}s")

    def test_cooldown_suppresses_storm(self):
        """Шторм OOM не должен долбить выгрузку — одна попытка на окно."""
        relief = OomAutoRelief(settings_service=_settings())
        with patch("backend.oom_auto_relief.unload_model_async") as unload, \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=None):
            for _ in range(5):
                relief.handle_event(*_oom_event())
            relief.wait_for_idle(timeout=10.0)
        self.assertEqual(unload.call_count, 1, "cooldown не удержал шторм")

    def test_disabled_switch_blocks_everything(self):
        relief = OomAutoRelief(settings_service=_settings(mlx_oom_auto_unload_enabled=False))
        with patch("backend.oom_auto_relief.unload_model_async") as unload, \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=None):
            relief.handle_event(*_oom_event())
            relief.wait_for_idle(timeout=5.0)
        unload.assert_not_called()

    def test_foreign_brain_lease_blocks_unload(self):
        """Лизу держит Главный Краб — его inference обрывать нельзя."""
        foreign = {"owner": "krab_main", "pid": 7, "acquired_ts": 0, "exp_ts": 9e18}
        relief = OomAutoRelief(settings_service=_settings())
        with patch("backend.oom_auto_relief.unload_model_async") as unload, \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=foreign):
            relief.handle_event(*_oom_event())
            relief.wait_for_idle(timeout=5.0)
        unload.assert_not_called()

    def test_listener_never_raises_into_emitter(self):
        """Сбой внутри обработчика не смеет всплыть в STT-поток."""
        relief = OomAutoRelief(settings_service=None)  # намеренно сломанная зависимость
        relief.handle_event(*_oom_event())  # не должно бросить
        relief.wait_for_idle(timeout=5.0)

    def test_unload_receives_real_llm_base_url_not_empty(self):
        """🔴 2026-08-20: `_relieve` читал ключ `LLM_BASE_URL` (верхний регистр) из
        рантайм-словаря настроек, который на деле хранит `llm_base_url` (нижний,
        как везде в SettingsService/settings.json) — `.get()` всегда мазал мимо,
        base_url всегда был "", и `lm_studio_lifecycle` отказывался выгружать
        модель (`_scheme_allowed("")` == False). Прод падал в OOM-крэш повторно,
        потому что аварийная разгрузка молча не срабатывала. Сигнатура бага —
        ИМЕННО значение base_url, а не факт вызова unload_model_async.
        """
        distinctive_url = "http://127.0.0.1:1234/v1"
        relief = OomAutoRelief(settings_service=_settings(**{"llm_base_url": distinctive_url}))
        with patch("backend.oom_auto_relief.unload_model_async") as unload, \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=None):
            relief.handle_event(*_oom_event())
            relief.wait_for_idle(timeout=5.0)
        unload.assert_called_once_with(distinctive_url, "qwen/qwen3.6-27b")

    def test_cooldown_expires(self):
        relief = OomAutoRelief(settings_service=_settings(mlx_oom_auto_unload_cooldown_sec=0.0))
        with patch("backend.oom_auto_relief.unload_model_async") as unload, \
                patch("backend.oom_auto_relief.current_lease_holder", return_value=None):
            relief.handle_event(*_oom_event())
            relief.wait_for_idle(timeout=5.0)
            relief.handle_event(*_oom_event())
            relief.wait_for_idle(timeout=5.0)
        self.assertEqual(unload.call_count, 2, "нулевой cooldown обязан пропускать повтор")


class WiringContractTest(unittest.TestCase):
    """🔴 Source-контракт: проводка обязана существовать в проде, а не только в тестах.

    Ровно этот класс мы чинили в соседних волнах: `setupErrorBus` был написан,
    покрыт тестами компонентов и НИКОГДА не вызывался из реального старта. Здесь
    проверяем, что слушатель действительно регистрируется на шине.
    """

    def _service_source(self) -> str:
        return (PROJECT_ROOT / "backend" / "service.py").read_text(encoding="utf-8")

    def test_relief_is_constructed_in_service(self):
        src = self._service_source()
        self.assertIn("OomAutoRelief(", src, "OomAutoRelief нигде не создаётся в проде")

    def test_relief_handler_is_registered_on_the_bus(self):
        """Создать объект мало — без add_listener он молчаливый no-op."""
        src = self._service_source()
        self.assertIn(
            "add_listener(self._oom_auto_relief.handle_event)", src,
            "OomAutoRelief создан, но не подписан на шину — декоративная проводка",
        )


if __name__ == "__main__":
    unittest.main()
