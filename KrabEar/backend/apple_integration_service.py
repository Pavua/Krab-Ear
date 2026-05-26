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

    # ── Apple Notes integration (Phase D.4) ─────────────────────────────────

    def handle_create_apple_note(self, params: dict) -> dict:
        """Create Apple Note from text via osascript.

        params: {"title": str, "body": str, "folder": str | None}
        Returns: {"ok": bool, "note_id": str | None, "error": str | None}
        """
        title = params.get("title", "Krab Ear note").replace('"', '\\"')
        body = params.get("body", "").replace('"', '\\"')
        folder = params.get("folder", "") or ""

        if folder:
            folder_escaped = folder.replace('"', '\\"')
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
        title = params.get("title", "Krab Ear reminder").replace('"', '\\"')
        body = params.get("body", "").replace('"', '\\"')
        list_name = params.get("list_name") or None
        due_date = params.get("due_date") or None

        # Build properties clause
        properties = f'name:"{title}"'
        if body:
            properties += f', body:"{body}"'
        if due_date:
            due_date_escaped = due_date.replace('"', '\\"')
            properties += f', due date:date "{due_date_escaped}"'

        if list_name:
            list_name_escaped = list_name.replace('"', '\\"')
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

        title_esc = title.replace('"', '\\"')
        notes = params.get("notes", "") or ""
        notes_esc = notes.replace('"', '\\"')
        start_date = str(params.get("start_date", "")).strip()
        if not start_date:
            return {"ok": False, "error": "start_date is required"}
        start_date_esc = start_date.replace('"', '\\"')
        duration_minutes = int(params.get("duration_minutes", 30) or 30)
        calendar_name = params.get("calendar_name") or None

        event_block = f'''
        set startDate to date "{start_date_esc}"
        set endDate to startDate + ({duration_minutes} * minutes)
        make new event with properties {{summary:"{title_esc}", description:"{notes_esc}", start date:startDate, end date:endDate}}'''

        if calendar_name:
            cal_esc = calendar_name.replace('"', '\\"')
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

        body = params.get("body", "").strip()
        if not body:
            return {"ok": False, "error": "body is required"}

        service_name = params.get("service", "iMessage") or "iMessage"
        if service_name not in ("iMessage", "SMS"):
            service_name = "iMessage"

        # Map service name to AppleScript service type constant
        service_type = "iMessage" if service_name == "iMessage" else "SMS"

        # Escape double quotes to prevent AppleScript injection
        recipient_esc = recipient.replace('"', '\\"')
        body_esc = body.replace('"', '\\"')

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
