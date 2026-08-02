"""Контракт IPC-поллинга wake word (spec 2026-07-05-wake-word-openwakeword).

Агент поллит wake_word_status и триггерит разговор по росту last_detection.ts.
Здесь: состояние last_detection в адаптере, контракт status, сбросы start/stop,
privacy loop-guard, и source-контракт проводки settings_get в service.py
(до фикса гейт privacy в handle_wake_word_start был ДЕКОРАТИВНЫМ в проде —
адаптер конструировался без settings_get).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_polling_contract.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
BACKEND_DIR = _PROJECT_ROOT / "KrabEar"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


class _NoLoopAdapter(OpenWakeWordAdapter):
    """Адаптер с no-op слушателем: start() спавнит поток, который сразу выходит.

    Позволяет тестировать сбросы состояния в start()/stop() без sounddevice
    и без установленного openwakeword.
    """

    def _listen_loop(self, **kwargs):
        return


class TestLastDetectionState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_initially_none(self) -> None:
        status = self.adapter.handle_wake_word_status({})
        self.assertIn("last_detection", status)
        self.assertIsNone(status["last_detection"])

    def test_record_detection_appears_in_status(self) -> None:
        self.adapter._record_detection("hey_jarvis", 0.91)
        status = self.adapter.handle_wake_word_status({})
        det = status["last_detection"]
        self.assertIsNotNone(det)
        self.assertEqual(det["model"], "hey_jarvis")
        self.assertAlmostEqual(det["score"], 0.91, places=6)
        self.assertIsInstance(det["ts"], float)

    def test_ts_monotonically_increases(self) -> None:
        self.adapter._record_detection("hey_jarvis", 0.8)
        ts1 = self.adapter.handle_wake_word_status({})["last_detection"]["ts"]
        self.adapter._record_detection("hey_jarvis", 0.85)
        ts2 = self.adapter.handle_wake_word_status({})["last_detection"]["ts"]
        self.assertGreater(ts2, ts1)

    def test_status_keeps_existing_contract_keys(self) -> None:
        status = self.adapter.handle_wake_word_status({})
        for key in ("ok", "running", "active_model", "engine_available"):
            self.assertIn(key, status)


class TestStartStopReset(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def test_stop_clears_last_detection(self) -> None:
        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._record_detection("hey_jarvis", 0.9)
        adapter.stop()  # потока нет — early return, но состояние чистится
        self.assertIsNone(adapter.handle_wake_word_status({})["last_detection"])

    def test_start_clears_last_detection(self) -> None:
        adapter = _NoLoopAdapter(data_dir=self.tmp)
        adapter._oww_available = True  # обходим проверку установленности либы
        adapter._record_detection("stale", 0.7)
        with patch.object(adapter, "_load_model", return_value=MagicMock()):
            adapter.start("hey_jarvis", on_detected=lambda n, s: None)
        try:
            self.assertIsNone(
                adapter.handle_wake_word_status({})["last_detection"]
            )
        finally:
            adapter.stop()


class TestPrivacyLoopGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def test_blocked_when_privacy_on(self) -> None:
        adapter = OpenWakeWordAdapter(
            data_dir=self.tmp, settings_get=lambda k, d: True
        )
        self.assertTrue(adapter._privacy_blocked())

    def test_not_blocked_when_privacy_off(self) -> None:
        adapter = OpenWakeWordAdapter(
            data_dir=self.tmp, settings_get=lambda k, d: False
        )
        self.assertFalse(adapter._privacy_blocked())

    def test_settings_exception_fails_open_to_false(self) -> None:
        def _boom(k, d):
            raise RuntimeError("settings unavailable")

        adapter = OpenWakeWordAdapter(data_dir=self.tmp, settings_get=_boom)
        self.assertFalse(adapter._privacy_blocked())


class TestServiceWiringSourceContract(unittest.TestCase):
    """Гейт privacy в handle_wake_word_start работает ТОЛЬКО если service.py
    пробросил settings_get. До фикса конструкция была декоративной."""

    def test_service_passes_runtime_settings_get(self) -> None:
        """Проводка settings_get — по AST-вызову, не по точному тексту.

        2026-08-02: старый точный regex покраснел от честного расширения
        конструкции (F6 добавил kwarg is_recording — свой AST-контракт в
        test_wake_word_recording_gate_2026_08_01.py). Намерение теста —
        «settings_get=self._get_runtime_setting присутствует в вызове» —
        сохранено, но проверяется теперь конструкцией, а не подстрокой,
        чтобы следующий честный kwarg его снова не красил.
        """
        import ast

        src = (BACKEND_DIR / "backend" / "service.py").read_text(encoding="utf-8")
        found = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "OpenWakeWordAdapter":
                continue
            kwargs = {
                kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg
            }
            found.append(kwargs)
        self.assertTrue(found, "конструкция OpenWakeWordAdapter не найдена в service.py")
        for kwargs in found:
            self.assertEqual(
                kwargs.get("settings_get"), "self._get_runtime_setting",
                "адаптер сконструирован без settings_get=self._get_runtime_setting "
                "— privacy-гейт в handle_wake_word_start снова декоративен",
            )


if __name__ == "__main__":
    unittest.main()
