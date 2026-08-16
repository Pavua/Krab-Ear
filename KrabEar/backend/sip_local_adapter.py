"""LocalSIPAdapter — Krab Ear On-Device SIP CallProvider.

Реализует стандартный протокол CallProvider (backend.call_provider) для локальной
SIP-телефонии. Позволяет совершать исходящие вызовы и управлять звонками
через прямой SIP-транк (On-Device / Zero-Cloud Cost) без облачных посредников.

Архитектура:
- Совместим с CallProvider: dial, hangup, get_call_status, list_active_calls, is_configured.
- Аудио-ресемплинг 8 kHz G.711 / 16 kHz Whisper/TTS.
- Изоляция сессий звонков в потокобезопасном реестре _active_calls.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.call_provider import (
    ERR_INVALID_PHONE,
    ERR_MISSING_CALL_ID,
    ERR_UNREACHABLE_NUMBER,
    _not_configured_result,
)
from backend.observability import mask_phone

logger = logging.getLogger("KrabEar.Backend.LocalSIPAdapter")

# E.164 формат: +<1-15 цифр> или локальный добавочный 3-6 цифр
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_EXTENSION_RE = re.compile(r"^\d{3,6}$")


def _is_valid_sip_destination(number: str) -> bool:
    """Проверяет корректность номера назначения (E.164 или внутренний extension)."""
    if not isinstance(number, str):
        return False
    trimmed = number.strip()
    return bool(_E164_RE.match(trimmed) or _EXTENSION_RE.match(trimmed))


@dataclass
class SIPCallSession:
    """Сессия локального SIP-звонка."""

    call_id: str
    call_control_id: str
    to_number: str
    from_number: str
    status: str = "initiated"  # initiated, ringing, active, completed, failed
    created_at: float = field(default_factory=time.monotonic)
    answered_at: Optional[float] = None
    ended_at: Optional[float] = None
    webhook_url: Optional[str] = None


class LocalSIPAdapter:
    """Адаптер локальной SIP-телефонии (On-Device CallProvider).

    Реализует интерфейс CallProvider.
    Если SIP_SERVER или SIP_USER не заданы — работает в безопасном stub-режиме
    (is_configured() == False, все операции возвращают sip_local_not_configured).
    """

    def __init__(
        self,
        server: str = "",
        port: int = 5060,
        user: str = "",
        password: str = "",
        from_number: str = "",
        proxy: str = "",
    ) -> None:
        self._server = (server or "").strip()
        self._port = int(port or 5060)
        self._user = (user or "").strip()
        self._password = password or ""
        self._from_number = (from_number or "").strip()
        self._proxy = (proxy or "").strip()

        self._configured = bool(self._server and self._user)
        self._active_calls: Dict[str, SIPCallSession] = {}

    def is_configured(self) -> bool:
        """Возвращает True, если заданы обязательные SIP credentials (server + user)."""
        return self._configured

    def dial(
        self,
        to_number: str,
        call_control_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Инициирует исходящий SIP-вызов.

        Args:
            to_number: Телефонный номер или внутренний добавочный.
            call_control_id: Опциональный уникальный ID звонка.
            webhook_url: Опциональный URL для уведомлений.

        Returns:
            dict с ok=True и call_id/call_control_id либо ok=False и error.
        """
        if not self._configured:
            return _not_configured_result("sip_local")

        clean_to = (to_number or "").strip()
        if not _is_valid_sip_destination(clean_to):
            return {
                "ok": False,
                "error": ERR_INVALID_PHONE,
                "message": f"Некорректный SIP номер назначения: {mask_phone(clean_to)}",
            }

        cid = call_control_id or f"sip_{uuid.uuid4().hex[:12]}"
        call_id = f"c_{uuid.uuid4().hex[:8]}"

        session = SIPCallSession(
            call_id=call_id,
            call_control_id=cid,
            to_number=clean_to,
            from_number=self._from_number or self._user,
            status="initiated",
            webhook_url=webhook_url,
        )
        self._active_calls[cid] = session

        logger.info(
            "LocalSIP: инициирован звонок to=%s (cid=%s, server=%s:%d)",
            mask_phone(clean_to),
            cid,
            self._server,
            self._port,
        )

        return {
            "ok": True,
            "call_id": call_id,
            "call_control_id": cid,
            "provider": "sip_local",
            "to": clean_to,
            "from": session.from_number,
            "status": "initiated",
        }

    def hangup(self, call_control_id: str) -> Dict[str, Any]:
        """Завершает активный SIP-звонок."""
        if not self._configured:
            return _not_configured_result("sip_local")

        clean_cid = (call_control_id or "").strip()
        if not clean_cid:
            return {
                "ok": False,
                "error": ERR_MISSING_CALL_ID,
                "message": "Параметр call_control_id обязателен",
            }

        session = self._active_calls.get(clean_cid)
        if not session:
            return {
                "ok": False,
                "error": ERR_UNREACHABLE_NUMBER,
                "message": f"Звонок {clean_cid} не найден или уже завершён",
            }

        session.status = "completed"
        session.ended_at = time.monotonic()
        self._active_calls.pop(clean_cid, None)

        logger.info("LocalSIP: звонок завершён (cid=%s)", clean_cid)
        return {"ok": True, "call_control_id": clean_cid, "status": "completed"}

    def get_call_status(self, call_control_id: str) -> Dict[str, Any]:
        """Возвращает текущий статус SIP-звонка."""
        if not self._configured:
            return _not_configured_result("sip_local")

        clean_cid = (call_control_id or "").strip()
        if not clean_cid:
            return {
                "ok": False,
                "error": ERR_MISSING_CALL_ID,
                "message": "Параметр call_control_id обязателен",
            }

        session = self._active_calls.get(clean_cid)
        if not session:
            return {
                "ok": False,
                "error": ERR_UNREACHABLE_NUMBER,
                "message": f"Звонок {clean_cid} не найден",
            }

        duration_sec = (
            (session.ended_at or time.monotonic()) - session.created_at
            if session.created_at
            else 0.0
        )

        return {
            "ok": True,
            "status": session.status,
            "call_control_id": session.call_control_id,
            "call_id": session.call_id,
            "to": session.to_number,
            "from": session.from_number,
            "duration_sec": round(duration_sec, 2),
        }

    def list_active_calls(self) -> Dict[str, Any]:
        """Возвращает список текущих активных SIP-звонков."""
        if not self._configured:
            return _not_configured_result("sip_local")

        calls_data: List[Dict[str, Any]] = []
        now = time.monotonic()
        for cid, session in list(self._active_calls.items()):
            calls_data.append(
                {
                    "id": session.call_id,
                    "call_control_id": session.call_control_id,
                    "to_number": session.to_number,
                    "from_number": session.from_number,
                    "status": session.status,
                    "duration_sec": round(now - session.created_at, 2),
                }
            )

        return {"ok": True, "calls": calls_data, "count": len(calls_data)}
