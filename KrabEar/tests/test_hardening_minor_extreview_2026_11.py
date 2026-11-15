"""Внешнее ревью E1/E3 (2026-11-15), мелкие хвосты.

* ``_EMAIL_RE.match`` с ``$`` пропускал адрес с ``\\n`` на конце (``$`` матчит
  перед финальным переводом строки). Инъекции в SMTP нет, но валидатор должен
  говорить правду: нужен ``fullmatch``.
* ``_resolve_and_check_host``: пустой ответ ``getaddrinfo`` проходил как
  «безопасно» (цикл по пустому списку, затем ``return True``). Fail-closed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from backend import webhook_manager  # noqa: E402
from backend.email_sender import EmailSender  # noqa: E402


@pytest.mark.parametrize("addr", ["user@example.com\n", "user@example.com\r\n", "a@b.co\n"])
def test_email_with_trailing_newline_is_rejected(addr):
    sender = EmailSender(backend_name="mail_app")

    with pytest.raises(ValueError):
        sender.send(addr, "тема", "<p>x</p>")


def test_plain_email_passes_validation_stage():
    from backend.email_sender import _EMAIL_RE

    assert _EMAIL_RE.fullmatch("user@example.com")
    assert not _EMAIL_RE.fullmatch("user@example.com\n")


@pytest.mark.parametrize("strict", [True, False])
def test_empty_dns_answer_is_not_safe(strict):
    with mock.patch.object(webhook_manager.socket, "getaddrinfo", return_value=[]):
        safe, reason = webhook_manager._resolve_and_check_host("hook.example.com", strict=strict)

    assert safe is False
    assert reason
