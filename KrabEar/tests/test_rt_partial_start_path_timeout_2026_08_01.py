"""Остановка старого rt_partial НЕ должна блокировать старт новой записи.

Живой инцидент 2026-08-01: `start_recording` занимал 31-41 секунду вместо
миллисекунд, запись не останавливалась вовремя, аудио копилось между попытками
(замер: 3-секундная запись дала duration=133.4s; у владельца — 61.9s), а
`stop_recording` отдавал recorder_timeout. В логе:

    realtime_partial worker не завершился за 30.0 с
    RealtimePartialTranscriber не перезапущен: прежний worker ещё жив
    Переполнение аудиобуфера во время записи

Причина: `_handle_start_recording_locked` останавливает прежний
RealtimePartialTranscriber вызовом `stop()` БЕЗ аргумента, то есть с дефолтным
таймаутом 30 секунд, под `_rt_lock`, прямо на пути старта записи. Пока воркер
висит в STT-вызове, каждая новая диктовка ждёт эти 30 секунд впустую, и
аудиобуфер за это время переполняется.

Ожидание бесполезно ИМЕННО ЗДЕСЬ: если stop() возвращает False, код уже умеет
жить дальше — превью просто не перезапускается («прежний worker ещё жив»), а
запись идёт своим чередом. То есть результат 30-секундного ожидания ни на что
не влияет, кроме задержки.

🔴 Дефолт 30.0 НЕ трогаем: он намеренно поднят с 4.0 волной W1323 и закреплён
её AST-тестом (`test_realtime_partial_stop_timeout_W1323.py`) — он покрывает
STT-вызов при ЧЕСТНОЙ остановке в конце диктовки, где дождаться воркера нужно.
Меняется только call-site в пути старта: там бюджет обязан быть коротким,
симметрично соседу — preview-воркер в том же методе ждёт
IPC_PREVIEW_THREAD_TIMEOUT_SEC (1.5 с).
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SRC = PROJECT_ROOT / "backend" / "recording_core_service.py"
_START_METHOD = "_handle_start_recording_locked"
_MAX_BUDGET_SEC = 2.0


def _find_method(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _rt_partial_stop_calls(method: ast.FunctionDef) -> list[ast.Call]:
    """Вызовы вида `<...>._rt_partial.stop(...)` внутри метода."""
    calls = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "stop":
            continue
        owner = func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "_rt_partial":
            calls.append(node)
    return calls


class TestStartPathUsesShortStopBudget(unittest.TestCase):
    """На пути старта записи ожидание старого воркера обязано быть коротким."""

    def setUp(self) -> None:
        self._tree = ast.parse(_SRC.read_text())
        self._method = _find_method(self._tree, _START_METHOD)
        self.assertIsNotNone(self._method, f"метод {_START_METHOD} не найден")

    def test_stop_call_passes_explicit_timeout(self) -> None:
        calls = _rt_partial_stop_calls(self._method)
        self.assertTrue(calls, "вызов _rt_partial.stop() в пути старта не найден")
        for call in calls:
            has_timeout = bool(call.args) or any(
                kw.arg == "timeout_sec" for kw in call.keywords
            )
            self.assertTrue(
                has_timeout,
                "_rt_partial.stop() на пути старта вызван без таймаута — "
                "это дефолтные 30 с блокировки каждой диктовки",
            )

    def test_start_path_budget_constant_is_short(self) -> None:
        """Значение бюджета — короткое и вынесено в именованную константу."""
        from backend import recording_core_service as rcs

        budget = getattr(rcs, "RT_PARTIAL_START_STOP_TIMEOUT_SEC", None)
        self.assertIsNotNone(
            budget,
            "ожидается именованная константа RT_PARTIAL_START_STOP_TIMEOUT_SEC "
            "(конвенция соседа IPC_PREVIEW_THREAD_TIMEOUT_SEC)",
        )
        self.assertLessEqual(
            float(budget),
            _MAX_BUDGET_SEC,
            f"бюджет ожидания на пути старта должен быть <= {_MAX_BUDGET_SEC} с",
        )
        self.assertGreater(float(budget), 0.0, "нулевой бюджет обессмыслил бы join")


class TestDefaultStopTimeoutUntouched(unittest.TestCase):
    """Инвариант W1323 обязан выжить: дефолт stop() остаётся 30 секунд.

    Локальный дубль чужого AST-теста — намеренный: он ловит попытку «починить»
    инцидент правкой дефолта вместо call-site, прямо в этом наборе.
    """

    def test_realtime_partial_stop_default_is_still_30s(self) -> None:
        src = (PROJECT_ROOT / "backend" / "realtime_partial.py").read_text()
        tree = ast.parse(src)
        stop = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "RealtimePartialTranscriber":
                stop = _find_method(node, "stop")
                break
        self.assertIsNotNone(stop, "RealtimePartialTranscriber.stop не найден")
        defaults = [
            d.value for d in stop.args.defaults
            if isinstance(d, ast.Constant) and isinstance(d.value, (int, float))
        ]
        self.assertIn(
            30.0,
            [float(d) for d in defaults],
            "дефолт stop() перестал быть 30.0 — это откат инварианта W1323",
        )


if __name__ == "__main__":
    unittest.main()
