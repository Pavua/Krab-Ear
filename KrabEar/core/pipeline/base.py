"""PipelineStage Protocol — контракт для всех стадий pipeline."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from .context import PipelineContext


@runtime_checkable
class PipelineStage(Protocol):
    """Контракт стадии pipeline.

    Каждая стадия:
    - получает контекст, изменяет свои поля, возвращает его;
    - НИКОГДА не поднимает исключений — soft-fail через ctx.errors.append();
    - логирует через собственный logger (не print).
    """

    @property
    def name(self) -> str:
        """Имя стадии для метрик и логов."""
        ...

    def should_run(self, ctx: PipelineContext) -> bool:
        """Нужно ли запускать стадию для данного контекста.

        Позволяет skip без изменения executor'а. Например, DiarizationStage
        возвращает False при is_preview=True.
        """
        ...

    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Выполняет обработку. Изменяет ctx inplace, возвращает его."""
        ...
