"""Regression tests: быстрый двойной profile switch (balanced→max→balanced) и
конкурентный доступ к MLX — воспроизводят crash 2026-04-19 21:34 (EXC_BAD_ACCESS
в libmlx.dylib при конкурентных вызовах mlx_whisper.transcribe).

PR #71 добавил core/mlx_lock.py (RLock) — все MLX вызовы идут через with mlx_lock().
Эти тесты гарантируют, что fix остаётся в силе при будущих рефакторингах.

Crash timeline из логов:
    21:34:23 Смена профиля STT: max -> balanced
    21:34:28 Смена профиля STT: balanced -> max
    21:34:29 CRASH (EXC_BAD_ACCESS in libmlx.dylib)
"""

from __future__ import annotations

import sys
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    """Создать AudioEngine с data-dir для избежания PermissionError."""
    from core.engine import AudioEngine
    from core.config import settings
    os.makedirs(str(settings.DATA_DIR), exist_ok=True)
    return AudioEngine()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class MlxLockModuleTestCase(unittest.TestCase):
    """Юнит-тесты самого модуля core/mlx_lock.py."""

    def test_mlx_lock_returns_rlock(self):
        """mlx_lock() должен возвращать threading.RLock-совместимый объект."""
        from core.mlx_lock import mlx_lock
        lock = mlx_lock()
        self.assertTrue(hasattr(lock, "acquire"), "должен быть lock-совместимым")
        self.assertTrue(hasattr(lock, "release"), "должен быть lock-совместимым")

    def test_mlx_lock_singleton(self):
        """Каждый вызов mlx_lock() возвращает тот же глобальный объект."""
        from core.mlx_lock import mlx_lock
        self.assertIs(mlx_lock(), mlx_lock(), "должен быть один глобальный lock")

    def test_mlx_lock_reentrant_no_deadlock(self):
        """RLock: вложенный with mlx_lock() в том же потоке не дедлочит."""
        from core.mlx_lock import mlx_lock
        completed = threading.Event()

        def nested():
            with mlx_lock():
                with mlx_lock():   # второй захват — RLock не блокирует
                    completed.set()

        t = threading.Thread(target=nested, daemon=True)
        t.start()
        t.join(timeout=2.0)
        self.assertTrue(completed.is_set(), "вложенный with mlx_lock() завис (deadlock?)")


