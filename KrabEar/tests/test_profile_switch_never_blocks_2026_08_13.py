"""Переключение профиля STT не имеет права ждать GPU-лок вечно.

Живой инцидент 2026-08-13 05:13 — диктовка владельца потеряна, бэкенд убит:

    05:13:15  GigaAM не распознал речь — request-local fallback на Whisper
    05:13:23  Смена профиля STT: balanced -> max   ← блокировка ЗДЕСЬ
    05:16:21  handle_request завис дольше 180с (method=stop_recording)

`set_quality_profile` сам по себе модель не грузит — он меняет два поля и
делает best-effort очистку Metal-кэша. Но очистка бралась под `mlx_lock()`
БЕЗ таймаута, тогда как соседний межпроцессный лок в той же строке уже умел
деградировать (`degrade_on_timeout=True`) — классическая асимметрия соседних
гейтов. Пока превью держало лок повисшим Whisper'ом, финальная транскрибация
диктовки стояла на очистке кэша — необязательной оптимизации.

Очистка кэша — оптимизация, а не корректность: не смогли взять лок за
бюджет — пропускаем и идём дальше.
"""
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine  # noqa: E402
from core.mlx_lock import mlx_lock  # noqa: E402


class _MinimalEngineState:
    """Минимальный носитель полей, которых касается set_quality_profile.

    Конструировать настоящий AudioEngine здесь незачем: проверяем поведение
    ОДНОГО метода относительно лока, а не инициализацию движка.
    """

    def __init__(self) -> None:
        self.quality_profile = "balanced"
        self.current_model = "stub-balanced-model"


class ProfileSwitchLockBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._lock = mlx_lock()
        self._release = threading.Event()
        self._held = threading.Event()
        self._holder: threading.Thread | None = None

    def tearDown(self) -> None:
        self._release.set()
        if self._holder is not None:
            self._holder.join(timeout=5)

    def _hold_mlx_lock_from_other_thread(self) -> None:
        """Удерживает mlx_lock из ДРУГОГО треда: RLock реентерабелен для
        своего треда, поэтому из этого же теста он бы не заблокировал."""
        def _holder() -> None:
            self._lock.acquire()
            try:
                self._held.set()
                self._release.wait(timeout=30)
            finally:
                self._lock.release()

        self._holder = threading.Thread(target=_holder, daemon=True)
        self._holder.start()
        self.assertTrue(self._held.wait(timeout=5), "держатель лока не стартовал")

    def test_profile_switch_returns_promptly_while_lock_is_held(self) -> None:
        self._hold_mlx_lock_from_other_thread()
        state = _MinimalEngineState()

        started = time.monotonic()
        AudioEngine.set_quality_profile(state, "max")
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed, 10.0,
            "смена профиля заблокировалась на удерживаемом mlx_lock: именно так "
            "финальная транскрибация диктовки владельца встала на 180с и "
            "закончилась SIGKILL бэкенда",
        )

    def test_profile_fields_updated_even_when_cache_flush_skipped(self) -> None:
        """Пропуск очистки кэша НЕ должен отменять саму смену профиля —
        иначе движок останется на старой модели и молча разойдётся с
        настройкой пользователя."""
        self._hold_mlx_lock_from_other_thread()
        state = _MinimalEngineState()

        AudioEngine.set_quality_profile(state, "max")

        self.assertEqual(state.quality_profile, "max")
        self.assertNotEqual(
            state.current_model, "stub-balanced-model",
            "current_model обязан смениться вместе с профилем",
        )


if __name__ == "__main__":
    unittest.main()
