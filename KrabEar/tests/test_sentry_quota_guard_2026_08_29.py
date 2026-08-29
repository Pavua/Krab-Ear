"""Волна «квота Sentry не выгорает от одной ошибки» (2026-08-29).

Живой факт, снятый через Sentry API: последнее ПРИНЯТОЕ событие организации —
13 августа. Дальше 16 суток полной слепоты: за неделю 789 событий отброшено по
лимиту, 387 клиентом, принято ноль. За 90 дней принято 10524, отброшено по
лимиту 6200.

Что выжгло квоту (issues, sort=freq):

    2488×  handle_request завис дольше 180с (method=stop_recording)
     821×  [cloudflared] Connection terminated
     310×  GeminiSTT exhausted model candidates: HTTP 429

🔴 Одна повторяющаяся ошибка съела квоту и ослепила мониторинг для ВСЕХ
проектов организации — включая те, где в это время были настоящие инциденты.

Почему существующий кап не помог: `ErrorBus` уже имеет часовой лимит
(SENTRY_HOURLY_CAP_PER_CODE=12, SENTRY_HOURLY_CAP_TOTAL=40), но лидер списка
идёт МИМО него — обычным `logger.error` в `ipc_server.py`, который забирает
LoggingIntegration сентри. Классическая sibling-asymmetry: путь научился, его
сосед — нет.

Поэтому предохранитель ставится в `before_send` — единственную точку, через
которую проходит КАЖДОЕ событие независимо от источника.
"""
from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import observability  # noqa: E402


def _log_event(message: str, logger_name: str = "KrabEar.Backend.Service") -> dict:
    """Событие в форме, которую отдаёт LoggingIntegration на logger.error()."""
    return {
        "level": "error",
        "logger": logger_name,
        "logentry": {"message": message, "params": []},
    }


def _exc_event(exc_type: str, value: str) -> dict:
    return {
        "level": "error",
        "exception": {"values": [{"type": exc_type, "value": value}]},
    }


class QuotaGuardTests(unittest.TestCase):
    def setUp(self):
        observability._reset_sentry_rate_limiter()
        self.addCleanup(observability._reset_sentry_rate_limiter)

    def test_repeated_identical_error_is_capped(self):
        """Ровно инцидент: 2488 повторов одной строки не должны уйти целиком."""
        msg = "handle_request завис дольше 180с (method=stop_recording)"
        sent = sum(
            1 for _ in range(200)
            if observability._sentry_before_send(_log_event(msg), None) is not None
        )
        self.assertGreater(sent, 0, "первое вхождение обязано доходить — иначе мы слепы")
        self.assertLessEqual(
            sent, observability.SENTRY_HOURLY_CAP_PER_SIGNATURE,
            f"за час ушло {sent} событий одной сигнатуры — квота выгорит снова",
        )

    def test_distinct_errors_are_not_suppressed_by_each_other(self):
        """Кап по сигнатуре, а не общий счётчик: разные ошибки не глушат друг друга."""
        for i in range(50):
            observability._sentry_before_send(_log_event(f"повтор одной ошибки {0}"), None)

        fresh = observability._sentry_before_send(
            _log_event("совершенно другая ошибка"), None
        )
        self.assertIsNotNone(
            fresh, "новая ошибка подавлена шумом соседней — это и есть слепота"
        )

    def test_message_with_varying_numbers_is_one_signature(self):
        """`завис дольше 180с` и `дольше 240с` — одна ошибка, не две.

        Иначе счётчик обнуляется на каждом новом числе и кап не работает: ровно
        так выглядит инцидентная строка, где меняются таймаут и имя метода.
        """
        sent = 0
        for i in range(120):
            ev = _log_event(f"handle_request завис дольше {180 + i}с (method=m{i})")
            if observability._sentry_before_send(ev, None) is not None:
                sent += 1
        self.assertLessEqual(
            sent, observability.SENTRY_HOURLY_CAP_PER_SIGNATURE,
            "числа в сообщении расщепили одну ошибку на много сигнатур",
        )

    def test_exception_events_are_capped_too(self):
        """Не только logger.error: исключения идут тем же путём."""
        sent = sum(
            1 for _ in range(100)
            if observability._sentry_before_send(
                _exc_event("RuntimeError", "GigaAM worker died"), None
            ) is not None
        )
        self.assertLessEqual(sent, observability.SENTRY_HOURLY_CAP_PER_SIGNATURE)

    def test_total_cap_protects_quota_from_many_distinct_signatures(self):
        """Защита и от «много разных ошибок разом» — иначе кап обходится числом видов."""
        sent = sum(
            1 for i in range(400)
            if observability._sentry_before_send(_log_event(f"ошибка вида {i}"), None)
            is not None
        )
        self.assertLessEqual(
            sent, observability.SENTRY_HOURLY_CAP_TOTAL,
            f"за час ушло {sent} событий — общий потолок не держит",
        )

    def test_suppressed_count_is_reported_not_silently_dropped(self):
        """Подавленное нельзя терять молча — иначе масштаб проблемы не виден.

        Событие, которым кап закрывается, помечается числом подавленных: в
        Sentry остаётся честный след «их было N», а не тишина.
        """
        msg = "повторяющаяся ошибка"
        last_sent = None
        for _ in range(60):
            ev = observability._sentry_before_send(_log_event(msg), None)
            if ev is not None:
                last_sent = ev
        self.assertIsNotNone(last_sent)
        tags = last_sent.get("tags") or {}
        self.assertIn(
            "suppressed_since_last", tags,
            "нет следа подавленных событий — масштаб проблемы теряется",
        )

    def test_pii_redaction_still_applied(self):
        """Кап не должен отменять существующую очистку PII (W1193 F4).

        🔴 Путь задан ЯВНО как /Users/..., а не через expanduser('~'): редакция
        сворачивает macOS-форму, а ubuntu-раннер живёт в /home/runner — тест на
        expanduser зелен локально и красен в CI, проверяя не то, что заявляет.
        """
        ev = observability._sentry_before_send(
            _log_event("путь /Users/someone/секрет.txt"), None
        )
        self.assertIsNotNone(ev)
        self.assertNotIn(
            "/Users/someone", ev["logentry"]["message"],
            "домашний путь утёк в Sentry — редакция PII сломана капом",
        )

    def test_unidentifiable_events_are_never_suppressed(self):
        """🔴 Направление отказа — в сторону видимости.

        Событие без сообщения и исключения (breadcrumb-only, служебный конверт)
        сигнатуры не имеет. Схлопывать такие в один ключ нельзя: они начнут
        глушить друг друга, и предохранитель от слепоты сам её создаст —
        именно так первая редакция этой волны уронила семь чужих тестов.
        """
        crumb_only = {"breadcrumbs": {"values": [{"message": "шаг"}]}}
        for _ in range(200):
            self.assertIsNotNone(
                observability._sentry_before_send(dict(crumb_only), None),
                "безымянное событие подавлено — предохранитель создаёт слепоту",
            )

    def test_transactions_are_not_rate_limited(self):
        """Транзакции живут на отдельной квоте и шумом ошибок не являются."""
        for _ in range(120):
            self.assertIsNotNone(
                observability._sentry_before_send(
                    {"type": "transaction", "transaction": "POST /v1/stt"}, None
                )
            )

    def test_guard_never_raises_on_malformed_event(self):
        """before_send обязан быть безотказным: телеметрия не ломает прод."""
        for bad in ({}, {"logentry": None}, {"exception": {"values": None}}):
            observability._sentry_before_send(bad, None)


if __name__ == "__main__":
    unittest.main()
