"""RecapScheduler — ежедневная отправка дайджеста транскрипций на email.

Запускает фоновый поток, который каждые N минут проверяет, наступил ли
RECAP_TIME_HOUR. При наступлении часа (и если за сегодня ещё не отправлено)
генерирует DailyDigest → форматирует HTML → отправляет через EmailSender.

Состояние (last_sent_date) хранится в recap_state.json в data_dir.
Двойная отправка за одну дату исключена даже при рестарте backend.
"""

from __future__ import annotations

import html
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("KrabEar.Backend.RecapScheduler")

# Интервал проверки (секунды). Достаточно мелко, чтобы не пропустить час.
_CHECK_INTERVAL_SEC: int = 60

# Имя файла состояния
_STATE_FILENAME = "recap_state.json"


# ---------------------------------------------------------------------------
# HTML-шаблон дайджеста
# ---------------------------------------------------------------------------

def _build_html(digest: Any) -> str:  # digest: DailyDigest
    """Форматирует DailyDigest в HTML-письмо."""
    topics_html = ""
    if digest.top_topics:
        tags = "".join(
            f'<span style="display:inline-block;background:#e8f4fd;border-radius:4px;'
            f'padding:2px 8px;margin:2px;font-size:13px;color:#1565c0;">{html.escape(t)}</span>'
            for t in digest.top_topics
        )
        topics_html = f"""
        <tr>
          <td style="padding:16px 24px 0;">
            <h3 style="margin:0 0 8px;font-size:15px;color:#333;">Темы дня</h3>
            <div>{tags}</div>
          </td>
        </tr>"""

    highlights_html = ""
    if digest.highlights:
        items_html = "".join(
            f'<li style="margin-bottom:10px;line-height:1.5;color:#444;">'
            f'{html.escape(h)}</li>'
            for h in digest.highlights
        )
        highlights_html = f"""
        <tr>
          <td style="padding:16px 24px 0;">
            <h3 style="margin:0 0 8px;font-size:15px;color:#333;">Избранные фрагменты</h3>
            <ol style="margin:0;padding-left:20px;">{items_html}</ol>
          </td>
        </tr>"""

    lang_str = ""
    if digest.languages_used:
        parts = [
            f"{html.escape(str(lang))}&nbsp;({int(cnt)})"
            for lang, cnt in sorted(digest.languages_used.items(), key=lambda x: -x[1])
        ]
        lang_str = ", ".join(parts)

    empty_note = ""
    if digest.total_recordings == 0:
        empty_note = (
            '<tr><td style="padding:16px 24px;color:#888;font-style:italic;">'
            'Записей за этот день не найдено.</td></tr>'
        )

    safe_date = html.escape(str(digest.date))
    safe_total_recordings = html.escape(str(digest.total_recordings))
    safe_total_duration_min = html.escape(str(digest.total_duration_min))
    safe_total_words = html.escape(str(digest.total_words))

    email_html = f"""\
<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8">
<title>Krab Ear — дайджест {safe_date}</title>
</head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:12px;overflow:hidden;
              box-shadow:0 2px 8px rgba(0,0,0,0.08);">

  <!-- Header -->
  <tr>
    <td style="background:linear-gradient(135deg,#1976d2,#42a5f5);
               padding:24px 24px 20px;text-align:center;">
      <h1 style="margin:0;color:#fff;font-size:22px;font-weight:600;">
        Krab Ear — Дайджест
      </h1>
      <p style="margin:4px 0 0;color:rgba(255,255,255,0.85);font-size:14px;">
        {safe_date}
      </p>
    </td>
  </tr>

  <!-- Stats -->
  <tr>
    <td style="padding:24px 24px 0;">
      <table width="100%" cellpadding="0" cellspacing="8">
        <tr>
          <td align="center" style="background:#f0f7ff;border-radius:8px;padding:16px;">
            <div style="font-size:28px;font-weight:700;color:#1976d2;">{safe_total_recordings}</div>
            <div style="font-size:12px;color:#666;margin-top:2px;">записей</div>
          </td>
          <td width="8"></td>
          <td align="center" style="background:#f0fff4;border-radius:8px;padding:16px;">
            <div style="font-size:28px;font-weight:700;color:#388e3c;">{safe_total_duration_min}</div>
            <div style="font-size:12px;color:#666;margin-top:2px;">минут</div>
          </td>
          <td width="8"></td>
          <td align="center" style="background:#fff8f0;border-radius:8px;padding:16px;">
            <div style="font-size:28px;font-weight:700;color:#e65100;">{safe_total_words}</div>
            <div style="font-size:12px;color:#666;margin-top:2px;">слов</div>
          </td>
        </tr>
      </table>
      {(f'<p style="margin:8px 0 0;font-size:13px;color:#888;">Языки: {lang_str}</p>') if lang_str else ""}
    </td>
  </tr>

  {topics_html}
  {highlights_html}
  {empty_note}

  <!-- Footer -->
  <tr>
    <td style="padding:24px;text-align:center;border-top:1px solid #eee;margin-top:16px;">
      <p style="margin:0;font-size:12px;color:#aaa;">
        Krab Ear &middot; Ежедневный дайджест &middot;
        Для отписки отключите RECAP_EMAIL_ENABLED в настройках.
      </p>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return email_html


# ---------------------------------------------------------------------------
# RecapScheduler
# ---------------------------------------------------------------------------

class RecapScheduler:
    """Фоновый планировщик ежедневного email-дайджеста.

    Thread-safe. Запускает daemon-поток при вызове start().
    Останавливается через stop().

    Args:
        email_sender: Экземпляр EmailSender для отправки.
        digest_generator: Экземпляр DailyDigestGenerator.
        store: Экземпляр StateStore (источник истории для дайджеста).
        data_dir: Директория для хранения recap_state.json.
        recap_email_to: Email получателя (default, переопределяется settings_provider).
        recap_time_hour: Час отправки (0–23) (default, переопределяется settings_provider).
        enabled: Включить/выключить (default, переопределяется settings_provider).
        check_interval_sec: Переопределяет интервал проверки (для тестов).
        clock_fn: Переопределяет datetime.now() (для тестов).
        settings_provider: Опциональный callable () -> Dict, вызывается каждый тик
            планировщика для чтения актуальных значений recap_enabled,
            recap_time_hour и recap_email_to.  Если None — используются
            значения, переданные в конструктор (обратная совместимость).
    """

    def __init__(
        self,
        email_sender: Any,
        digest_generator: Any,
        store: Any,
        data_dir: "Path | str",
        recap_email_to: str = "",
        recap_time_hour: int = 20,
        enabled: bool = False,
        check_interval_sec: int = _CHECK_INTERVAL_SEC,
        clock_fn: Any = None,
        settings_provider: Optional[Callable[[], Dict]] = None,
    ) -> None:
        self.email_sender = email_sender
        self.digest_generator = digest_generator
        self.store = store
        self.data_dir = Path(data_dir)
        # Constructor defaults — used as fallback when settings_provider is absent
        # or raises an exception.
        self._default_recap_email_to = recap_email_to
        self._default_recap_time_hour = recap_time_hour
        self._default_enabled = enabled
        # Live values (overwritten each tick when settings_provider is wired)
        self.recap_email_to = recap_email_to
        self.recap_time_hour = recap_time_hour
        self.enabled = enabled
        self._settings_provider = settings_provider
        self._check_interval_sec = check_interval_sec
        self._clock_fn = clock_fn or datetime.now

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Runtime settings refresh
    # ------------------------------------------------------------------

    def _current_settings(self) -> Dict:
        """Возвращает актуальные настройки из settings_provider (с fallback).

        Никогда не бросает исключение — при ошибке возвращает пустой dict,
        и _refresh_settings() упадёт на default-значения конструктора.
        """
        if self._settings_provider is None:
            return {}
        try:
            return self._settings_provider() or {}
        except Exception:
            logger.exception("RecapScheduler: не удалось получить настройки, используются defaults")
            return {}

    def _refresh_settings(self) -> None:
        """Перечитывает recap_email_enabled / recap_time_hour / recap_email_to из runtime настроек.

        Вызывается в начале каждого тика _run() вне recap_lock,
        что соответствует рекомендации W922: re-read происходит до
        проверки _should_send().
        """
        s = self._current_settings()
        # Ключ настройки — "recap_email_enabled" (DEFAULT_SETTINGS / settings.json).
        # Раньше читался несуществующий "recap_enabled" → set_settings(recap_email_enabled=True)
        # был тихим no-op (письма не уходили, ошибки нет). Принимаем оба ключа для
        # обратной совместимости со старыми persisted-настройками.
        enabled_raw = s.get("recap_email_enabled", s.get("recap_enabled", self._default_enabled))
        self.enabled = bool(enabled_raw)
        hour_raw = s.get("recap_time_hour", self._default_recap_time_hour)
        try:
            # wave-1770 HIGH: clamp to valid 0-23 range. An out-of-bounds value
            # (e.g. -1 or 25) would make `now.hour != self.recap_time_hour` always
            # True, silently disabling the scheduler indefinitely.
            self.recap_time_hour = max(0, min(23, int(hour_raw)))
        except (TypeError, ValueError):
            self.recap_time_hour = self._default_recap_time_hour
        email_raw = s.get("recap_email_to", self._default_recap_email_to)
        self.recap_email_to = str(email_raw or "")

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.data_dir / _STATE_FILENAME

    def _load_state(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"last_sent_date": None, "send_count": 0}

    def _save_state(self, state: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def _should_send(self, now: datetime) -> bool:
        """Возвращает True если нужно отправить дайджест прямо сейчас.

        Примечание: вызывающий код обязан держать self._lock при вызове
        этого метода, если проверка используется для принятия решения об
        отправке (иначе возможен TOCTOU).
        """
        if not self.enabled:
            return False
        if not self.recap_email_to:
            return False
        if now.hour != self.recap_time_hour:
            return False
        today_str = now.date().isoformat()
        state = self._load_state()
        if state.get("last_sent_date") == today_str:
            return False
        # Проверяем маркер «в процессе отправки» — исключает параллельный TOCTOU
        if state.get("_sending_date") == today_str:
            return False
        return True

    def send_recap(self, target_date: Optional[str] = None) -> dict:
        """Генерирует и отправляет дайджест.

        Thread-safe: использует двухфазную блокировку чтобы исключить
        TOCTOU-гонку при одновременном вызове из scheduler-цикла и
        IPC-обработчика.

        Алгоритм:
          1. Под lock: проверить _should_send + записать "_sending_date"
             (атомарный tentative-маркер). Конкурирующий вызов увидит
             маркер и выйдет без отправки.
          2. Без lock: выполнить SMTP (долгая IO-операция).
          3. Под lock: заменить "_sending_date" на "last_sent_date",
             увеличить счётчик, сохранить состояние.
          4. При ошибке SMTP: под lock очистить "_sending_date" (разрешить
             повторную попытку).

        Args:
            target_date: Дата в формате YYYY-MM-DD или None (сегодня).

        Returns:
            dict с ключами: sent (bool), date (str), error (str|None).
        """
        now = self._clock_fn()
        date_str = target_date or now.date().isoformat()

        # --- Privacy gate: режим конфиденциальности запрещает egress ---
        # Дайджест формируется из текста транскрипций, поэтому при активном
        # privacy_mode_enabled письмо НЕ отправляется (mirror export_scheduler F4).
        # _current_settings() безопасен: при отсутствии провайдера или ошибке
        # возвращает {}, и privacy_mode_enabled трактуется как False.
        if self._current_settings().get("privacy_mode_enabled", False):
            logger.info(
                "recap_scheduler: пропуск отправки дайджеста (privacy mode активен)",
                extra={"date": date_str, "reason": "privacy_mode_active"},
            )
            return {
                "sent": False,
                "date": date_str,
                "error": None,
                "reason": "privacy_mode_active",
            }

        # --- Фаза 1: атомарно зарезервировать отправку ---
        with self._lock:
            state = self._load_state()
            # Быстрая проверка: уже отправлено или другой поток начал отправку?
            if state.get("last_sent_date") == date_str:
                return {"sent": False, "date": date_str, "error": None}
            if state.get("_sending_date") == date_str:
                return {"sent": False, "date": date_str, "error": None}
            # Записываем tentative-маркер до освобождения блокировки
            state["_sending_date"] = date_str
            self._save_state(state)

        # --- Фаза 2: генерация дайджеста (без lock) ---
        try:
            digest = self.digest_generator.generate_digest(
                date_str=date_str,
                store=self.store,
            )
        except Exception as exc:
            logger.exception("Ошибка генерации дайджеста за %s", date_str)
            # Очищаем маркер, чтобы следующая попытка могла пройти
            with self._lock:
                state = self._load_state()
                if state.get("_sending_date") == date_str:
                    state.pop("_sending_date", None)
                    self._save_state(state)
            return {"sent": False, "date": date_str, "error": f"digest_error: {exc}"}

        subject = f"Krab Ear — дайджест за {date_str} ({digest.total_recordings} записей)"
        body_html = _build_html(digest)
        body_text = digest.formatted_markdown

        # --- Фаза 3: отправка email (без lock, долгая IO) ---
        try:
            self.email_sender.send(
                to=self.recap_email_to,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
            )
        except Exception as exc:
            logger.exception("Ошибка отправки дайджеста за %s", date_str)
            # Очищаем маркер, чтобы следующая попытка могла пройти
            with self._lock:
                state = self._load_state()
                if state.get("_sending_date") == date_str:
                    state.pop("_sending_date", None)
                    self._save_state(state)
            return {"sent": False, "date": date_str, "error": f"send_error: {exc}"}

        # --- Фаза 4: зафиксировать успех под lock ---
        with self._lock:
            state = self._load_state()
            state.pop("_sending_date", None)
            state["last_sent_date"] = date_str
            state["send_count"] = state.get("send_count", 0) + 1
            state["last_sent_ts"] = now.isoformat()
            self._save_state(state)

        logger.info(
            "Дайджест за %s отправлен на %s (%d записей)",
            date_str,
            self.recap_email_to,
            digest.total_recordings,
        )
        return {"sent": True, "date": date_str, "error": None}

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Основной цикл фонового потока."""
        logger.info(
            "RecapScheduler запущен: enabled=%s to=%s hour=%d",
            self.enabled,
            self.recap_email_to,
            self.recap_time_hour,
        )
        while not self._stop_event.is_set():
            # Re-read settings each tick so IPC set_settings() takes effect
            # without a backend restart.  Happens OUTSIDE _lock (pure dict read).
            self._refresh_settings()
            try:
                now = self._clock_fn()
                if self._should_send(now):
                    self.send_recap()
            except Exception:
                logger.exception("Необработанная ошибка в RecapScheduler._run")

            self._stop_event.wait(timeout=self._check_interval_sec)

        logger.info("RecapScheduler остановлен")

    def start(self) -> None:
        """Запускает фоновый поток планировщика (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="RecapScheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Останавливает фоновый поток (ждёт завершения до 5 секунд)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Возвращает статус планировщика.

        Returns:
            dict с ключами:
                enabled (bool)
                recap_email_to (str)
                recap_time_hour (int)
                last_sent_date (str | None)
                last_sent_ts (str | None)
                send_count (int)
                next_run (str): ISO-8601 время следующего запуска
                running (bool): True если фоновый поток активен
        """
        state = self._load_state()
        now = self._clock_fn()

        # Вычисляем следующий запуск
        next_run_dt = now.replace(
            hour=self.recap_time_hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if next_run_dt <= now:
            from datetime import timedelta
            next_run_dt = next_run_dt + timedelta(days=1)

        return {
            "enabled": self.enabled,
            "recap_email_to": self.recap_email_to,
            "recap_time_hour": self.recap_time_hour,
            "last_sent_date": state.get("last_sent_date"),
            "last_sent_ts": state.get("last_sent_ts"),
            "send_count": state.get("send_count", 0),
            "next_run": next_run_dt.isoformat(),
            "running": self._thread is not None and self._thread.is_alive(),
        }
