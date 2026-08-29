"""Волна «ожидание mlx_lock ограничено» (2026-08-29).

Продолжение #1958. Тот фикс закрыл ПОТЕРЮ замка (watchdog отпускал его под
живым потоком). Но 2026-08-28 инцидент повторился дважды — 06:20 и 07:48 — и
оба раза прошёл МИМО фикса: в логе нет ни одной строки watchdog'а, хотя от
старта транскрипции до backstop прошло больше 120 с его порога.

Разбор по логам и коду:

    06:18:00  Смена профиля STT: balanced -> max (whisper-large-v3-mlx)
    06:18:02  GigaAM-RNNT добавлен в chain первым
              ← тишина, ни одного лога GigaAM
    06:20:57  handle_request завис дольше 180с (stop_recording)
    06:21:01  stt_gigaam adapter таймаут 200.0s — GPU stall?

Watchdog не сработал, потому что инференс НЕ НАЧИНАЛСЯ: поток стоял на входе
в критическую секцию. Метод исключения по пути `transcribe`:
`_get_model()` логирует загрузку (лога нет → модель была в памяти),
`mlx_inter_process_lock` ограничен 5 с, `_warmup` пропускается по `_warmed`.
Остаётся единственное неограниченное место — `with mlx_lock()` в `_infer_chunk`.

Держатель: превью. `RealtimePartialTranscriber` гоняет whisper через общий
`transcribe(is_preview=True)`, то есть под тем же `mlx_lock` и без ограничения;
в обоих эпизодах ему предшествует «Realtime preview worker не завершился за 1.5 с».

🔴 Это второй виток той же sibling-asymmetry: 2026-08-13 ровно этот сценарий
уже чинили — но только для очистки Metal-кэша в `set_quality_profile`
(`MLX_CACHE_FLUSH_LOCK_TIMEOUT_SEC`, комментарий там описывает инцидент
дословно). Вход GigaAM в секцию остался с вечным ожиданием.

Цена вечного ожидания: GigaAM съедает 200 с общего дедлайна, whisper получает
остаток и не укладывается в свои 92 с — «Критическая ошибка распознавания» на
здоровом стеке, диктовка потеряна.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

from core.mlx_lock import MLXLockTimeoutError, mlx_lock  # noqa: E402
from core.pipeline.stt_gigaam_mlx import GigaAMMLXAdapter  # noqa: E402


class _FakeGigaAMMLX:
    """Заглушка gigaam_mlx: инференс мгновенный, GPU не трогаем."""

    @staticmethod
    def transcribe(model, tokenizer, path):
        return "распознано"


def _adapter(**kw):
    a = GigaAMMLXAdapter(**kw)
    # Модель «уже загружена» — путь до критической секции без диска и сети.
    a._model, a._tokenizer = object(), object()
    a._warmed = True
    return a


class LockWaitIsBoundedTests(unittest.TestCase):
    """Ожидание mlx_lock не должно быть бесконечным."""

    def test_wait_gives_up_instead_of_blocking_forever(self):
        adapter = _adapter(lock_wait_timeout_sec=0.2)
        holder_started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def hold_lock():
            with mlx_lock():
                holder_started.set()
                release.wait(timeout=10.0)

        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        self.assertTrue(holder_started.wait(timeout=5.0), "держатель замка не стартовал")

        audio = np.zeros(1600, dtype=np.float32)
        with self.assertRaises(MLXLockTimeoutError):
            adapter._infer_chunk(_FakeGigaAMMLX, adapter._model, adapter._tokenizer, audio)

        release.set()
        t.join(timeout=5.0)

    def test_free_lock_path_still_works(self):
        """Свободный замок — обычный инференс, никаких отказов."""
        adapter = _adapter(lock_wait_timeout_sec=5.0)
        audio = np.zeros(1600, dtype=np.float32)
        out = adapter._infer_chunk(_FakeGigaAMMLX, adapter._model, adapter._tokenizer, audio)
        self.assertEqual(out, "распознано")

    def test_lock_released_after_successful_inference(self):
        """Замок обязан быть отпущен — иначе следующая диктовка встанет навсегда."""
        adapter = _adapter(lock_wait_timeout_sec=5.0)
        audio = np.zeros(1600, dtype=np.float32)
        adapter._infer_chunk(_FakeGigaAMMLX, adapter._model, adapter._tokenizer, audio)

        lock = mlx_lock()
        acquired = lock.acquire(timeout=1.0)
        if acquired:
            lock.release()
        self.assertTrue(acquired, "mlx_lock остался захваченным после успешного инференса")

    def test_lock_released_after_inference_error(self):
        """Ошибка инференса не должна оставлять замок захваченным."""
        adapter = _adapter(lock_wait_timeout_sec=5.0)

        class Boom:
            @staticmethod
            def transcribe(model, tokenizer, path):
                raise RuntimeError("инференс упал")

        audio = np.zeros(1600, dtype=np.float32)
        with self.assertRaises(RuntimeError):
            adapter._infer_chunk(Boom, adapter._model, adapter._tokenizer, audio)

        lock = mlx_lock()
        acquired = lock.acquire(timeout=1.0)
        if acquired:
            lock.release()
        self.assertTrue(acquired, "mlx_lock остался захваченным после ошибки инференса")

    def test_wait_timeout_is_not_the_inference_watchdog(self):
        """Ожидание и инференс — разные бюджеты.

        Иначе «ждать замок» и «считать на GPU» смешаются в один порог, и любая
        его правка поедет сразу по обоим смыслам.
        """
        adapter = _adapter(watchdog_timeout_sec=120.0, lock_wait_timeout_sec=30.0)
        self.assertNotEqual(adapter._watchdog_timeout_sec, adapter._lock_wait_timeout_sec)

    def test_timeout_error_is_distinct_from_inference_timeout(self):
        """🔴 Тип ошибки отличается от MLXTimeoutError намеренно.

        `engine.py` ловит MLXTimeoutError и помечает модель НЕДОСТУПНОЙ на 300 с.
        Ожидание в очереди за GPU — не отказ движка; блэклист за него и есть тот
        дефект, который разбирала спека #1956 («диктовка во время импорта часового
        файла истекает по ожиданию, ни разу не коснувшись GPU»).
        """
        from core.mlx_subprocess import MLXTimeoutError

        self.assertFalse(issubclass(MLXLockTimeoutError, MLXTimeoutError))


class BlacklistGateTests(unittest.TestCase):
    """Ожидание очереди за GPU не смеет метить движок недоступным."""

    def test_lock_wait_timeout_never_blacklists_the_engine(self):
        """🔴 Ядро урока #1956, теперь и для adapter-ветки.

        `_blacklist_allowed_for` пропускает TimeoutError в adapter-ветке к
        решению по остатку дедлайна — но MLXLockTimeoutError означает «GPU занят
        соседом», а не «движок сломан». Блэклист за очередь выбивает здоровый
        GigaAM на 300 с и отправляет следующую диктовку в облако, которого нет.
        """
        from core.engine import AudioEngine

        eng = AudioEngine.__new__(AudioEngine)  # без тяжёлого __init__
        exc = MLXLockTimeoutError("mlx_lock занят дольше 25.0с")

        self.assertFalse(
            eng._blacklist_allowed_for(exc, is_adapter=True),
            "ожидание mlx_lock заблэклистило здоровый движок",
        )
        self.assertFalse(
            eng._blacklist_allowed_for(exc, is_adapter=False),
            "ожидание mlx_lock заблэклистило здоровый движок (whisper-ветка)",
        )

    def test_real_engine_failure_still_blacklists(self):
        """Проверка обратной стороны: настоящий отказ движка блэклист заслуживает."""
        from core.engine import AudioEngine

        eng = AudioEngine.__new__(AudioEngine)
        self.assertTrue(
            eng._blacklist_allowed_for(RuntimeError("воркер умер"), is_adapter=True),
            "настоящий отказ движка перестал блэклиститься",
        )


class LockWaitDefaultsTests(unittest.TestCase):
    def test_default_wait_is_bounded_and_smaller_than_adapter_budget(self):
        """Дефолт обязан оставлять резервному движку рабочий остаток дедлайна.

        В инциденте GigaAM съел все 200 с бюджета адаптера, whisper получил
        остаток и не уложился в 92 с.
        """
        adapter = _adapter()
        self.assertGreater(adapter._lock_wait_timeout_sec, 0)
        self.assertLess(adapter._lock_wait_timeout_sec, 60.0)


if __name__ == "__main__":
    unittest.main()
