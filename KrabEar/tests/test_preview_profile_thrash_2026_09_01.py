"""Превью не выбрасывает whisper-модель из кэша, когда она ему не нужна.

ЖИВОЙ ИНЦИДЕНТ 01.09.2026
--------------------------
Все три диктовки владельца подряд отработали за 86–92 секунды на 31–34
секунды речи и закончились «Критическая ошибка распознавания: Все доступные
STT-движки вышли из строя». Текст в буфер попадал из накопителя превью —
отсюда жалоба «долго и качество хуже».

Цепочка (лог backend):
  1. превью зовёт ``set_quality_profile("balanced")`` → ``mx.clear_cache()``
     выбрасывает загруженную whisper-модель;
  2. финальная транскрипция при ``quality_profile=max`` грузит
     whisper-large-v3 (~3 ГБ) заново;
  3. под нагрузкой загрузка+инференс не укладываются в бюджет
     ``stt_timeout_interactive_factor=3.0`` × длительность (93с на 31с речи);
  4. каскад падает целиком;
  5. 🔴 самоподдержание: пока идёт 90-секундная финальная транскрипция,
     превью СЛЕДУЮЩЕЙ диктовки голодает на том же ``mlx_lock`` 25с и тоже
     сдаётся.

Превью идёт ``single_pass=True`` — фоллбэк-цепочки у него нет. Значит
whisper-профиль важен ему ровно тогда, когда whisper окажется ПЕРВЫМ
движком; для русского первым идёт GigaAM.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.transcriber import _preview_needs_whisper_profile  # noqa: E402


class PreviewProfileGateTests(unittest.TestCase):
    """Предикат решает, платить ли чисткой кэша."""

    def test_skips_profile_when_gigaam_will_handle_preview(self) -> None:
        """GigaAM доступен ⇒ whisper превью не нужен ⇒ кэш не трогаем."""
        engine = MagicMock()
        engine.preview_needs_whisper_profile.return_value = False
        self.assertFalse(_preview_needs_whisper_profile(engine))

    def test_keeps_profile_when_whisper_would_be_first(self) -> None:
        """GigaAM недоступен ⇒ превью пойдёт в whisper ⇒ лёгкий профиль нужен."""
        engine = MagicMock()
        engine.preview_needs_whisper_profile.return_value = True
        self.assertTrue(_preview_needs_whisper_profile(engine))

    def test_fake_engine_without_predicate_keeps_old_behaviour(self) -> None:
        """🔴 Fake-движок из старых тестов предиката не имеет — поведение прежнее.

        Иначе фикс молча поменял бы сценарии чужих тестов.
        """
        class _FakeEngine:
            pass

        self.assertTrue(_preview_needs_whisper_profile(_FakeEngine()))

    def test_predicate_raising_falls_back_to_old_behaviour(self) -> None:
        """Сбой предиката не должен ломать превью — консервативный дефолт."""
        engine = MagicMock()
        engine.preview_needs_whisper_profile.side_effect = RuntimeError("boom")
        self.assertTrue(_preview_needs_whisper_profile(engine))


class EnginePredicateTests(unittest.TestCase):
    """Предикат на реальном AudioEngine, без загрузки моделей."""

    def _engine(self):
        from core.engine import AudioEngine
        return AudioEngine.__new__(AudioEngine)  # без __init__: модели не грузим

    def test_gigaam_available_means_profile_not_needed(self) -> None:
        eng = self._engine()
        eng._is_model_unavailable = lambda _m: False
        eng._skip_gigaam = False
        with unittest.mock.patch("core.engine.settings") as st:
            st.STT_GIGAAM_ENABLED = True
            self.assertFalse(eng.preview_needs_whisper_profile())

    def test_gigaam_marked_unavailable_means_profile_needed(self) -> None:
        eng = self._engine()
        eng._is_model_unavailable = lambda _m: True
        eng._skip_gigaam = False
        with unittest.mock.patch("core.engine.settings") as st:
            st.STT_GIGAAM_ENABLED = True
            self.assertTrue(eng.preview_needs_whisper_profile())

    def test_rest_engine_guard_means_profile_needed(self) -> None:
        """REST-движок гейтит GigaAM (_skip_gigaam) ⇒ превью нужен whisper."""
        eng = self._engine()
        eng._is_model_unavailable = lambda _m: False
        eng._skip_gigaam = True
        with unittest.mock.patch("core.engine.settings") as st:
            st.STT_GIGAAM_ENABLED = True
            self.assertTrue(eng.preview_needs_whisper_profile())


if __name__ == "__main__":
    unittest.main()


class ClearCacheOnlyOnRealModelChangeTests(unittest.TestCase):
    """🔴 Вторая половина инцидента: у владельца ОБА профиля указывали на одну
    и ту же модель (whisper-large-v3-mlx), поэтому «смена профиля» balanced↔max
    меняла лишь строку, но всё равно делала mx.clear_cache() — выбрасывала
    модель, которая тут же снова требовалась. Дважды на диктовку.
    """

    def _engine(self):
        from core.engine import AudioEngine
        eng = AudioEngine.__new__(AudioEngine)
        eng.quality_profile = "balanced"
        eng.current_model = "same/model"
        return eng

    def test_no_cache_flush_when_model_is_identical(self) -> None:
        """Модель та же — кэш не трогаем (иначе платим перезагрузкой)."""
        import core.engine as eng_mod
        eng = self._engine()
        calls = []
        with unittest.mock.patch.object(eng_mod, "mlx_lock",
                                        lambda: calls.append("lock") or MagicMock()):
            with unittest.mock.patch.object(eng_mod, "settings") as st:
                st.MODEL_BALANCED = "same/model"
                st.model_max_list = ["same/model"]
                changed = eng.set_quality_profile("max")
        self.assertTrue(changed, "профиль обязан смениться")
        self.assertEqual(eng.quality_profile, "max")
        self.assertEqual(calls, [], "при неизменной модели mlx_lock для flush не берётся")

    def test_cache_flush_happens_on_real_model_change(self) -> None:
        """Модель ДЕЙСТВИТЕЛЬНО другая — flush нужен, чтобы освободить буферы."""
        import core.engine as eng_mod
        eng = self._engine()
        calls = []

        def _fake_lock():
            calls.append("lock")
            m = MagicMock()
            m.acquire.return_value = False  # не уходим в реальный mlx
            return m

        with unittest.mock.patch.object(eng_mod, "mlx_lock", _fake_lock):
            with unittest.mock.patch.object(eng_mod, "settings") as st:
                st.MODEL_BALANCED = "same/model"
                st.model_max_list = ["other/model"]
                eng.set_quality_profile("max")
        self.assertEqual(calls, ["lock"], "при смене модели flush обязан запрашивать лок")
