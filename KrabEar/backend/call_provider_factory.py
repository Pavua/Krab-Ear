"""CallProvider factory — возвращает нужный адаптер по настройкам.

Использование:
    from backend.call_provider_factory import get_provider
    provider = get_provider(settings)
    result = provider.dial("+79001234567")

Настройки:
    CALL_PROVIDER: "gateway" | "none"  (default: "gateway")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.call_provider import CallProvider

logger = logging.getLogger("KrabEar.Backend.CallProviderFactory")

# Допустимые значения CALL_PROVIDER
PROVIDER_GATEWAY = "gateway"
PROVIDER_NONE = "none"
_VALID_PROVIDERS = frozenset([PROVIDER_GATEWAY, PROVIDER_NONE])


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
        Экземпляр GatewayCallProvider или NullCallProvider.
    """
    provider_name = (getattr(settings, "CALL_PROVIDER", PROVIDER_GATEWAY) or "").lower().strip()

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

    # Ветки telnyx / twilio / sip_local удалены 03.09.2026 вместе с адаптерами:
    # линия принадлежит Voice Gateway (спека
    # docs/superpowers/specs/2026-09-03-telephony-consolidation.md). Архив кода —
    # тег telephony-archive-2026-09-03.
    logger.warning(
        "CALL_PROVIDER=%r больше не поддерживается, звонки идут через Voice Gateway",
        provider_name,
    )
    return NullCallProvider()  # type: ignore[return-value]
