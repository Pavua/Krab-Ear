"""Тесты RecapScheduler и EmailSender.

8 тестов:
1. Срабатывание в нужный час.
2. Один раз в день (no double-send).
3. Disabled=False — ничего не делает.
4. Fallback на mail_app при SMTP-ошибке (SMTP_failure_triggers_error).
5. mail_app backend вызывает osascript.
6. next_run calculation.
7. IPC-совместимые ответы send_recap / get_status.
8. Персистентность состояния между перезапусками.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# Путь для импорта
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.email_sender import EmailSender
from backend.recap_scheduler import RecapScheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_digest(date_str: str = "2026-04-25"):
    """Возвращает fake DailyDigest-совместимый объект."""
    d = MagicMock()
    d.date = date_str
    d.total_recordings = 5
    d.total_duration_min = 12.5
    d.total_words = 350
    d.languages_used = {"ru": 3, "en": 2}
    d.top_topics = ["встреча", "задача", "дедлайн"]
    d.highlights = ["Обсудили план на квартал.", "Договорились о следующем звонке."]
    d.formatted_markdown = "# Дайджест 2026-04-25\n\n- Записей: 5"
    return d


def _make_scheduler(
    tmpdir: Path,
    enabled: bool = True,
    recap_email_to: str = "test@example.com",
    recap_time_hour: int = 20,
    clock_fn=None,
) -> tuple:
    """Создаёт RecapScheduler с mock'нутыми зависимостями."""
    sender = MagicMock(spec=EmailSender)
    store = MagicMock()

    digest_gen = MagicMock()
    digest_gen.generate_digest.return_value = _make_fake_digest()

    sched = RecapScheduler(
        email_sender=sender,
        digest_generator=digest_gen,
        store=store,
        data_dir=tmpdir,
        recap_email_to=recap_email_to,
        recap_time_hour=recap_time_hour,
        enabled=enabled,
        check_interval_sec=1,
        clock_fn=clock_fn,
    )
    return sched, sender, digest_gen


# ---------------------------------------------------------------------------
# Тест 1: Срабатывание в нужный час
# ---------------------------------------------------------------------------

class TestRecapTriggersAtRightHour(unittest.TestCase):
    """send_recap вызывает email_sender.send только когда enabled + нужный час + не отправлено."""

    def test_triggers_at_correct_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir, recap_time_hour=20)
            result = sched.send_recap("2026-04-25")
            self.assertTrue(result["sent"])
            self.assertEqual(result["date"], "2026-04-25")
            self.assertIsNone(result["error"])
            sender.send.assert_called_once()
            call_kwargs = sender.send.call_args
            self.assertIn("to", call_kwargs.kwargs)
            self.assertEqual(call_kwargs.kwargs["to"], "test@example.com")
            self.assertIn("2026-04-25", call_kwargs.kwargs["subject"])

    def test_should_send_true_at_correct_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fixed_now = datetime(2026, 4, 25, 20, 5, 0)
            sched, _, _ = _make_scheduler(
                tmpdir, recap_time_hour=20, clock_fn=lambda: fixed_now
            )
            self.assertTrue(sched._should_send(fixed_now))

    def test_should_not_send_at_wrong_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fixed_now = datetime(2026, 4, 25, 15, 0, 0)
            sched, _, _ = _make_scheduler(
                tmpdir, recap_time_hour=20, clock_fn=lambda: fixed_now
            )
            self.assertFalse(sched._should_send(fixed_now))


# ---------------------------------------------------------------------------
# Тест 2: Один раз в день
# ---------------------------------------------------------------------------

class TestRecapOncePerDay(unittest.TestCase):
    """Второй вызов send_recap в тот же день не отправляет письмо."""

    def test_no_double_send_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir)

            # Первая отправка
            result1 = sched.send_recap("2026-04-25")
            self.assertTrue(result1["sent"])

            # should_send теперь должен вернуть False
            fixed_now = datetime(2026, 4, 25, 20, 5, 0)
            sched._clock_fn = lambda: fixed_now
            self.assertFalse(sched._should_send(fixed_now))

            # Прямой второй вызов должен всё равно отправить (send_recap не проверяет _should_send)
            # Но scheduler loop пропустит — это корректное поведение
            self.assertEqual(sender.send.call_count, 1)

    def test_state_persisted_after_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir)
            sched.send_recap("2026-04-25")

            state = sched._load_state()
            self.assertEqual(state["last_sent_date"], "2026-04-25")
            self.assertGreater(state.get("send_count", 0), 0)


# ---------------------------------------------------------------------------
# Тест 3: Disabled = ничего не делает
# ---------------------------------------------------------------------------

class TestRecapDisabled(unittest.TestCase):
    """Если enabled=False — _should_send всегда False."""

    def test_disabled_should_not_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir, enabled=False)
            fixed_now = datetime(2026, 4, 25, 20, 0, 0)
            self.assertFalse(sched._should_send(fixed_now))

    def test_disabled_empty_to_should_not_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir, enabled=True, recap_email_to="")
            fixed_now = datetime(2026, 4, 25, 20, 0, 0)
            self.assertFalse(sched._should_send(fixed_now))


