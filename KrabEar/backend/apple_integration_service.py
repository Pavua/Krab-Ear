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
from typing import Any, Callable, TYPE_CHECKING

from core.config import settings
from backend.telegram_bridge import CircuitBreakerOpen, TelegramBridge

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
            raise RuntimeError(f"circuit_open: {exc}") from exc
        except (Exception,) as exc:
            msg = str(exc)
            if "krab_unavailable" in msg or "krab_error" in msg:
                raise RuntimeError(msg) from exc
            raise RuntimeError(f"krab_unavailable: {msg}") from exc

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
            raise RuntimeError(f"circuit_open: {exc}") from exc
        except Exception as exc:
            msg = str(exc)
            if "krab_unavailable" in msg or "krab_error" in msg:
                raise RuntimeError(msg) from exc
            raise RuntimeError(f"krab_unavailable: {msg}") from exc

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
        # W1765 MED-1: экранируем переносы строк и NUL (могут прервать AppleScript-инструкцию)
        s = re.sub(r'[\r\n\x00]', ' ', s)
        s = s.replace('\\', '\\\\')  # обратный слэш ПЕРВЫМ — иначе \\" → дублирование
        s = s.replace('"', '\\"')
        return s

    # ── Apple Notes integration (Phase D.4) ─────────────────────────────────

    def handle_create_apple_note(self, params: dict) -> dict:
        """Create Apple Note from text via osascript.

        params: {"title": str, "body": str, "folder": str | None}
        Returns: {"ok": bool, "note_id": str | None, "error": str | None}
        """
        title = self._escape_as_str(params.get("title", "Krab Ear note"))
        body = self._escape_as_str(params.get("body", ""))
        folder = params.get("folder", "") or ""

        if folder:
            folder_escaped = self._escape_as_str(folder)
            script = f'''
tell application "Notes"
    tell account "iCloud"
        set targetFolder to folder "{folder_escaped}"
        make new note at targetFolder with properties {{name:"{title}", body:"{body}"}}
    end tell
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
                capture_output=True, text=True, timeout=10,
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
        title = self._escape_as_str(params.get("title", "Krab Ear reminder"))
        body = self._escape_as_str(params.get("body", ""))
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
                capture_output=True, text=True, timeout=10,
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
        title = params.get("title", "").strip()
        if not title:
            return {"ok": False, "error": "title is required"}

        title_esc = self._escape_as_str(title)
        notes = params.get("notes", "") or ""
        notes_esc = self._escape_as_str(notes)
        start_date = str(params.get("start_date", "")).strip()
        if not start_date:
            return {"ok": False, "error": "start_date is required"}
        start_date_esc = self._escape_as_str(start_date)
        duration_minutes = int(params.get("duration_minutes", 30) or 30)
        calendar_name = params.get("calendar_name") or None

        event_block = f'''
        set startDate to date "{start_date_esc}"
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
                capture_output=True, text=True, timeout=15,
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
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return {"ok": True, "error": None}
            return {"ok": False, "error": result.stderr.strip()}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "osascript timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
