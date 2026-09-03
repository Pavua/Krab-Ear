"""Волна «убийца воркера обязан успевать раньше сдающегося» (2026-09-03).

Живой инцидент: ``ai.krab.ear.rest`` перезапускался 14 раз за сутки. Улика,
отвергающая «просто медленно»: аудио длиной **0.5 секунды** висело ровно 25
секунд. Компьютерной работы там нет — клип стоял за чужим замком.

Корень — два независимых предела на одну операцию, и реально прекратить работу
умеет только МЕДЛЕННЫЙ из них:

* внешний (``stt_budget.resolve_attempt_timeout_sec``, в проде 25 с) при
  срабатывании делает ``executor.shutdown(wait=False, cancel_futures=True)``,
  что на УЖЕ ЗАПУЩЕННУЮ задачу не действует вовсе — поток остаётся жив;
* внутренний (``settings.MLX_TRANSCRIBE_TIMEOUT_SEC``, 120 с) убивает
  subprocess-воркер через ``threading.Timer`` — и только он освобождает
  ``MLXWhisperSession._lock``, удерживаемый весь ``readline()``.

Брошенный поток держит замок сессии ещё до 95 секунд, и КАЖДЫЙ следующий
запрос встаёт за ним в очередь, ловит свой внешний таймаут и добавляет ещё
один брошенный поток. Каскад заканчивался fail-fast'ом всего REST-процесса.

🔴 Критерий здесь СТРУКТУРНЫЙ, а не по wallclock: проверяем не «сколько секунд
прошло», а «какое число уехало воркеру». Замер времени дал бы флейк на
загруженной машине, а измеряемая величина здесь — именно согласование двух
порогов, а не длительность.

Спека: docs/superpowers/specs/2026-09-03-mlx-worker-timeout-handoff-design.md
Предшественники того же класса: #1958 (GigaAM-MLX), W1358 (MLXWatchdog).
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.engine import AudioEngine  # noqa: E402


class _WorkerTimeoutCapture:
    """Перехватывает timeout_sec, уехавший в subprocess-воркер."""

    def __init__(self):
        self.timeout_sec = None

    def __call__(self, audio_data, mlx_params, timeout_sec, model_name):
        self.timeout_sec = timeout_sec
        return {"text": "готово", "segments": []}


def _engine_with_worker_path():
    """Движок без __init__ (модели не грузим) на пути subprocess-воркера."""
    engine = AudioEngine.__new__(AudioEngine)
    engine._error_bus = None
    engine._unavailable_models = {}
    return engine


class WorkerTimeoutFitsAttemptBudgetTests(unittest.TestCase):
    """Внутренний предел обязан быть короче внешнего, иначе убийца опаздывает."""

    def _run(self, attempt_timeout_sec, mlx_setting=120.0):
        capture = _WorkerTimeoutCapture()
        with mock.patch(
            "core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True
        ), mock.patch(
            "core.mlx_whisper_session.transcribe_via_mlx_worker", capture
        ), mock.patch.object(
            AudioEngine, "_push_error", lambda *a, **k: None
        ):
            from core.config import settings

            with mock.patch.object(
                settings, "MLX_TRANSCRIBE_TIMEOUT_SEC", mlx_setting, create=True
            ):
                engine = _engine_with_worker_path()
                engine._transcribe_model(
                    "audio.wav",
                    "mlx-community/whisper-large-v3-turbo",
                    "",
                    "ru",
                    attempt_timeout_sec=attempt_timeout_sec,
                )
        return capture.timeout_sec

    def test_worker_timeout_strictly_below_attempt_budget(self):
        """25с бюджета → воркер обязан получить СТРОГО меньше 25с.

        Равенство недопустимо: при равных порогах порядок срабатывания
        не определён, и внешний «сдающийся» может опередить убийцу — ровно
        тот дефект, который чиним.
        """
        got = self._run(attempt_timeout_sec=25.0)
        self.assertIsNotNone(got, "_transcribe_model не дошёл до воркера")
        self.assertLess(
            got, 25.0,
            "воркер получил таймаут не короче внешнего предела — при зависании "
            "внешний бросит поток раньше, чем убийца успеет освободить "
            "MLXWhisperSession._lock",
        )
        self.assertGreater(got, 0.0, "неположительный таймаут = мгновенная смерть воркера")

    def test_batch_budget_does_not_stretch_beyond_setting(self):
        """Пакетный бюджет в сотни секунд не удлиняет таймаут сверх настройки."""
        got = self._run(attempt_timeout_sec=3600.0, mlx_setting=120.0)
        self.assertLessEqual(
            got, 120.0,
            "щедрый пакетный бюджет не должен поднимать таймаут воркера выше "
            "MLX_TRANSCRIBE_TIMEOUT_SEC — это ослабило бы защиту, а не усилило",
        )

    def test_no_budget_keeps_previous_behaviour(self):
        """Прямые вызовы без бюджета (перепрогон, ru_finetune) не меняются."""
        got = self._run(attempt_timeout_sec=None, mlx_setting=120.0)
        self.assertEqual(
            got, 120.0,
            "без переданного бюджета обязано остаться прежнее значение настройки",
        )

    def test_tiny_budget_stays_usable(self):
        """Крошечный остаток бюджета не превращается в ноль или отрицательное.

        Мгновенный таймаут неотличим от настоящего зависания и убил бы
        здоровый воркер на ровном месте.
        """
        got = self._run(attempt_timeout_sec=1.0)
        self.assertGreater(got, 0.0)
        self.assertLess(got, 1.0)


class BothExecutorSitesPassBudgetTests(unittest.TestCase):
    """Оба места с executor обязаны передавать в _transcribe_model тот же
    бюджет, что ставят на future.result.

    🔴 Урок #1958 буквально об этом: «одного входа в секцию мало — проверь ВСЕ».
    Прошлый фикс закрыл один вход, и инцидент повторился через сутки, пройдя
    мимо второго. Критерий по AST, а не по подстроке: честный комментарий про
    таймаут не должен красить корректную реализацию.
    """

    @staticmethod
    def _executor_sites():
        """Находит вызовы future.result(timeout=...) рядом с _transcribe_model."""
        path = os.path.join(PROJECT_ROOT, "core", "engine.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        sites = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # executor.submit(self._transcribe_model, ...)
            if not (isinstance(func, ast.Attribute) and func.attr == "submit"):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (
                isinstance(first, ast.Attribute)
                and first.attr == "_transcribe_model"
            ):
                continue
            sites.append(node)
        return sites

    def test_both_sites_found(self):
        """Страховка от молчаливой слепоты: мест ровно два, как в спеке."""
        sites = self._executor_sites()
        self.assertEqual(
            len(sites), 2,
            "изменилось число мест, где _transcribe_model уходит в executor — "
            f"найдено {len(sites)}; проверь, что новое место тоже передаёт бюджет",
        )

    def test_every_site_passes_attempt_budget(self):
        """Каждый submit обязан нести attempt_timeout_sec."""
        for site in self._executor_sites():
            kwargs = {kw.arg for kw in site.keywords if kw.arg}
            self.assertIn(
                "attempt_timeout_sec", kwargs,
                f"submit(_transcribe_model) на строке {site.lineno} не передаёт "
                "attempt_timeout_sec — внутренний предел там снова разъедется "
                "с внешним, и брошенный поток унесёт замок сессии",
            )


if __name__ == "__main__":
    unittest.main()
