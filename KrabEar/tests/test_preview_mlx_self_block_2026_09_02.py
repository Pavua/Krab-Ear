"""Превью не удерживает mlx_lock во время transcribe (самоблокировка через пул).

ЖИВОЙ ИНЦИДЕНТ 02.09.2026
--------------------------
Диктовки владельца шли по 90 секунд на 30 секунд речи и падали с «Все
доступные STT-движки вышли из строя». В логе на КАЖДОЙ итерации превью:

    GigaAM-MLX: mlx_lock занят дольше 25.0с — уступаю очередь
    Realtime preview worker не завершился за 1.5 с

Цепочка самоблокировки:
  1. `transcribe_preview` в потоке A берёт `mlx_lock` (проба занятости GPU);
  2. зовёт `engine.transcribe(...)`;
  3. движок отправляет адаптер в `ThreadPoolExecutor` — поток B
     (`engine.py`: `_pool.submit(adapter_fn)`);
  4. адаптер в потоке B берёт ТОТ ЖЕ `mlx_lock`
     (`stt_gigaam_mlx.py`) — и ждёт поток A.

🔴 `RLock` реентерабелен ТОЛЬКО для своего потока. Передача работы в пул
ломает реентерабельность: вспомогательная функция блокировала ту самую
работу, ради которой бралась.

Отпускать лок безопасно: каждый downstream-путь берёт его сам — и
GigaAM-адаптер, и whisper (`_transcribe_model`). Инвариант «любой
MLX-инференс под локом» сохраняется; снимается лишь дублирующий внешний
захват.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class PreviewDoesNotHoldLockDuringTranscribeTests(unittest.TestCase):
    def _make_transcriber(self, engine):
        from backend.transcriber import Transcriber
        t = Transcriber.__new__(Transcriber)
        t.engine = engine
        return t

    def test_lock_is_free_while_engine_transcribes(self) -> None:
        """Главный регресс: во время transcribe() лок обязан быть СВОБОДЕН.

        Проверяем так же, как ломалось в проде — из ДРУГОГО потока, потому что
        для своего RLock всегда реентерабелен и баг был бы невидим.
        """
        import core.mlx_lock as mlx_mod
        acquired_from_other_thread: list[bool] = []

        def _fake_transcribe(*_a, **_kw):
            # имитируем поток пула, в который движок отдаёт адаптер
            def _probe():
                lk = mlx_mod.mlx_lock()
                got = lk.acquire(timeout=2.0)
                acquired_from_other_thread.append(got)
                if got:
                    lk.release()
            th = threading.Thread(target=_probe)
            th.start()
            th.join(timeout=5.0)
            return {"text": "ок", "engine": "test"}

        engine = MagicMock()
        engine.transcribe.side_effect = _fake_transcribe
        engine.preview_needs_whisper_profile.return_value = False

        tr = self._make_transcriber(engine)
        tr.transcribe_preview(b"\x00" * 320)

        self.assertEqual(
            acquired_from_other_thread, [True],
            "поток пула не смог взять mlx_lock — превью удерживает его "
            "во время transcribe и блокирует собственный инференс",
        )

    def test_lock_released_even_when_engine_raises(self) -> None:
        """Сбой движка не должен оставить лок захваченным."""
        import core.mlx_lock as mlx_mod
        engine = MagicMock()
        engine.transcribe.side_effect = RuntimeError("boom")
        engine.preview_needs_whisper_profile.return_value = False

        tr = self._make_transcriber(engine)
        with self.assertRaises(RuntimeError):
            tr.transcribe_preview(b"\x00" * 320)

        lk = mlx_mod.mlx_lock()
        got = lk.acquire(timeout=2.0)
        if got:
            lk.release()
        self.assertTrue(got, "после исключения в движке лок остался захваченным")

    def test_busy_gpu_still_skips_iteration(self) -> None:
        """🔴 Проба занятости сохранена: занят GPU — итерация пропускается.

        Иначе фикс выродился бы в «превью всегда лезет на занятый GPU».
        """
        import core.mlx_lock as mlx_mod
        engine = MagicMock()
        tr = self._make_transcriber(engine)

        lk = mlx_mod.mlx_lock()
        holder_ready = threading.Event()
        release_now = threading.Event()

        def _hold():
            lk.acquire()
            holder_ready.set()
            release_now.wait(timeout=10.0)
            lk.release()

        th = threading.Thread(target=_hold)
        th.start()
        try:
            holder_ready.wait(timeout=5.0)
            res = tr.transcribe_preview(b"\x00" * 320)
            self.assertEqual(res.get("skipped"), "mlx_busy")
            engine.transcribe.assert_not_called()
        finally:
            release_now.set()
            th.join(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
