"""AppleIntegrationService — IPC-обработчики интеграций с macOS-приложениями.

Выделен из backend/service.py (Wave 688) для снижения размера монолитного модуля.
Содержит 6 IPC-обработчиков:
  - send_to_telegram        — отправить транскрипцию в Telegram через main Krab userbot
  - list_telegram_chats     — получить список доступных чатов Telegram
  - create_apple_note       — создать заметку в Apple Notes через osascript
  - create_apple_reminder   — создать напоминание в Apple Reminders через osascript
  - create_calendar_event   — создать событие в Apple Calendar через osascript
  - send_imessage           — отправить iMessage/SMS через Messages.app via osascript
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime
from typing import Any, Callable, TYPE_CHECKING

from core.config import settings
from backend.telegram_bridge import CircuitBreakerOpen, TelegramBridge
from backend.ipc_errors import IpcOperationalError

if TYPE_CHECKING:
    pass

logger = logging.getLogger("KrabEar.Backend.AppleIntegrationService")


class AppleIntegrationService:
    """Обработчики IPC-команд для интеграций с macOS-приложениями и Telegram."""

    def __init__(
        self,
        telegram_bridge: TelegramBridge,
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._telegram_bridge = telegram_bridge
        # Optional runtime settings provider (e.g. BackendService._get_runtime_setting).
        # Falls back to always returning the default when not provided.
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda key, default: default)

    # ── Telegram integration ─────────────────────────────────────────────────

    def handle_send_to_telegram(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отправляет текст в Telegram через main Krab userbot.

        Параметры:
          - text: str — текст сообщения (обязательный, не пустой).
          - chat_id: int | str — ID или username чата Telegram (обязательный).
          - reply_to: int | None — ID сообщения для цитирования (опционально).

        Возвращает:
          {message_id, sent_at, chat_title}

        Ошибки:
          - "bridge_disabled" — если TELEGRAM_BRIDGE_ENABLED=false.
          - "krab_unavailable" — если main Krab недоступен (503 / ConnectionError).
          - "circuit_open" — если circuit breaker разомкнут после 3 ошибок подряд.
        """
        if not settings.TELEGRAM_BRIDGE_ENABLED:
            raise RuntimeError("bridge_disabled: Telegram Bridge отключён в настройках")

        # Privacy mode guard: never send transcript text to external service.
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "ok": False,
                "error": "privacy_mode_active",
                "user_msg_ru": "Приватный режим включён — отправка в Telegram запрещена.",
            }

        text = str(params.get("text") or "").strip()
        if not text:
            raise ValueError("Параметр 'text' обязателен и не может быть пустым")

        raw_chat_id = params.get("chat_id")
        if raw_chat_id is None or str(raw_chat_id).strip() == "":
            raise ValueError("Параметр 'chat_id' обязателен")
        chat_id: int | str
        try:
            chat_id = int(raw_chat_id)
        except (ValueError, TypeError):
            chat_id = str(raw_chat_id).strip()

        reply_to_raw = params.get("reply_to")
        reply_to: int | None = None
        if reply_to_raw is not None:
            try:
                reply_to = int(reply_to_raw)
            except (ValueError, TypeError):
                pass

        try:
            result = self._telegram_bridge.send_message(
                text=text,
                chat_id=chat_id,
                reply_to=reply_to,
            )
        except CircuitBreakerOpen as exc:
            raise IpcOperationalError(f"circuit_open: {exc}") from exc
        except (Exception,) as exc:
            msg = str(exc)
            if "krab_unavailable" in msg or "krab_error" in msg:
                raise IpcOperationalError(msg) from exc
            raise IpcOperationalError(f"krab_unavailable: {msg}") from exc

        return result

    def handle_list_telegram_chats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных чатов через main Krab userbot.

        Параметры: нет.

        Возвращает:
          {chats: [{id, title, type}, ...]}

        Ошибки:
          - "bridge_disabled" — если TELEGRAM_BRIDGE_ENABLED=false.
          - "krab_unavailable" — если main Krab недоступен (503 / ConnectionError).
          - "circuit_open" — если circuit breaker разомкнут.
        """
        # W1211 F2: privacy_mode_enabled guard
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "chats": [], "skipped": "privacy_mode"}

        if not settings.TELEGRAM_BRIDGE_ENABLED:
            raise RuntimeError("bridge_disabled: Telegram Bridge отключён в настройках")

        try:
            chats = self._telegram_bridge.get_chats()
        except CircuitBreakerOpen as exc:
            raise IpcOperationalError(f"circuit_open: {exc}") from exc
        except Exception as exc:
            msg = str(exc)
            if "krab_unavailable" in msg or "krab_error" in msg:
                raise IpcOperationalError(msg) from exc
            raise IpcOperationalError(f"krab_unavailable: {msg}") from exc

        return {"chats": chats}

    # ── AppleScript string escaping (W944 / W1052 / W1765) ─────────────────────

    @staticmethod
    def _escape_as_str(s: str) -> str:
        """Экранирует *s* для безопасного встраивания в AppleScript-строку в двойных кавычках.

        Порядок операций критичен:
          1. Заменяем \\r, \\n, \\x00 пробелом — символы конца строки разрывают
             AppleScript-инструкцию и позволяют инъекцию произвольных команд
             (W942 HIGH-1 / W1765 MED-1: регрессия отсутствовала в сервисе).
          2. Удваиваем обратные слэши ДО экранирования кавычек — иначе слэш
             перед кавычкой превратится в \\\\" (двойное экранирование).
          3. Экранируем двойные кавычки как \\".

        Защита от нестроковых входных данных: приводим через str() чтобы не
        пропустить несанированное значение при numeric/None-параметре.

        W1765: добавлена обработка \\r/\\n/\\x00 — слабое место относительно
        BackendService._escape_as_str (re.sub), теперь поведение идентично.
        """
        if not isinstance(s, str):
            s = str(s)
        # W1765 MED-1: экранируем переносы строк и NUL (могут прервать AppleScript-инструкцию).
        # Также Unicode LINE/PARAGRAPH SEPARATOR (U+2028/U+2029) — они выживают после
        # \r/\n-стрипа и, если OSA-лексер трактует их как конец строки, дают тот же
        # injection-bypass; стрип сепараторов из текста для AppleScript-литерала
        # однозначно корректен (defense-in-depth, тот же класс что W942/W1765).
        s = re.sub('[\r\n\x00\u2028\u2029]', ' ', s)
        s = s.replace('\\', '\\\\')  # обратный слэш ПЕРВЫМ — иначе \\" → дублирование
        s = s.replace('"', '\\"')
        return s

    @staticmethod
    def _clamp_field(value: str, max_chars: int, field_name: str = "field") -> str:
        """Fix 4 (LOW): Обрезает user-поле до max_chars символов перед формированием osascript.

        Слишком длинные значения могут привести к ошибкам osascript, чрезмерному
        потреблению памяти или зависанию macOS-процесса.
        Существующая sanitize-логика (_escape_as_str) применяется после обрезки.

        Лимиты: title ≤ 500, body/notes ≤ 20 000 символов.
        """
        if len(value) > max_chars:
            logger.warning(
                "AppleIntegrationService: поле %r обрезано до %d символов (было %d)",
                field_name, max_chars, len(value),
            )
            return value[:max_chars]
        return value

    # константы для защиты от слишком длинных полей (Fix 4)
    _MAX_TITLE_CHARS = 500
    _MAX_BODY_CHARS = 20_000

    # ── Apple Notes integration (Phase D.4) ───────────────────────────────────────────

    def handle_create_apple_note(self, params: dict) -> dict:
        """Create Apple Note from text via osascript.

        params: {"title": str, "body": str, "folder": str | None}
        Returns: {"ok": bool, "note_id": str | None, "error": str | None}
        """
        # wave-1770 HIGH: sends transcript text to external Apple Notes app.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "error": "privacy_mode_active",
                    "user_msg": "Приватный режим включён — запись в Notes запрещена."}
        title = self._escape_as_str(
            self._clamp_field(params.get("title", "Krab Ear note"), self._MAX_TITLE_CHARS, "title")
        )
        body = self._escape_as_str(
            self._clamp_field(params.get("body", ""), self._MAX_BODY_CHARS, "body")
        )
        folder = params.get("folder", "") or ""

        if folder:
            folder_escaped = self._escape_as_str(folder)
            # Fix 3: don't hardcode "iCloud" account — use the default account.
            # Also wrap folder targeting in try/on error so the folder is created
            # if it does not exist yet, rather than failing with an osascript error.
            script = f'''
tell application "Notes"
    set defaultAcct to default account
    set targetFolder to missing value
    try
        set targetFolder to folder "{folder_escaped}" of defaultAcct
    on error
        set targetFolder to (make new folder at defaultAcct with properties {{name:"{folder_escaped}"}})
    end try
    make new note at targetFolder with properties {{name:"{title}", body:"{body}"}}
end tell
'''
        else:
            script = f'''
tell application "Notes"
    make new note with properties {{name:"{title}", body:"{body}"}}
end tell
'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, encoding="utf-8", timeout=10,  # wave-1770 MED: pin UTF-8
            )
            if result.returncode == 0:
                return {"ok": True, "note_id": result.stdout.strip(), "error": None}
            return {"ok": False, "note_id": None, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "note_id": None, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "note_id": None, "error": str(exc)}

    # ── Apple Reminders integration (Phase D.4) ──────────────────────────────

    def handle_create_apple_reminder(self, params: dict) -> dict:
        """Create Apple Reminder from text via osascript.

        params: {"title": str, "body": str, "list_name": str | None, "due_date": str | None}
        Returns: {"ok": bool, "error": str | None}
        """
        # wave-1770 HIGH: sends transcript text to external Apple Reminders app.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "error": "privacy_mode_active",
                    "user_msg": "Приватный режим включён — запись в Reminders запрещена."}
        title = self._escape_as_str(
            self._clamp_field(params.get("title", "Krab Ear reminder"), self._MAX_TITLE_CHARS, "title")
        )
        body = self._escape_as_str(
            self._clamp_field(params.get("body", ""), self._MAX_BODY_CHARS, "body")
        )
        list_name = params.get("list_name") or None
        due_date = params.get("due_date") or None

        # Build properties clause
        properties = f'name:"{title}"'
        if body:
            properties += f', body:"{body}"'
        if due_date:
            due_date_escaped = self._escape_as_str(due_date)
            properties += f', due date:date "{due_date_escaped}"'

        if list_name:
            list_name_escaped = self._escape_as_str(list_name)
            script = f'''
tell application "Reminders"
    tell list "{list_name_escaped}"
        make new reminder with properties {{{properties}}}
    end tell
end tell
'''
        else:
            script = f'''
tell application "Reminders"
    make new reminder with properties {{{properties}}}
end tell
'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, encoding="utf-8", timeout=10,  # wave-1770 MED: pin UTF-8
            )
            if result.returncode == 0:
                return {"ok": True, "error": None}
            return {"ok": False, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── Apple Calendar integration (Phase D.4) ──────────────────────────────

    def handle_create_calendar_event(self, params: dict) -> dict:
        """Create Apple Calendar event via osascript.

        params:
          title: str (required)
          notes: str (optional, default "")
          start_date: str (required, ISO 8601 or AppleScript-parseable date string)
          duration_minutes: int (optional, default 30)
          calendar_name: str | None (optional, default first writable calendar)
        Returns: {"ok": bool, "error": str | None}
        """
        # wave-1770 HIGH: sends transcript text to external Calendar app.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "error": "privacy_mode_active",
                    "user_msg": "Приватный режим включён — запись в Calendar запрещена."}
        title = params.get("title", "").strip()
        if not title:
            return {"ok": False, "error": "title is required"}

        title_esc = self._escape_as_str(
            self._clamp_field(title, self._MAX_TITLE_CHARS, "title")
        )
        notes = params.get("notes", "") or ""
        notes_esc = self._escape_as_str(
            self._clamp_field(notes, self._MAX_BODY_CHARS, "notes")
        )
        start_date = str(params.get("start_date", "")).strip()
        if not start_date:
            return {"ok": False, "error": "start_date is required"}
        duration_minutes = int(params.get("duration_minutes", 30) or 30)
        calendar_name = params.get("calendar_name") or None

        # Fix 1 (HIGH for RU users): AppleScript `date "..."` coercion is LOCALE-DEPENDENT
        # and breaks on ru_RU macOS.  Instead compute an integer delta from now and emit
        # `(current date) + <delta>` arithmetic which is locale-agnostic.
        # Accept ISO-8601 ("yyyy-MM-dd'T'HH:mm:ss") as the canonical format.
        # Backward-compat: also accept the old "MM/dd/yyyy HH:mm:ss" format sent by
        # pre-fix Swift clients, and fall back to raw-string injection as a last resort.
        start_dt: datetime | None = None
        _ISO_FMTS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")
        _LEGACY_FMTS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M")
        for fmt in _ISO_FMTS + _LEGACY_FMTS:
            try:
                start_dt = datetime.strptime(start_date, fmt)
                break
            except ValueError:
                continue

        if start_dt is not None:
            # Compute seconds-delta from now so AppleScript uses locale-safe arithmetic.
            delta_sec = int((start_dt - datetime.now()).total_seconds())
            start_date_block = f"set startDate to (current date) + {delta_sec}"
        else:
            # Unknown format: escape and inject raw string (best-effort, may fail on ru_RU).
            start_date_esc = self._escape_as_str(start_date)
            start_date_block = f'set startDate to date "{start_date_esc}"'

        event_block = f'''
        {start_date_block}
        set endDate to startDate + ({duration_minutes} * minutes)
        make new event with properties {{summary:"{title_esc}", description:"{notes_esc}", start date:startDate, end date:endDate}}'''

        if calendar_name:
            cal_esc = self._escape_as_str(calendar_name)
            script = f'''tell application "Calendar"
    tell calendar "{cal_esc}"{event_block}
    end tell
end tell'''
        else:
            script = f'''tell application "Calendar"
    tell (first calendar whose writable is true){event_block}
    end tell
end tell'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, encoding="utf-8", timeout=15,  # wave-1770 MED: pin UTF-8
            )
            if result.returncode == 0:
                return {"ok": True, "error": None}
            return {"ok": False, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ── iMessage integration (Phase D.4) ────────────────────────────────────

    def handle_send_imessage(self, params: dict) -> dict:
        """Send iMessage/SMS via Messages.app using osascript.

        params:
          recipient: str (required) — phone number, email, or contact name
          body: str (required) — message text
          service: str (optional, default "iMessage") — "iMessage" | "SMS"
        Returns: {"ok": bool, "error": str | None}
        """
        # wave-1770 HIGH: sends transcript text to external Messages app (iMessage/SMS).
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "error": "privacy_mode_active",
                    "user_msg": "Приватный режим включён — отправка iMessage запрещена."}
        recipient = params.get("recipient", "").strip()
        if not recipient:
            return {"ok": False, "error": "recipient is required"}
        # Bounds check: prevent excessively long or unusual recipients.
        # Phone (+XX digits, up to 20 chars), email (up to 254 per RFC5321),
        # or contact name (up to 128 chars).  Strict max covers all three.
        if len(recipient) > 254:
            return {"ok": False, "error": "recipient too long (max 254 chars)"}

        body = params.get("body", "").strip()
        if not body:
            return {"ok": False, "error": "body is required"}
        if len(body) > 10_000:
            return {"ok": False, "error": "body too long (max 10000 chars)"}

        service_name = params.get("service", "iMessage") or "iMessage"
        if service_name not in ("iMessage", "SMS"):
            service_name = "iMessage"

        # Map service name to AppleScript service type constant
        service_type = "iMessage" if service_name == "iMessage" else "SMS"

        # Escape backslashes then double quotes to prevent AppleScript injection
        recipient_esc = self._escape_as_str(recipient)
        body_esc = self._escape_as_str(body)

        script = f'''tell application "Messages"
    set targetService to 1st service whose service type = {service_type}
    set targetBuddy to buddy "{recipient_esc}" of targetService
    send "{body_esc}" to targetBuddy
end tell'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, encoding="utf-8", timeout=10,  # wave-1770 MED: pin UTF-8
            )
            if result.returncode == 0:
                return {"ok": True, "error": None}
            return {"ok": False, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
