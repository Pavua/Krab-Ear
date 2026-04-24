"""Abstract CallProvider Protocol — Krab Ear Phase 3 provider abstraction.

Общий интерфейс для всех поставщиков телефонии (Telnyx, Twilio, …).
Все методы возвращают унифицированный dict: {"ok": bool, …}.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, runtime_checkable

try:
    from typing import Protocol
except ImportError:  # Python < 3.8
    from typing_extensions import Protocol  # type: ignore[no-redef]


@runtime_checkable
class CallProvider(Protocol):
    """Protocol для адаптеров телефонии.

    Реализующий класс должен предоставить все пять методов.
    Для stub/unconfigured режима каждый метод возвращает
    {"ok": False, "error": "<provider>_not_configured"}.
    """

    # ------------------------------------------------------------------
    # Core call operations
    # ------------------------------------------------------------------

    def dial(
        self,
        to_number: str,
        call_control_id: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Инициирует исходящий вызов.

        Args:
            to_number: Номер назначения в формате E.164.
            call_control_id: Опциональный идентификатор звонка (provider-specific).
            webhook_url: URL для event-хуков провайдера.

        Returns:
            Успех: {"ok": True, "call_id": str, "call_control_id": str, ...}
            Ошибка: {"ok": False, "error": str, "message": str}
        """
        ...

    def hangup(self, call_control_id: str) -> Dict[str, Any]:
        """Завершает активный звонок.

        Args:
            call_control_id: Идентификатор звонка, полученный из dial().

        Returns:
            Успех: {"ok": True}
            Ошибка: {"ok": False, "error": str}
        """
        ...

    def get_call_status(self, call_control_id: str) -> Dict[str, Any]:
        """Возвращает текущий статус звонка.

        Returns:
            Успех: {"ok": True, "status": str, "data": dict}
            Ошибка: {"ok": False, "error": str}
        """
        ...

    def list_active_calls(self) -> Dict[str, Any]:
        """Возвращает список активных звонков.

        Returns:
            Успех: {"ok": True, "calls": [{"id", "to_number", "duration_sec", "status"}, ...]}
            Ошибка: {"ok": False, "error": str}
        """
        ...

    def is_configured(self) -> bool:
        """Возвращает True если провайдер сконфигурирован (credentials заданы)."""
        ...


# ------------------------------------------------------------------
# Standardised error codes (используются во всех адаптерах)
# ------------------------------------------------------------------

#: Провайдер не сконфигурирован (нет credentials).
ERR_NOT_CONFIGURED = "provider_not_configured"

#: Некорректный формат номера телефона (не E.164).
ERR_INVALID_PHONE = "invalid_phone_number"

#: Неверные credentials (HTTP 401).
ERR_UNAUTHORIZED = "unauthorized"

#: Недостаточно средств на счёте (HTTP 402 или аналог).
ERR_INSUFFICIENT_BALANCE = "insufficient_balance"

#: Номер недоступен / некорректен (HTTP 422 или аналог).
ERR_UNREACHABLE_NUMBER = "unreachable_number"

#: Превышен лимит запросов (HTTP 429).
ERR_RATE_LIMIT = "rate_limit"

#: Сетевая ошибка.
ERR_NETWORK = "network_error"

#: Отсутствует обязательный параметр call_control_id.
ERR_MISSING_CALL_ID = "missing_call_control_id"


def _not_configured_result(provider: str) -> Dict[str, Any]:
    """Стандартный ответ для stub/unconfigured режима."""
    return {
        "ok": False,
        "error": f"{provider}_not_configured",
        "message": f"Провайдер '{provider}' не сконфигурирован",
    }
