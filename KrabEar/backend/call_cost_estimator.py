"""Оценщик стоимости телефонных звонков для Phase 3 Call Automation.

Табличные тарифы для провайдеров Telnyx / Twilio / LiveKit.
Предупреждение пользователя если текущая стоимость звонка превышает порог.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("KrabEar.Backend.CallCostEstimator")

# ---------------------------------------------------------------------------
# Rates table: (provider, destination_country) → USD per minute
# Sources: public pricing pages (2026-04)
# ---------------------------------------------------------------------------

# Telnyx: https://telnyx.com/pricing/voice
_TELNYX_RATES: dict[str, float] = {
    "us": 0.004,
    "ca": 0.004,
    "gb": 0.008,
    "de": 0.010,
    "fr": 0.012,
    "ru": 0.025,
    "mx": 0.018,
    "es": 0.012,
    "it": 0.012,
    "au": 0.012,
    "jp": 0.018,
    "cn": 0.040,
    "br": 0.022,
    "in": 0.010,
    "default": 0.035,
}

# Twilio: https://www.twilio.com/en-us/voice/pricing
_TWILIO_RATES: dict[str, float] = {
    "us": 0.0140,
    "ca": 0.0140,
    "gb": 0.0200,
    "de": 0.0220,
    "fr": 0.0200,
    "ru": 0.0490,
    "mx": 0.0380,
    "es": 0.0190,
    "it": 0.0190,
    "au": 0.0240,
    "jp": 0.0370,
    "cn": 0.0700,
    "br": 0.0400,
    "in": 0.0190,
    "default": 0.0600,
}

# LiveKit: WebRTC-based, media relay costs per minute
_LIVEKIT_RATES: dict[str, float] = {
    "us": 0.001,
    "ca": 0.001,
    "gb": 0.002,
    "de": 0.002,
    "fr": 0.002,
    "ru": 0.004,
    "mx": 0.003,
    "es": 0.002,
    "it": 0.002,
    "au": 0.002,
    "jp": 0.003,
    "cn": 0.006,
    "br": 0.004,
    "in": 0.002,
    "default": 0.005,
}

# Local SIP: On-Device / Zero-Cloud Cost (прямой SIP-транк)
_SIP_LOCAL_RATES: dict[str, float] = {
    "default": 0.0,
}

_PROVIDER_TABLES: dict[str, dict[str, float]] = {
    "telnyx": _TELNYX_RATES,
    "twilio": _TWILIO_RATES,
    "livekit": _LIVEKIT_RATES,
    "sip_local": _SIP_LOCAL_RATES,
}

# Предупреждение: running cost превышает этот порог в USD
WARN_THRESHOLD_USD = 5.0


class CallCostEstimator:
    """Оценщик стоимости звонка по провайдеру и стране назначения."""

    def estimate_minute_cost(
        self,
        provider: str,
        destination_country: str,
    ) -> float:
        """Возвращает стоимость одной минуты в USD.

        Args:
            provider: 'telnyx', 'twilio' или 'livekit' (case-insensitive).
            destination_country: ISO 3166-1 alpha-2 код страны (case-insensitive).

        Returns:
            float: стоимость минуты в USD. Возвращает тариф 'default' для
            неизвестных стран или провайдеров.
        """
        provider_key = provider.lower().strip()
        country_key = destination_country.lower().strip()

        table = _PROVIDER_TABLES.get(provider_key)
        if table is None:
            logger.warning(
                "Неизвестный провайдер '%s', используем telnyx как fallback",
                provider,
            )
            table = _TELNYX_RATES

        rate = table.get(country_key, table["default"])
        logger.debug(
            "Тариф: провайдер=%s страна=%s → %.4f USD/мин",
            provider_key,
            country_key,
            rate,
        )
        return rate

    def should_warn_user(
        self,
        current_duration_sec: float,
        hourly_rate_usd: float,
    ) -> bool:
        """Возвращает True если текущая накопленная стоимость превышает WARN_THRESHOLD_USD.

        Args:
            current_duration_sec: длительность звонка в секундах.
            hourly_rate_usd: тариф в USD/час (= minute_rate * 60).

        Returns:
            bool: True если running cost > $5.
        """
        if current_duration_sec <= 0 or hourly_rate_usd <= 0:
            return False
        running_cost = (current_duration_sec / 3600.0) * hourly_rate_usd
        warn = running_cost > WARN_THRESHOLD_USD
        if warn:
            logger.info(
                "Предупреждение о стоимости: %.2f USD за %.0f сек",
                running_cost,
                current_duration_sec,
            )
        return warn

    def running_cost_usd(
        self,
        current_duration_sec: float,
        provider: str,
        destination_country: str,
    ) -> float:
        """Рассчитывает текущую накопленную стоимость в USD.

        Args:
            current_duration_sec: длительность звонка в секундах.
            provider: провайдер звонка.
            destination_country: страна назначения.

        Returns:
            float: текущая стоимость в USD.
        """
        minute_rate = self.estimate_minute_cost(provider, destination_country)
        return (current_duration_sec / 60.0) * minute_rate

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_estimate_cost(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: call_estimate_cost — вернуть тариф и running cost.

        Params:
            provider (str): провайдер звонка.
            destination (str): страна назначения (ISO alpha-2).
            duration_sec (float, optional): текущая длительность для running cost.
        """
        provider = str(params.get("provider", "telnyx")).strip()
        destination = str(params.get("destination", "us")).strip()
        # wave-1770 HIGH: NaN/Inf duration propagates to running_cost_usd → JSON NaN →
        # Swift Decodable crash. Guard here before any arithmetic.
        _raw_dur = float(params.get("duration_sec", 0))
        duration_sec = _raw_dur if math.isfinite(_raw_dur) and _raw_dur >= 0 else 0.0

        minute_rate = self.estimate_minute_cost(provider, destination)
        hourly_rate = minute_rate * 60.0
        running_cost = self.running_cost_usd(duration_sec, provider, destination)
        warn = self.should_warn_user(duration_sec, hourly_rate)

        return {
            "ok": True,
            "result": {
                "provider": provider,
                "destination": destination,
                "minute_rate_usd": minute_rate,
                "hourly_rate_usd": hourly_rate,
                "running_cost_usd": running_cost,
                "warn_threshold_usd": WARN_THRESHOLD_USD,
                "should_warn": warn,
            },
        }