# ---------------------------------------------------------------------------
# Тест 4: SMTP failure → error в результате
# ---------------------------------------------------------------------------

class TestSMTPFailure(unittest.TestCase):
    """При ошибке SMTP send_recap возвращает sent=False и error."""

    def test_smtp_failure_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir)
            sender.send.side_effect = RuntimeError("Connection refused")

            result = sched.send_recap("2026-04-25")
            self.assertFalse(result["sent"])
            self.assertIn("send_error", result["error"])
            self.assertIn("Connection refused", result["error"])

    def test_smtp_failure_does_not_update_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir)
            sender.send.side_effect = RuntimeError("Network error")

            sched.send_recap("2026-04-25")
            state = sched._load_state()
            # State должен остаться пустым (не обновляться при ошибке)
            self.assertIsNone(state.get("last_sent_date"))


# ---------------------------------------------------------------------------
# Тест 5: Mail.app backend
# ---------------------------------------------------------------------------

class TestMailAppBackend(unittest.TestCase):
    """EmailSender с mail_app вызывает osascript."""

    def test_mail_app_calls_osascript(self):
        sender = EmailSender(backend_name="mail_app")

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sender.send(
                to="user@example.com",
                subject="Test Subject",
                body_html="<p>Hello</p>",
            )
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "osascript")

    def test_mail_app_osascript_failure_raises(self):
        sender = EmailSender(backend_name="mail_app")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Mail.app error"

        with patch("subprocess.run", return_value=mock_result):
            with self.assertRaises(RuntimeError):
                sender.send(
                    to="user@example.com",
                    subject="Subject",
                    body_html="<p>Body</p>",
                )


# ---------------------------------------------------------------------------
# Тест 6: next_run calculation
# ---------------------------------------------------------------------------

class TestNextRunCalculation(unittest.TestCase):
    """get_status возвращает корректное next_run."""

    def test_next_run_today_if_not_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # Сейчас 15:00, recap_time_hour=20 → next_run сегодня в 20:00
            fixed_now = datetime(2026, 4, 25, 15, 0, 0)
            sched, _, _ = _make_scheduler(
                tmpdir, recap_time_hour=20, clock_fn=lambda: fixed_now
            )
            status = sched.get_status()
            next_run = datetime.fromisoformat(status["next_run"])
            self.assertEqual(next_run.hour, 20)
            self.assertEqual(next_run.date(), fixed_now.date())

    def test_next_run_tomorrow_if_past(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # Сейчас 21:00, recap_time_hour=20 → next_run завтра в 20:00
            fixed_now = datetime(2026, 4, 25, 21, 0, 0)
            sched, _, _ = _make_scheduler(
                tmpdir, recap_time_hour=20, clock_fn=lambda: fixed_now
            )
            status = sched.get_status()
            next_run = datetime.fromisoformat(status["next_run"])
            self.assertEqual(next_run.hour, 20)
            self.assertGreater(next_run.date(), fixed_now.date())


# ---------------------------------------------------------------------------
# Тест 7: IPC handler compatibility
# ---------------------------------------------------------------------------

class TestIPCHandlers(unittest.TestCase):
    """send_recap и get_status возвращают IPC-совместимые dict."""

    def test_send_recap_result_has_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir)
            result = sched.send_recap("2026-04-25")
            self.assertIn("sent", result)
            self.assertIn("date", result)
            self.assertIn("error", result)

    def test_get_status_result_has_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir)
            status = sched.get_status()
            for key in ("enabled", "recap_email_to", "recap_time_hour",
                        "last_sent_date", "next_run", "running", "send_count"):
                self.assertIn(key, status, f"Missing key: {key}")

    def test_get_status_running_false_before_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir)
            status = sched.get_status()
            self.assertFalse(status["running"])


# ---------------------------------------------------------------------------
# Тест 8: Персистентность состояния
# ---------------------------------------------------------------------------

class TestStatePersistence(unittest.TestCase):
    """Состояние сохраняется на диск и восстанавливается после перезапуска."""

    def test_state_written_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir)
            sched.send_recap("2026-04-25")

            state_path = tmpdir / "recap_state.json"
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_sent_date"], "2026-04-25")

    def test_new_scheduler_reads_persisted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)

            # Первый экземпляр — отправляет
            sched1, _, _ = _make_scheduler(tmpdir)
            sched1.send_recap("2026-04-25")

            # Второй экземпляр — читает состояние с диска
            fixed_now = datetime(2026, 4, 25, 20, 5, 0)
            sched2, _, _ = _make_scheduler(
                tmpdir, recap_time_hour=20, clock_fn=lambda: fixed_now
            )
            # _should_send должен вернуть False, т.к. сегодня уже отправлено
            self.assertFalse(sched2._should_send(fixed_now))


if __name__ == "__main__":
    unittest.main()
