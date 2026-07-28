"""Regression tests for fail-closed _privacy_getter in RecordingCoreService
(W1768 — MED privacy fail-open fix).

Контекст
--------
``RecordingCoreService.handle_start_recording`` собирает замыкание
``_privacy_getter`` и передаёт его в ``RealtimePartialTranscriber``.
Контракт getter: вернуть ``True`` ⇒ privacy ON ⇒ emit подавляется.

До исправления W1768 внутренний ``except`` getter'а возвращал ``False``
(fail-OPEN): если ``settings_svc.cached_settings()`` бросал исключение,
состояние приватности неизвестно, но getter рапортовал «privacy OFF» →
``RealtimePartialTranscriber`` эмитил частичный транскрипт. Это маскировало
исключение ещё до того, как оно могло достичь fail-closed ветки потребителя
(W1763), поэтому утечка происходила несмотря на defense-in-depth на уровне
emit.

После исправления внутренний ``except`` возвращает ``True`` (fail-CLOSED):
неизвестное состояние ⇒ считаем privacy ON ⇒ emit подавляется. Нормальный
путь по-прежнему возвращает реальный флаг.

Покрытие:
  - test_privacy_getter_returns_true_when_cached_settings_raises  (главный регресс)
  - test_privacy_getter_returns_real_flag_on_normal_path
  - test_privacy_getter_reflects_runtime_toggle_then_fails_closed_on_raise
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Path setup — нужен для запуска standalone из корня репо.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_KRAB_EAR = os.path.join(_PROJECT_ROOT, "KrabEar")
if _KRAB_EAR not in sys.path:
    sys.path.insert(0, _KRAB_EAR)


# ---------------------------------------------------------------------------
# Helpers (переиспользуем паттерн из test_realtime_partial_privacy_W1200.py)
# ---------------------------------------------------------------------------

def _make_transcriber():
    t = MagicMock()
    t.transcribe_preview.return_value = {"text": "не должно утечь"}
    return t


def _base_settings(privacy: bool = False) -> dict:
    return {
        "privacy_mode_enabled": privacy,
        "realtime_partial_enabled": True,
        "realtime_preview_enabled": False,
        "rt_partial_interval_sec": 0.05,
        "rt_partial_buffer_sec": 2.0,
        "quality_profile": "balanced",
    }


def _make_service(settings_side_effect):
    """Wire a RecordingCoreService whose settings_svc.cached_settings uses the
    provided ``side_effect`` (callable returning a dict, or raising)."""
    from backend.recording_core_service import RecordingCoreService

    settings_svc = MagicMock()
    settings_svc.cached_settings.side_effect = settings_side_effect

    recorder = MagicMock()
    recorder.sample_rate = 16000
    # R2: Core читает is_recording ДО старта. У голого MagicMock любой
    # неуказанный атрибут истинен, что читалось бы как «микрофон уже занят»
    # и уводило старт в unmanaged_recording. Реальный AudioRecorder держит
    # False до start() и True после (recorder.py) — повторяем это.
    recorder.is_recording = False

    def _start(*_args, **_kwargs):
        recorder.is_recording = True
        return True

    recorder.start = MagicMock(side_effect=_start)

    return RecordingCoreService(
        recorder=recorder,
        transcriber=_make_transcriber(),
        translator=MagicMock(),
        store=MagicMock(),
        vocabulary=MagicMock(),
        settings_svc=settings_svc,
        llm_rewriter=MagicMock(),
        auto_glossary=MagicMock(),
        semantic_searcher=MagicMock(),
        context_memory=MagicMock(),
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=MagicMock(),
        action_items_extractor=MagicMock(),
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )


def _capture_privacy_getter(svc):
    """Run handle_start_recording with RealtimePartialTranscriber patched and
    return the captured ``privacy_getter`` closure."""
    captured: list = []

    def _capture_constructor(**kwargs):
        captured.append(kwargs.get("privacy_getter"))
        inst = MagicMock()
        inst.start = MagicMock()
        return inst

    with patch("backend.recording_core_service.event_bus"), \
         patch("backend.recording_core_service.RealtimePartialTranscriber") as MockRPT:
        MockRPT.side_effect = _capture_constructor
        svc.handle_start_recording({})

    assert captured, "RealtimePartialTranscriber constructor should have been called"
    getter = captured[0]
    assert getter is not None, "privacy_getter must be passed to constructor"
    return getter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordingCorePrivacyGetterFailClosed(unittest.TestCase):
    """Проверяет fail-closed поведение _privacy_getter в RecordingCoreService."""

    def test_privacy_getter_returns_true_when_cached_settings_raises(self):
        """Когда cached_settings() бросает — getter возвращает True (privacy ON).

        Это основной регрессионный тест для W1768.
        Fail-before: except возвращал False → privacy трактуется OFF → emit (утечка).
        Pass-after:  except возвращает True  → privacy трактуется ON  → emit подавлен.

        Замыкание захватывается в момент start (settings тогда читаются успешно
        для построения guard), затем подменяем side_effect на raise — это
        воспроизводит сценарий, когда settings-сервис падает во время записи.
        """
        settings_state = {"mode": "ok"}

        def _settings():
            if settings_state["mode"] == "raise":
                raise RuntimeError("settings service unavailable")
            return _base_settings(privacy=False)

        svc = _make_service(_settings)
        getter = _capture_privacy_getter(svc)

        # Нормальный путь захвачен; теперь имитируем сбой settings-сервиса.
        settings_state["mode"] = "raise"

        self.assertTrue(
            getter(),
            "privacy_getter must FAIL CLOSED (return True) when cached_settings() "
            "raises — returning False is a fail-open privacy leak (W1768 regression)",
        )

    def test_privacy_getter_returns_real_flag_on_normal_path(self):
        """Нормальный путь не затронут: getter возвращает реальный флаг.

        Гарантирует, что fail-closed не подавляет штатную работу — при исправном
        cached_settings() getter отражает фактическое privacy_mode_enabled.
        """
        # privacy OFF → getter() == False (emit разрешён в штатном режиме)
        svc_off = _make_service(lambda: _base_settings(privacy=False))
        getter_off = _capture_privacy_getter(svc_off)
        self.assertFalse(
            getter_off(),
            "Normal path with privacy OFF must return False (do not wrongly suppress)",
        )

    def test_privacy_getter_reflects_runtime_toggle_then_fails_closed_on_raise(self):
        """Getter перечитывает settings каждый вызов: toggle отражается, raise ⇒ True.

        Итерация 1: privacy=False → False (emit разрешён).
        Итерация 2: privacy=True  → True  (emit подавлён по реальному флагу).
        Итерация 3: cached_settings() raise → True (fail-closed).
        """
        settings_dict = _base_settings(privacy=False)
        state = {"raise": False}

        def _settings():
            if state["raise"]:
                raise ValueError("transient settings failure")
            return dict(settings_dict)

        svc = _make_service(_settings)
        getter = _capture_privacy_getter(svc)

        # Реальный флаг OFF.
        self.assertFalse(getter(), "should reflect privacy OFF")

        # Toggle privacy ON в рантайме.
        settings_dict["privacy_mode_enabled"] = True
        self.assertTrue(getter(), "should reflect runtime toggle to privacy ON")

        # Сбой settings-сервиса ⇒ fail-closed.
        state["raise"] = True
        self.assertTrue(getter(), "should FAIL CLOSED (True) when settings read raises")


if __name__ == "__main__":
    unittest.main()