class ProfileSwitchSerializationTestCase(unittest.TestCase):
    """set_quality_profile() — pure Python, не требует MLX lock."""

    def setUp(self):
        self.engine = _make_engine()

    def test_single_profile_switch_balanced_to_max(self):
        """Переключение balanced→max меняет quality_profile и current_model."""
        from core.config import settings
        self.engine.quality_profile = "balanced"
        changed = self.engine.set_quality_profile("max")
        self.assertTrue(changed)
        self.assertEqual(self.engine.quality_profile, "max")
        self.assertEqual(self.engine.current_model, settings.model_max_list[0])

    def test_single_profile_switch_max_to_balanced(self):
        """Переключение max→balanced меняет quality_profile и current_model."""
        from core.config import settings
        self.engine.quality_profile = "max"
        self.engine.current_model = settings.model_max_list[0]
        changed = self.engine.set_quality_profile("balanced")
        self.assertTrue(changed)
        self.assertEqual(self.engine.quality_profile, "balanced")
        self.assertEqual(self.engine.current_model, settings.MODEL_BALANCED)

    def test_noop_on_same_profile(self):
        """Повторный set_quality_profile с тем же значением возвращает False."""
        self.engine.quality_profile = "balanced"
        changed = self.engine.set_quality_profile("balanced")
        self.assertFalse(changed)

    def test_rapid_profile_switch_three_threads(self):
        """Быстрый profile switch из 3 потоков — воспроизводит crash timeline.

        balanced→max→balanced за <5 сек из разных потоков. Тест проверяет, что
        финальный профиль — одно из допустимых значений (нет UB/исключений).
        Profile switch — чистый Python без MLX, поэтому RLock здесь не нужен,
        но атомарность присвоения строк в CPython гарантирует корректность.
        """
        errors: list[Exception] = []

        def switch(profile: str, delay: float = 0.0):
            try:
                time.sleep(delay)
                self.engine.set_quality_profile(profile)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=switch, args=("balanced", 0.00), daemon=True),
            threading.Thread(target=switch, args=("max",     0.05), daemon=True),
            threading.Thread(target=switch, args=("balanced", 0.06), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        self.assertEqual(errors, [], f"исключения при rapid profile switch: {errors}")
        self.assertIn(
            self.engine.quality_profile,
            {"balanced", "max"},
            "quality_profile должен быть в допустимом диапазоне",
        )


class MlxLockSerializesTranscribeTestCase(unittest.TestCase):
    """Ключевые regression-тесты: mlx_lock сериализует _transcribe_model."""

    _MLX_CALL_DELAY = 0.1  # секунд — имитирует GPU работу

    def setUp(self):
        self.engine = _make_engine()

    def _make_slow_transcribe(self, call_log: list, delay: float = _MLX_CALL_DELAY):
        """Создать side_effect для mlx_whisper.transcribe, который логирует overlap."""
        active = threading.Event()

        def fake_transcribe(*args, **kwargs):
            if active.is_set():
                call_log.append("OVERLAP_DETECTED")
            active.set()
            time.sleep(delay)
            active.clear()
            return {"text": "ok", "segments": []}

        return fake_transcribe

    @patch("core.engine.mlx_whisper")
    def test_concurrent_transcribe_calls_serialize(self, mock_mlx):
        """Два конкурентных _transcribe_model вызова НЕ перекрываются.

        Это прямой тест того, что crash 2026-04-19 не воспроизводится.
        Если бы mlx_lock отсутствовал — оба потока вошли бы в mlx_whisper.transcribe
        одновременно, вызывая race condition в __hash_table<MTL::Resource*>.
        """
        overlap_log: list = []
        mock_mlx.transcribe.side_effect = self._make_slow_transcribe(overlap_log)

        import numpy as np
        audio = np.zeros(8000, dtype=np.float32)

        results: list = []
        errors: list = []

        def run_transcribe():
            try:
                r = self.engine._transcribe_model(audio, "mlx-community/whisper-small-mlx", "", None)
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run_transcribe, daemon=True)
        t2 = threading.Thread(target=run_transcribe, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(errors, [], f"исключения при конкурентном transcribe: {errors}")
        self.assertEqual(
            overlap_log, [],
            "OVERLAP_DETECTED: mlx_lock НЕ сериализует _transcribe_model (regression!)",
        )
        self.assertEqual(len(results), 2, "оба вызова должны завершиться")

    @patch("core.engine.mlx_whisper")
    def test_concurrent_transcribe_total_time_is_sequential(self, mock_mlx):
        """Время 2 конкурентных вызовов ≈ 2× delay (sequential), не ~1× (parallel).

        Доказывает, что lock действительно блокирует, а не проходит насквозь.
        """
        delay = 0.08
        mock_mlx.transcribe.side_effect = lambda *a, **kw: (
            time.sleep(delay) or {"text": "ok", "segments": []}
        )

        import numpy as np
        audio = np.zeros(8000, dtype=np.float32)

        barrier = threading.Barrier(2)
        times: list[float] = []
        errors: list = []

        def run():
            try:
                barrier.wait()  # запустить оба потока одновременно
                start = time.monotonic()
                self.engine._transcribe_model(audio, "mlx-community/whisper-small-mlx", "", None)
                times.append(time.monotonic() - start)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=run, daemon=True)
        t2 = threading.Thread(target=run, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(errors, [], f"исключения: {errors}")
        self.assertEqual(len(times), 2, "оба вызова должны завершиться")
        # Один поток ждёт другого — его elapsed ≥ delay (не ~0)
        # Проверяем, что хотя бы один поток ждал (т.е. не параллельно)
        self.assertGreaterEqual(
            max(times),
            delay * 1.5,
            f"Если бы вызовы шли параллельно, max(times) был бы ~{delay}s; "
            f"получили {max(times):.3f}s. Lock может не работать (regression!)",
        )

    @patch("core.engine.mlx_whisper")
    def test_transcribe_plus_profile_switch_no_exception(self, mock_mlx):
        """Thread A = transcribe, Thread B = set_quality_profile — нет исключений.

        Воспроизводит точный crash scenario: профиль меняется пока идёт inference.
        Profile switch — pure Python (нет MLX), поэтому может идти конкурентно с lock,
        но не должен вызывать исключений.
        """
        mock_mlx.transcribe.side_effect = lambda *a, **kw: (
            time.sleep(0.05) or {"text": "ok", "segments": []}
        )

        import numpy as np
        audio = np.zeros(8000, dtype=np.float32)

        errors: list = []
        results: list = []

        def do_transcribe():
            try:
                r = self.engine._transcribe_model(audio, "mlx-community/whisper-small-mlx", "", None)
                results.append(r)
            except Exception as exc:
                errors.append(("transcribe", exc))

        def do_switch():
            try:
                time.sleep(0.02)  # начать switch пока transcribe работает
                self.engine.set_quality_profile("max")
                time.sleep(0.02)
                self.engine.set_quality_profile("balanced")
            except Exception as exc:
                errors.append(("switch", exc))

        t_transcribe = threading.Thread(target=do_transcribe, daemon=True)
        t_switch = threading.Thread(target=do_switch, daemon=True)
        t_transcribe.start()
        t_switch.start()
        t_transcribe.join(timeout=3.0)
        t_switch.join(timeout=3.0)

        self.assertEqual(errors, [], f"исключения при concurrent transcribe+switch: {errors}")
        self.assertEqual(len(results), 1, "transcribe должен завершиться")

    @patch("core.engine.mlx_whisper")
    def test_non_mlx_config_changes_do_not_block_on_lock(self, mock_mlx):
        """set_quality_profile не захватывает mlx_lock — не блокирует конфигурацию.

        Это важно для latency: pure Python config changes должны проходить
        мгновенно, независимо от длинного MLX inference в другом потоке.
        """
        from core.mlx_lock import mlx_lock

        # Захватить MLX lock из другого потока (имитируем активный inference)
        lock_held = threading.Event()
        lock_released = threading.Event()

        def hold_lock():
            with mlx_lock():
                lock_held.set()
                lock_released.wait(timeout=1.0)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        lock_held.wait(timeout=1.0)  # дождаться, пока lock занят

        # set_quality_profile не должен блокироваться на mlx_lock
        start = time.monotonic()
        self.engine.set_quality_profile("max")
        elapsed = time.monotonic() - start

        lock_released.set()
        holder.join(timeout=1.0)

        self.assertLess(
            elapsed,
            0.1,
            f"set_quality_profile заблокировался на mlx_lock ({elapsed:.3f}s)! "
            "Profile switch должен быть мгновенным (pure Python, без MLX lock).",
        )
        self.assertEqual(self.engine.quality_profile, "max")


if __name__ == "__main__":
    unittest.main()
