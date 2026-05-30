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
from typing import Optional
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


class TestWave138RecapScheduler(unittest.TestCase):
    """Wave 138 — дополнительные тесты по спецификации задачи."""

    # ------------------------------------------------------------------
    # test_schedule_starts_at_configured_hour
    # ------------------------------------------------------------------

    def test_schedule_starts_at_configured_hour(self):
        """_should_send возвращает True только в сконфигурированный час."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            for hour in (0, 8, 12, 20, 23):
                with self.subTest(recap_time_hour=hour):
                    fixed_now = datetime(2026, 5, 19, hour, 0, 0)
                    sched, _, _ = _make_scheduler(
                        tmpdir, recap_time_hour=hour, clock_fn=lambda dt=fixed_now: dt
                    )
                    self.assertTrue(sched._should_send(fixed_now))

    def test_schedule_does_not_trigger_outside_configured_hour(self):
        """_should_send возвращает False для любого другого часа."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            recap_hour = 20
            wrong_now = datetime(2026, 5, 19, 10, 0, 0)
            sched, _, _ = _make_scheduler(
                tmpdir, recap_time_hour=recap_hour, clock_fn=lambda: wrong_now
            )
            self.assertFalse(sched._should_send(wrong_now))

    # ------------------------------------------------------------------
    # test_send_recap_invokes_email
    # ------------------------------------------------------------------

    def test_send_recap_invokes_email(self):
        """send_recap вызывает email_sender.send с корректными аргументами."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            target_date = "2026-04-25"  # matches fake digest date
            sched, sender, digest_gen = _make_scheduler(
                tmpdir, recap_email_to="recipient@test.com"
            )
            result = sched.send_recap(target_date)

            self.assertTrue(result["sent"])
            sender.send.assert_called_once()
            kwargs = sender.send.call_args.kwargs
            self.assertEqual(kwargs["to"], "recipient@test.com")
            self.assertIn(target_date, kwargs["subject"])
            self.assertIn("body_html", kwargs)
            self.assertIn("body_text", kwargs)
            # HTML содержит дату дайджеста
            self.assertIn(target_date, kwargs["body_html"])

    def test_send_recap_calls_digest_generator(self):
        """send_recap вызывает digest_generator.generate_digest с правильной датой."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, digest_gen = _make_scheduler(tmpdir)
            sched.send_recap("2026-05-19")
            digest_gen.generate_digest.assert_called_once()
            call_kwargs = digest_gen.generate_digest.call_args.kwargs
            self.assertEqual(call_kwargs["date_str"], "2026-05-19")

    # ------------------------------------------------------------------
    # test_skip_if_no_recordings
    # ------------------------------------------------------------------

    def test_skip_if_no_recordings(self):
        """Дайджест с 0 записей всё равно отправляется, но email содержит пометку об отсутствии данных."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, digest_gen = _make_scheduler(tmpdir)

            # Настраиваем пустой дайджест
            empty_digest = MagicMock()
            empty_digest.date = "2026-05-19"
            empty_digest.total_recordings = 0
            empty_digest.total_duration_min = 0
            empty_digest.total_words = 0
            empty_digest.languages_used = {}
            empty_digest.top_topics = []
            empty_digest.highlights = []
            empty_digest.formatted_markdown = "# Дайджест 2026-05-19\n\nЗаписей нет."
            digest_gen.generate_digest.return_value = empty_digest

            result = sched.send_recap("2026-05-19")
            # Отправка происходит даже при 0 записях (empty_note будет в HTML)
            self.assertTrue(result["sent"])
            # HTML содержит пустую пометку
            html_body = sender.send.call_args.kwargs["body_html"]
            self.assertIn("не найдено", html_body)

    def test_skip_if_digest_generation_fails(self):
        """Если генерация дайджеста падает — sent=False, email не отправляется."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, digest_gen = _make_scheduler(tmpdir)
            digest_gen.generate_digest.side_effect = RuntimeError("Ошибка генерации")

            result = sched.send_recap("2026-05-19")
            self.assertFalse(result["sent"])
            self.assertIn("digest_error", result["error"])
            sender.send.assert_not_called()

    # ------------------------------------------------------------------
    # test_handles_email_failure_gracefully
    # ------------------------------------------------------------------

    def test_handles_email_failure_gracefully(self):
        """Ошибка отправки email не роняет backend — возвращается sent=False с деталями."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir)
            sender.send.side_effect = OSError("SMTP connection reset")

            result = sched.send_recap("2026-05-19")
            self.assertFalse(result["sent"])
            self.assertIsNotNone(result["error"])
            self.assertIn("send_error", result["error"])
            self.assertIn("SMTP connection reset", result["error"])

    def test_handles_email_failure_state_not_updated(self):
        """После ошибки email last_sent_date не обновляется — повтор возможен."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir)
            sender.send.side_effect = ConnectionRefusedError("refused")

            sched.send_recap("2026-05-19")
            state = sched._load_state()
            self.assertIsNone(state.get("last_sent_date"))

    # ------------------------------------------------------------------
    # test_concurrent_trigger_idempotent
    # ------------------------------------------------------------------

    def test_concurrent_trigger_idempotent(self):
        """Одновременный вызов send_recap из 5 потоков отправляет письмо ровно 1 раз.

        W922 H1: TOCTOU race — два конкурирующих вызова могли оба пройти
        _should_send до того, как один из них запишет last_sent_date, что
        приводило к двойной отправке. После фикса (tentative _sending_date
        маркер под lock) ровно один поток выигрывает гонку.
        """
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir)
            errors: list[Exception] = []
            results: list[dict] = []
            lock = threading.Lock()

            def trigger() -> None:
                try:
                    r = sched.send_recap("2026-05-19")
                    with lock:
                        results.append(r)
                except Exception as exc:
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=trigger) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            self.assertEqual(errors, [], f"Concurrent errors: {errors}")
            # Все вызовы вернули корректный dict
            self.assertEqual(len(results), 5)
            for r in results:
                self.assertIn("sent", r)
                self.assertIn("date", r)
                self.assertIn("error", r)

            # W922 H1 TOCTOU fix: ровно ОДНА отправка email (не 2-5)
            self.assertEqual(
                sender.send.call_count,
                1,
                f"Ожидалась 1 отправка, получено {sender.send.call_count} "
                f"(TOCTOU race не устранена)",
            )
            # Ровно один результат с sent=True
            sent_results = [r for r in results if r["sent"]]
            self.assertEqual(len(sent_results), 1, "Ожидался ровно 1 успешный результат")

    def test_start_is_idempotent(self):
        """Повторный вызов start() не создаёт дополнительных потоков."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, _, _ = _make_scheduler(tmpdir, enabled=True)
            sched.start()
            self.addCleanup(sched.stop)
            thread_before = sched._thread
            sched.start()
            thread_after = sched._thread
            self.assertIs(thread_before, thread_after)
            sched.stop()

    # ------------------------------------------------------------------
    # test_unicode_in_recap_body
    # ------------------------------------------------------------------

    def test_unicode_in_recap_body(self):
        """Тела письма корректно содержат кириллицу, emoji и спецсимволы."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, digest_gen = _make_scheduler(tmpdir)

            # Дайджест с юникод-содержимым
            uni_digest = MagicMock()
            uni_digest.date = "2026-05-19"
            uni_digest.total_recordings = 3
            uni_digest.total_duration_min = 7.5
            uni_digest.total_words = 180
            uni_digest.languages_used = {"ru": 2, "es": 1}
            uni_digest.top_topics = ["переговоры 🤝", "бюджет €1000", "сроки — дедлайн"]
            uni_digest.highlights = ["Обсудили план на квартал 📋.", "Договорились о встрече."]
            uni_digest.formatted_markdown = "# Дайджест\n\nПереговоры 🤝 бюджет €1000"
            digest_gen.generate_digest.return_value = uni_digest

            result = sched.send_recap("2026-05-19")
            self.assertTrue(result["sent"])

            html_body = sender.send.call_args.kwargs["body_html"]
            text_body = sender.send.call_args.kwargs["body_text"]

            # Кириллица сохранена в HTML
            self.assertIn("переговоры", html_body)
            self.assertIn("🤝", html_body)
            # Markdown body содержит спецсимволы
            self.assertIn("€1000", text_body)
            self.assertIn("🤝", text_body)

    def test_unicode_subject_line(self):
        """Тема письма корректно формируется с кириллицей."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sched, sender, _ = _make_scheduler(tmpdir)
            sched.send_recap("2026-05-19")
            subject = sender.send.call_args.kwargs["subject"]
            # Тема содержит кириллические символы
            self.assertIn("дайджест", subject)
            self.assertIn("2026-05-19", subject)


# ---------------------------------------------------------------------------
# Тест 9 (W933): runtime settings_provider — изменение часа без рестарта
# ---------------------------------------------------------------------------

class TestW933RuntimeSettingsProvider(unittest.TestCase):
    """W922 H2 fix — settings_provider перечитывается каждый тик.

    Проверяет, что после изменения recap_time_hour через settings_provider
    (эквивалент IPC set_settings) следующий вызов _refresh_settings() + _should_send()
    использует новое значение часа без перезапуска backend.
    """

    def _make_scheduler_with_provider(
        self,
        tmpdir: Path,
        initial_hour: int = 20,
        provider_dict: Optional[dict] = None,
    ):
        """Создаёт RecapScheduler с settings_provider."""
        sender = MagicMock(spec=EmailSender)
        store = MagicMock()
        digest_gen = MagicMock()
        digest_gen.generate_digest.return_value = _make_fake_digest()

        # Mutable container so the lambda captures by reference
        live_settings: dict = provider_dict if provider_dict is not None else {}

        sched = RecapScheduler(
            email_sender=sender,
            digest_generator=digest_gen,
            store=store,
            data_dir=tmpdir,
            recap_email_to="test@example.com",
            recap_time_hour=initial_hour,
            enabled=True,
            check_interval_sec=1,
            settings_provider=lambda: live_settings,
        )
        return sched, sender, live_settings

    def test_hour_change_picked_up_on_next_tick(self):
        """Изменение recap_time_hour в settings_provider видно после _refresh_settings()."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            live_settings: dict = {
                "recap_enabled": True,
                "recap_time_hour": 20,
                "recap_email_to": "test@example.com",
            }
            sched, _, _ = self._make_scheduler_with_provider(
                tmpdir, initial_hour=20, provider_dict=live_settings
            )

            # Hour=20 → should_send at 20:00
            now_20 = datetime(2026, 5, 26, 20, 0, 0)
            sched._refresh_settings()
            self.assertTrue(sched._should_send(now_20), "должен отправлять в 20:00")

            # IPC set_settings({"recap_time_hour": 8}) — imitate runtime change
            live_settings["recap_time_hour"] = 8

            # Before refresh: still thinks it's hour=20
            # (already updated in previous refresh, but let's check refresh effect)
            sched._refresh_settings()

            # Hour=8 now → should NOT send at 20:00
            self.assertFalse(
                sched._should_send(now_20),
                "после изменения часа на 8 не должен отправлять в 20:00",
            )
            # But should send at 08:00
            now_08 = datetime(2026, 5, 26, 8, 0, 0)
            self.assertTrue(
                sched._should_send(now_08),
                "должен отправлять в 08:00 после смены часа",
            )

    def test_enabled_toggle_picked_up_on_next_tick(self):
        """Изменение recap_enabled=False в settings_provider отключает отправку."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            live_settings: dict = {
                "recap_enabled": True,
                "recap_time_hour": 20,
                "recap_email_to": "test@example.com",
            }
            sched, _, _ = self._make_scheduler_with_provider(
                tmpdir, initial_hour=20, provider_dict=live_settings
            )

            now_20 = datetime(2026, 5, 26, 20, 0, 0)
            sched._refresh_settings()
            self.assertTrue(sched._should_send(now_20))

            # Disable via IPC-equivalent mutation
            live_settings["recap_enabled"] = False
            sched._refresh_settings()

            self.assertFalse(
                sched._should_send(now_20),
                "после отключения recap_enabled не должен отправлять",
            )

    def test_email_to_change_picked_up_on_next_tick(self):
        """Изменение recap_email_to в settings_provider обновляет адрес получателя."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            live_settings: dict = {
                "recap_enabled": True,
                "recap_time_hour": 20,
                "recap_email_to": "old@example.com",
            }
            sched, _, _ = self._make_scheduler_with_provider(
                tmpdir, initial_hour=20, provider_dict=live_settings
            )

            sched._refresh_settings()
            self.assertEqual(sched.recap_email_to, "old@example.com")

            live_settings["recap_email_to"] = "new@example.com"
            sched._refresh_settings()
            self.assertEqual(sched.recap_email_to, "new@example.com")

    def test_no_settings_provider_uses_constructor_defaults(self):
        """Без settings_provider конструкторные defaults остаются стабильными."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sender = MagicMock(spec=EmailSender)
            store = MagicMock()
            digest_gen = MagicMock()
            digest_gen.generate_digest.return_value = _make_fake_digest()

            sched = RecapScheduler(
                email_sender=sender,
                digest_generator=digest_gen,
                store=store,
                data_dir=tmpdir,
                recap_email_to="fixed@example.com",
                recap_time_hour=15,
                enabled=True,
                # No settings_provider
            )

            sched._refresh_settings()
            self.assertEqual(sched.recap_time_hour, 15)
            self.assertEqual(sched.recap_email_to, "fixed@example.com")
            self.assertTrue(sched.enabled)

    def test_settings_provider_exception_falls_back_to_defaults(self):
        """Если settings_provider бросает исключение — используются constructor defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            sender = MagicMock(spec=EmailSender)
            store = MagicMock()
            digest_gen = MagicMock()
            digest_gen.generate_digest.return_value = _make_fake_digest()

            def broken_provider():
                raise RuntimeError("settings DB unavailable")

            sched = RecapScheduler(
                email_sender=sender,
                digest_generator=digest_gen,
                store=store,
                data_dir=tmpdir,
                recap_email_to="fallback@example.com",
                recap_time_hour=18,
                enabled=True,
                settings_provider=broken_provider,
            )

            # Should not raise; should fall back to constructor defaults
            sched._refresh_settings()
            self.assertEqual(sched.recap_time_hour, 18)
            self.assertEqual(sched.recap_email_to, "fallback@example.com")


if __name__ == "__main__":
    unittest.main()
