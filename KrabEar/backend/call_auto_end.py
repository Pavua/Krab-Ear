"""Комбайнер правил автоматического завершения звонка (Phase 3 step 2/4).

Объединяет: max_duration, silence probe, operator-silence-after-interruption.
Возвращает should_end + reason через handle_check_auto_end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.call_cost_estimator import CallCostEstimator
from backend.call_silence_probe import CallSilenceProbe

logger = logging.getLogger("KrabEar.Backend.CallAutoEnd")

# ---------------------------------------------------------------------------
# Константы правил
# ---------------------------------------------------------------------------

MAX_DURATION_DEFAULT_SEC: int = 1800          # 30 минут
SILENCE_PROBE_TRIGGER_SEC: float = 10.0       # тишина 10 сек → probe
OPERATOR_SILENT_AFTER_INTERRUPTION_SEC: float = 15.0  # 15 сек → end

# Ключи reason
REASON_MAX_DURATION = "max_duration"
REASON_SILENCE_CONFIRMED = "silence_confirmed"
REASON_OPERATOR_SILENT = "operator_silent_after_interruption"
REASON_COST_LIMIT = "cost_limit"


@dataclass
class AutoEndResult:
    """Результат проверки правил автоматического завершения звонка."""

    should_end: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_end": self.should_end,
            "reason": self.reason,
            "details": self.details,
        }


class CallAutoEnd:
    """Проверяет набор правил и возвращает решение о завершении звонка."""

    def __init__(
        self,
        cost_estimator: CallCostEstimator | None = None,
        silence_probe: CallSilenceProbe | None = None,
        max_duration_sec: int = MAX_DURATION_DEFAULT_SEC,
        silence_trigger_sec: float = SILENCE_PROBE_TRIGGER_SEC,
        operator_silent_sec: float = OPERATOR_SILENT_AFTER_INTERRUPTION_SEC,
        cost_warn_threshold_usd: float = 5.0,
    ) -> None:
        self._cost_estimator = cost_estimator or CallCostEstimator()
        self._silence_probe = silence_probe or CallSilenceProbe()
        self.max_duration_sec = max_duration_sec
        self.silence_trigger_sec = silence_trigger_sec
        self.operator_silent_sec = operator_silent_sec
        self.cost_warn_threshold_usd = cost_warn_threshold_usd

    # ------------------------------------------------------------------
    # Rule checkers
    # ------------------------------------------------------------------

    def _check_max_duration(self, current_duration_sec: float) -> AutoEndResult | None:
        """Правило 1: превышение максимальной длительности."""
        if current_duration_sec >= self.max_duration_sec:
            logger.info(
                "Правило MAX_DURATION: %.0f >= %d сек",
                current_duration_sec,
                self.max_duration_sec,
            )
            return AutoEndResult(
                should_end=True,
                reason=REASON_MAX_DURATION,
                details={
                    "current_duration_sec": current_duration_sec,
                    "max_duration_sec": self.max_duration_sec,
                },
            )
        return None

    def _check_silence(
        self,
        silence_duration_sec: float,
        after_interruption: bool,
    ) -> AutoEndResult | None:
        """Правила 2 и 3: тишина (probe или после прерывания)."""
        if after_interruption and silence_duration_sec >= self.operator_silent_sec:
            logger.info(
                "Правило OPERATOR_SILENT: %.0f сек тишины после прерывания",
                silence_duration_sec,
            )
            return AutoEndResult(
                should_end=True,
                reason=REASON_OPERATOR_SILENT,
                details={
                    "silence_duration_sec": silence_duration_sec,
                    "operator_silent_threshold_sec": self.operator_silent_sec,
                },
            )
        if silence_duration_sec >= self.silence_trigger_sec:
            logger.info(
                "Правило SILENCE_PROBE: %.0f сек тишины → probe",
                silence_duration_sec,
            )
            return AutoEndResult(
                should_end=True,
                reason=REASON_SILENCE_CONFIRMED,
                details={
                    "silence_duration_sec": silence_duration_sec,
                    "trigger_threshold_sec": self.silence_trigger_sec,
                },
            )
        return None

    def _check_cost(
        self,
        current_duration_sec: float,
        provider: str,
        destination_country: str,
    ) -> AutoEndResult | None:
        """Правило 4: превышение стоимостного порога."""
        if not provider or not destination_country:
            return None
        minute_rate = self._cost_estimator.estimate_minute_cost(
            provider, destination_country
        )
        hourly_rate = minute_rate * 60.0
        warn = self._cost_estimator.should_warn_user(
            current_duration_sec, hourly_rate
        )
        if warn:
            running_cost = self._cost_estimator.running_cost_usd(
                current_duration_sec, provider, destination_country
            )
            logger.info(
                "Правило COST_LIMIT: %.2f USD превышает %.2f USD",
                running_cost,
                self.cost_warn_threshold_usd,
            )
            return AutoEndResult(
                should_end=True,
                reason=REASON_COST_LIMIT,
                details={
                    "running_cost_usd": running_cost,
                    "warn_threshold_usd": self.cost_warn_threshold_usd,
                    "provider": provider,
                    "destination_country": destination_country,
                },
            )
        return None

    def evaluate(
        self,
        current_duration_sec: float,
        silence_duration_sec: float = 0.0,
        after_interruption: bool = False,
        provider: str = "",
        destination_country: str = "",
    ) -> AutoEndResult:
        """Оценивает все правила и возвращает первое срабатывание.

        Порядок проверки: max_duration → cost → operator_silent → silence_probe.

        Args:
            current_duration_sec: общая длительность звонка в секундах.
            silence_duration_sec: длина текущего окна тишины в секундах.
            after_interruption: True если тишина идёт после interruption-события.
            provider: провайдер для cost check ('telnyx'/'twilio'/'livekit').
            destination_country: ISO alpha-2 страна для cost check.

        Returns:
            AutoEndResult: should_end=True при первом срабатывании, иначе False.
        """
        result = self._check_max_duration(current_duration_sec)
        if result:
            return result

        result = self._check_cost(current_duration_sec, provider, destination_country)
        if result:
            return result

        result = self._check_silence(silence_duration_sec, after_interruption)
        if result:
            return result

        return AutoEndResult(should_end=False)

    # ------------------------------------------------------------------
    # IPC handler
    # ------------------------------------------------------------------

    def handle_check_auto_end(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: call_check_auto_end — проверить правила для сессии.

        Params:
            session_id (str): идентификатор сессии (логирование).
            current_state (dict): текущее состояние звонка:
                - duration_sec (float): длительность звонка.
                - silence_sec (float, optional): текущее окно тишины.
                - after_interruption (bool, optional): тишина после прерывания.
                - provider (str, optional): провайдер.
                - destination_country (str, optional): страна назначения.
        """
        session_id = str(params.get("session_id", "unknown"))
        state = params.get("current_state", {})
        if not isinstance(state, dict):
            state = {}

        duration_sec = float(state.get("duration_sec", 0))
        silence_sec = float(state.get("silence_sec", 0))
        after_interruption = bool(state.get("after_interruption", False))
        provider = str(state.get("provider", ""))
        destination_country = str(state.get("destination_country", ""))

        result = self.evaluate(
            current_duration_sec=duration_sec,
            silence_duration_sec=silence_sec,
            after_interruption=after_interruption,
            provider=provider,
            destination_country=destination_country,
        )

        logger.debug(
            "check_auto_end session=%s should_end=%s reason=%s",
            session_id,
            result.should_end,
            result.reason,
        )

        return {
            "ok": True,
            "result": result.to_dict(),
        }
