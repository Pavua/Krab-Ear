"""`transcribe_preview` не должен ждать mlx_lock дольше короткого бюджета.

Живой инцидент 2026-08-01 (третий, корневой слой). После починки двух верхних
дефектов осталось: воркер `RealtimePartialTranscriber` не завершается ВООБЩЕ —
ни за 1.5 с (путь старта), ни за 30 с (честная остановка):

    realtime_partial worker не завершился за 1.5 с
    realtime_partial worker не завершился за 30.0 с
    Переполнение аудиобуфера во время записи

Механизм — композиция, а не одиночный баг:

  1. `stop_recording` запускает ФИНАЛЬНУЮ транскрибацию, которая держит
     `mlx_lock` десятки секунд (живой замер: `STT готово: 26.98s` — с
     ретраями по низкой уверенности через несколько моделей).
  2. Воркер превью в это время зовёт `transcribe_preview`, который берёт
     ТОТ ЖЕ `mlx_lock` БЕЗ таймаута — и блокируется на всё это время.
  3. Остановка воркера ждёт его join'ом. Воркер честно проверяет
     `_stop_event` до и после STT-вызова (realtime_partial.py:218,245), но
     прервать сам захват лока не может — проверки просто не выполняются.
  4. Join истекает, воркер остаётся жив, аудиобуфер переполняется.

Ключ: превью — BEST-EFFORT функция. Ждать GPU десятки секунд ради строки,
которая к моменту готовности уже устареет, бессмысленно; пропустить итерацию
и попробовать снова через интервал — правильное поведение. Блокирующий захват
превращает вспомогательную функцию в блокировщик основной.

🔴 Инвариант W1364 обязан выжить: `set_quality_profile` + `transcribe` остаются
АТОМАРНЫ внутри лока (см. test_preview_profile_toctou_W1364.py). Меняется
только СПОСОБ захвата — с бесконечного на ограниченный по времени.

🔴 У `transcribe_preview` три потребителя: `realtime_partial`,
`call_assist_service`, `meeting_session_service`. Все трое умеют пустой текст
(воркер: `if not text: continue`), поэтому отказ по таймауту безопасен для
всех — но выдаёт себя явным маркером в ответе, а не молча.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcriber import Transcriber  # noqa: E402
from core.mlx_lock import mlx_lock  # noqa: E402


def _make_transcriber(transcribe_result=None) -> Transcriber:
    engine = MagicMock()
    engine.transcribe.return_value = transcribe_result or {"text": "живой результат"}
    t = Transcriber.__new__(Transcriber)
    t.engine = engine
    return t


class TestPreviewDoesNotBlockOnBusyGpu(unittest.TestCase):
    """Занятый другим потоком mlx_lock не должен вешать превью."""

    def test_returns_quickly_when_lock_held_by_other_thread(self) -> None:
        from backend import transcriber as tr_mod

        budget = getattr(tr_mod, "PREVIEW_MLX_LOCK_TIMEOUT_SEC", None)
        self.assertIsNotNone(
            budget,
            "ожидается именованная константа PREVIEW_MLX_LOCK_TIMEOUT_SEC",
        )

        holder_got_lock = threading.Event()
        release_holder = threading.Event()

        def _hold() -> None:
            with mlx_lock():
                holder_got_lock.set()
                # Держим ДОЛЬШЕ бюджета — имитируем финальную транскрибацию
                # (живой замер: 26.98 s).
                release_holder.wait(timeout=float(budget) * 6)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        self.addCleanup(release_holder.set)
        self.assertTrue(holder_got_lock.wait(timeout=5), "держатель не взял лок")

        t = _make_transcriber()
        started = time.monotonic()
        result = t.transcribe_preview(audio_data=object())
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            float(budget) * 3,
            f"transcribe_preview ждал {elapsed:.1f} с при бюджете {budget} с — "
            "захват лока по-прежнему неограничен",
        )
        self.assertEqual(
            (result.get("text") or ""), "",
            "при недоступном GPU превью обязано вернуть пустой текст",
        )
        t.engine.transcribe.assert_not_called()

    def test_budget_is_short(self) -> None:
        from backend import transcriber as tr_mod

        budget = float(getattr(tr_mod, "PREVIEW_MLX_LOCK_TIMEOUT_SEC", 999))
        self.assertGreater(budget, 0.0, "нулевой бюджет обессмыслил бы превью")
        self.assertLessEqual(
            budget, 2.0,
            "бюджет ожидания GPU для BEST-EFFORT превью должен быть <= 2 с",
        )


class TestPreviewStillWorksWhenGpuFree(unittest.TestCase):
    """Свободный лок — превью работает как раньше (регресс-страховка)."""

    def test_transcribes_when_lock_available(self) -> None:
        t = _make_transcriber({"text": "живой результат"})
        result = t.transcribe_preview(audio_data=object())
        self.assertEqual(result.get("text"), "живой результат")
        t.engine.set_quality_profile.assert_called_once_with("balanced")
        t.engine.transcribe.assert_called_once()

    def test_reentrant_same_thread_still_works(self) -> None:
        """RLock реентерабелен: превью изнутри уже взятого лока не self-deadlock."""
        t = _make_transcriber({"text": "вложенный"})
        with mlx_lock():
            result = t.transcribe_preview(audio_data=object())
        self.assertEqual(result.get("text"), "вложенный")


class TestW1364AtomicityPreserved(unittest.TestCase):
    """Профиль и инференс обязаны остаться атомарными внутри лока."""

    def test_profile_and_transcribe_both_inside_lock(self) -> None:
        order: list[str] = []
        t = _make_transcriber()
        t.engine.set_quality_profile.side_effect = lambda p: order.append("profile")
        t.engine.transcribe.side_effect = lambda *a, **k: (
            order.append("transcribe") or {"text": "ок"}
        )

        lock = mlx_lock()
        t.transcribe_preview(audio_data=object())

        self.assertEqual(order, ["profile", "transcribe"])
        # Лок отпущен после выхода — иначе следующий захват из этого же теста
        # висел бы вечно на чужом потоке.
        released = threading.Event()

        def _probe() -> None:
            if lock.acquire(timeout=2.0):
                try:
                    released.set()
                finally:
                    lock.release()

        probe = threading.Thread(target=_probe, daemon=True)
        probe.start()
        probe.join(timeout=5)
        self.assertTrue(released.is_set(), "mlx_lock не отпущен после transcribe_preview")


if __name__ == "__main__":
    unittest.main()
