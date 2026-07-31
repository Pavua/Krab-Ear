"""S3/финальное ревью, Фикс 2: проба сторожа не принимает свой же 503 за здоровье.

Спека: docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md §Р6.

Найденный баг: ``_default_probe`` (rest_watchdog.py) считает провалом ТОЛЬКО
таймаут/ошибку соединения — любой HTTP-ответ (в т.ч. 429/5xx от общего
rate-limit-бакета) намеренно классифицируется как "жив" (п.2 контракта). Но
``rest_inprocess.py:_gate_or_count`` отдаёт 503 ``{"error": "shutting_down"}``
на ЛЮБОЙ путь, включая ``/health``, пока флаг ``shutting_down`` взведён.

Сценарий: ``restart()`` истёк по ``RESTART_STOP_DEADLINE_SEC`` и вернул
``False``, НЕ дождавшись своего ``stop()``. ``_shutting_down`` уже взведён (он
взводится ПЕРВЫМ действием stop()), старый сервер всё ещё в ``serve_forever``
и отвечает 503 ``shutting_down`` всем, включая ``/health``. Следующий тик:
``running == False`` (stop() уже успел обнулить ``self._server``/``self._thread``
под локом — это происходит МГНОВЕННО, задолго до фактического закрытия
сокета), проба по СТАРОЙ логике возвращает True (любой HTTP-ответ — "жив") →
сторож считает REST здоровым, выставляет ``port_held_externally=True`` и
сбрасывает окно анти-шторма. Итог: REST отдаёт 503 всем клиентам, сторож
больше НИКОГДА не лечит, диагностика лжёт "порт держит кто-то другой".

Фикс, две части:
1. ``_default_probe`` — 503 с телом ``{"error": "shutting_down"}`` (наш
   собственный детерминированный маркер) = НЕздоров. Прочие 5xx/429 —
   по-прежнему здоровье (регрессия на п.2 контракта — тесты ниже).
2. ``check_once`` — ``port_held_externally`` выставляется ТОЛЬКО когда
   ``status().get("ever_served") is False``: "порт занят снаружи" осмысленно
   лишь для сервера, который ни разу не слушал за жизнь процесса.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.rest_watchdog import RestWatchdog  # noqa: E402


class _FakeOwner:
    def __init__(self, *, running=True, enabled=True, tombstone=False,
                 ever_served=True, port=5005, restart_result=True):
        self.running = running
        self.enabled = enabled
        self.tombstone = tombstone
        self.ever_served = ever_served
        self.port = port
        self.restart_result = restart_result
        self.restart_calls = 0

    def status(self):
        return {
            "running": self.running,
            "enabled": self.enabled,
            "tombstone": self.tombstone,
            "ever_served": self.ever_served,
            "port": self.port,
        }

    def restart(self):
        self.restart_calls += 1
        return self.restart_result


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class DefaultProbeOwnShutdownMarkerTest(unittest.TestCase):
    """Часть 1: наш собственный 503 shutting_down != здоровье."""

    def test_default_probe_returns_false_for_own_shutting_down_marker(self):
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get") as m:
            m.return_value = mock.Mock(
                status_code=503, json=lambda: {"error": "shutting_down"},
            )
            self.assertFalse(wd._default_probe())

    def test_default_probe_still_returns_true_on_generic_503_without_marker_body(self):
        """Регрессия: обычный 503 БЕЗ нашего маркера — по-прежнему здоровье
        (п.2 контракта не отменяется, только сужается специальный случай)."""
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get") as m:
            m.return_value = mock.Mock(status_code=503)
            self.assertTrue(wd._default_probe())

    def test_default_probe_still_returns_true_on_429_response(self):
        """Регрессия п.2: rate-limit 429 не должен считаться маркером."""
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get") as m:
            m.return_value = mock.Mock(status_code=429)
            self.assertTrue(wd._default_probe())

    def test_default_probe_returns_true_when_503_body_is_not_json(self):
        """Реальный не-JSON 503 (не наш маркер, напр. от прокси/легаси) —
        .json() бросает, вероятность классифицируется как "жив" (fail-open к
        п.2, а не к новому спец-случаю)."""
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get") as m:
            resp = mock.Mock(status_code=503)
            resp.json.side_effect = ValueError("not json")
            m.return_value = resp
            self.assertTrue(wd._default_probe())

    def test_default_probe_returns_false_only_for_503_with_marker_not_other_codes(self):
        """Маркерное тело на НЕ-503 статусе (гипотетическая мутация) не
        должно триггерить — гейт специфичен именно к 503 rest_inprocess."""
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get") as m:
            m.return_value = mock.Mock(
                status_code=200, json=lambda: {"error": "shutting_down"},
            )
            self.assertTrue(wd._default_probe())


class PortHeldExternallyRequiresNeverServedTest(unittest.TestCase):
    """Часть 2: port_held_externally требует ever_served is False."""

    def test_healthy_probe_while_not_running_and_never_served_is_port_held_externally(self):
        """Настоящий «занятый порт» сценарий: наш сервер НИКОГДА не слушал
        (конфликт порта на старте), но проба здорова — легаси-юнит."""
        probe = mock.Mock(return_value=True)
        owner = _FakeOwner(running=False, ever_served=False)
        clock = _Clock()
        wd = RestWatchdog(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())
        self.assertTrue(wd.state()["port_held_externally"])

    def test_healthy_probe_while_not_running_but_ever_served_is_not_port_held_externally(self):
        """Регрессия основного бага: сервер УЖЕ поднимался (ever_served=True),
        сейчас формально not running (stop() уже обнулил _server под локом),
        но проба всё ещё здорова (наш же полу-мёртвый инстанс отвечает) —
        это НЕ «порт держит кто-то другой», а наш собственный процесс."""
        probe = mock.Mock(return_value=True)
        owner = _FakeOwner(running=False, ever_served=True)
        clock = _Clock()
        wd = RestWatchdog(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())
        self.assertFalse(
            wd.state()["port_held_externally"],
            "ever_served=True не должен давать ложный диагноз "
            "«порт занят снаружи» — владелец пошёл бы выгружать "
            "несуществующий юнит",
        )

    def test_running_true_with_healthy_probe_is_still_not_port_held_externally(self):
        probe = mock.Mock(return_value=True)
        owner = _FakeOwner(running=True, ever_served=True)
        wd = RestWatchdog(owner=owner, probe=probe)
        wd.check_once()
        self.assertFalse(wd.state()["port_held_externally"])


if __name__ == "__main__":
    unittest.main()
