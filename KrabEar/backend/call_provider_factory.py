"""CallProvider factory — возвращает нужный адаптер по настройкам.

Использование:
    from backend.call_provider_factory import get_provider
    provider = get_provider(settings)
    result = provider.dial("+79001234567")

Настройки:
    CALL_PROVIDER: "telnyx" | "twilio" | "none"  (default: "telnyx")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.call_provider import CallProvider

logger = logging.getLogger("KrabEar.Backend.CallProviderFactory")

# Допустимые значения CALL_PROVIDER
PROVIDER_GATEWAY = "gateway"
PROVIDER_TELNYX = "telnyx"
PROVIDER_TWILIO = "twilio"
PROVIDER_SIP_LOCAL = "sip_local"
PROVIDER_NONE = "none"
_VALID_PROVIDERS = frozenset(
    [PROVIDER_GATEWAY, PROVIDER_TELNYX, PROVIDER_TWILIO, PROVIDER_SIP_LOCAL, PROVIDER_NONE]
)


class NullCallProvider:
    """Заглушка: всегда возвращает {"ok": False, "error": "no_provider"}.

    Используется при CALL_PROVIDER="none" или неизвестном значении.
    """

    def is_configured(self) -> bool:
        return False

    def dial(self, to_number: str, call_control_id: Any = None, webhook_url: Any = None) -> dict:
        return {"ok": False, "error": "no_provider", "message": "Провайдер звонков не выбран"}

    def hangup(self, call_control_id: str) -> dict:
        return {"ok": False, "error": "no_provider", "message": "Провайдер звонков не выбран"}

    def get_call_status(self, call_control_id: str) -> dict:
        return {"ok": False, "error": "no_provider", "message": "Провайдер звонков не выбран"}

    def list_active_calls(self) -> dict:
        return {"ok": False, "error": "no_provider", "message": "Провайдер звонков не выбран"}


def get_provider(settings: Any) -> "CallProvider":
    """Создаёт и возвращает CallProvider по значению settings.CALL_PROVIDER.

    Args:
        settings: объект настроек (core.config.Settings или совместимый).
                  Читаются атрибуты: CALL_PROVIDER, TELNYX_*, TWILIO_*, SIP_*.

    Returns:
        Экземпляр TelnyxAdapter, TwilioAdapter, LocalSIPAdapter или NullCallProvider.
    """
    provider_name = (getattr(settings, "CALL_PROVIDER", PROVIDER_TELNYX) or "").lower().strip()

    if provider_name not in _VALID_PROVIDERS:
        logger.warning(
            "Неизвестный CALL_PROVIDER=%r, используется NullCallProvider", provider_name
        )
        return NullCallProvider()  # type: ignore[return-value]

    if provider_name == PROVIDER_NONE:
        logger.info("CALL_PROVIDER=none, используется NullCallProvider")
        return NullCallProvider()  # type: ignore[return-value]

    if provider_name == PROVIDER_GATEWAY:
        # Волна консолидации 03.09.2026: линия принадлежит Voice Gateway, у него
        # пять транспортов и ежедневные живые звонки; наши адаптеры не совершили
        # ни одного. Спека — docs/superpowers/specs/2026-09-03-telephony-consolidation.md.
        from backend.gateway_call_provider import GatewayCallProvider

        adapter = GatewayCallProvider(
            base_url=getattr(settings, "VOICE_GATEWAY_URL", "") or "",
            api_key=getattr(settings, "VOICE_GATEWAY_API_KEY", "") or "",
        )
        logger.info("CallProvider=gateway configured=%s", adapter.is_configured())
        return adapter  # type: ignore[return-value]

    if provider_name == PROVIDER_SIP_LOCAL:
        from backend.sip_local_adapter import LocalSIPAdapter

        server = getattr(settings, "SIP_SERVER", "") or ""
        port = int(getattr(settings, "SIP_PORT", 5060) or 5060)
        user = getattr(settings, "SIP_USER", "") or ""
        password = getattr(settings, "SIP_PASSWORD", "") or ""
        from_number = getattr(settings, "SIP_FROM_NUMBER", "") or ""
        proxy = getattr(settings, "SIP_PROXY", "") or ""

        adapter = LocalSIPAdapter(
            server=server,
            port=port,
            user=user,
            password=password,
            from_number=from_number,
            proxy=proxy,
        )
        configured = adapter.is_configured()
        logger.info(
            "CallProvider=sip_local configured=%s server=%s:%d user=%s",
            configured,
            server or "(not set)",
            port,
            user or "(not set)",
        )
        return adapter  # type: ignore[return-value]

    if provider_name == PROVIDER_TWILIO:
        from backend.twilio_adapter import TwilioAdapter

        account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "") or ""
        auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "") or ""
        from_number = getattr(settings, "TWILIO_FROM_NUMBER", "") or ""

        adapter = TwilioAdapter(
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=from_number,
        )
        configured = adapter.is_configured()
        logger.info(
            "CallProvider=twilio configured=%s from=%s",
            configured,
            from_number or "(not set)",
        )
        return adapter  # type: ignore[return-value]

    # Default: telnyx
    from backend.telnyx_adapter import TelnyxAdapter

    api_key = getattr(settings, "TELNYX_API_KEY", "") or ""
    connection_id = getattr(settings, "TELNYX_CONNECTION_ID", "") or ""
    from_number = getattr(settings, "TELNYX_FROM_NUMBER", "") or ""

    adapter = TelnyxAdapter(
        api_key=api_key,
        connection_id=connection_id,
        from_number=from_number,
    )
    configured = adapter._configured  # noqa: SLF001 (internal attribute)
    logger.info(
        "CallProvider=telnyx configured=%s from=%s",
        configured,
        from_number or "(not set)",
    )
    return adapter  # type: ignore[return-value]
