# Phase 4 Pipeline — Implementation Plan

Дата: 2026-04-12  
Статус: Design  
Автор: Claude (Sonnet 4.6)

Строится поверх `docs/PHASE4_DETERMINISTIC_PIPELINE.md`. Этот документ фиксирует
конкретные интерфейсы и порядок миграции.

---

## 1. PipelineContext dataclass

Единственный объект, передаваемый между стадиями. Стадия получает контекст,
изменяет своё поле, возвращает контекст. Остальные поля она не трогает.

```python
# KrabEar/core/pipeline/context.py

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np


@dataclass
class StageMetric:
    stage: str
    duration_ms: int
    skipped: bool = False
    error: Optional[str] = None


@dataclass
class PipelineContext:
    # --- Вход ---
    # numpy array (float32 16kHz mono) или Path/str к файлу
    audio_input: Any                          # np.ndarray | str | Path

    # --- Runtime параметры (инжектируются перед запуском) ---
    cleanup_profile: str = "soft"             # "soft" | "strict"
    is_preview: bool = False
    domain: str = "casual"
    lang_hint: Optional[str] = None           # ISO 639-1 или None
    extra_vocabulary: list[str] = field(default_factory=list)
    translation_mode: str = "off"

    # --- Промежуточные данные (заполняются стадиями) ---
    # AudioNormalizationStage → STTStage
    normalized_audio: Any = None              # str путь к нормализованному файлу или ndarray

    # STTStage output
    raw_text: str = ""
    segments: list[dict] = field(default_factory=list)
    language_detected: Optional[str] = None
    model_used: str = ""
    confidence: float = 0.0

    # DiarizationStage output
    diarization: dict = field(default_factory=dict)

    # TextCleanupStage output
    cleaned_text: str = ""

    # LLMRewriteStage output
    rewritten_text: str = ""
    llm_applied: bool = False
    llm_fallback_reason: Optional[str] = None
    llm_latency_ms: Optional[int] = None

    # TranslationStage output
    translation: Optional[str] = None

    # --- Финальный текст (выставляется последней стадией или executor'ом) ---
    final_text: str = ""

    # --- Мета ---
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    stage_metrics: list[StageMetric] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Временный путь (iCloud copy) — executor чистит его в finally
    _temp_path: Optional[str] = None
```

Ключевые решения:
- `np.ndarray` не оборачивается в Pydantic — горячий путь, копий нет.
- `errors` — список, не одиночное поле: стадия может добавить предупреждение,
  не останавливая pipeline.
- `stage_metrics` собирается executor'ом, не стадией.

---

## 2. PipelineStage Protocol

```python
# KrabEar/core/pipeline/base.py

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
```

`@runtime_checkable` позволяет проверять `isinstance(stage, PipelineStage)` в тестах.

---

## 3. Стадии — точные классы

### 3.1 AudioNormalizationStage

```python
# KrabEar/core/pipeline/stages/audio_normalization.py

class AudioNormalizationStage:
    name = "audio_normalization"

    def should_run(self, ctx: PipelineContext) -> bool:
        # Нормализация только для файлов, не для ndarray (live mic)
        return isinstance(ctx.audio_input, (str, Path))

    def process(self, ctx: PipelineContext) -> PipelineContext:
        # Логика из AudioEngine.normalize_audio()
        # + iCloud copy workaround (из engine.py строки 251-259)
        # ctx.normalized_audio = путь к нормализованному файлу
        # ctx._temp_path = путь к iCloud копии (если применялось)
        ...
```

Источник: `AudioEngine.normalize_audio()` + iCloud workaround блок из
`AudioEngine.transcribe()` (строки 251–259 engine.py).

---

### 3.2 STTStage

```python
# KrabEar/core/pipeline/stages/stt.py

class STTStage:
    name = "stt"

    def __init__(self, engine: AudioEngine) -> None:
        self._engine = engine

    def should_run(self, ctx: PipelineContext) -> bool:
        return True

    def process(self, ctx: PipelineContext) -> PipelineContext:
        audio = ctx.normalized_audio if ctx.normalized_audio is not None else ctx.audio_input
        prompt = self._build_prompt(ctx)
        resolved_lang = AudioEngine._resolve_language(ctx.lang_hint)
        try:
            result = self._engine._transcribe_with_fallback(
                audio, prompt=prompt, language=resolved_lang
            )
        except RuntimeError as e:
            ctx.errors.append(f"stt_failed: {e}")
            return ctx
        ctx.raw_text = str(result.get("text", "")).strip()
        ctx.segments = result.get("segments", [])
        ctx.language_detected = result.get("language", resolved_lang)
        ctx.model_used = result.get("model_used", self._engine.current_model)
        if ctx.segments:
            import numpy as np
            ctx.confidence = float(np.mean([
                np.exp(s.get("avg_logprob", -1.0)) for s in ctx.segments
            ]))
        return ctx

    def _build_prompt(self, ctx: PipelineContext) -> str:
        if ctx.is_preview:
            return ""
        from core.config import settings
        domain_desc = AudioEngine.DOMAIN_PROMPTS.get(ctx.domain, AudioEngine.DOMAIN_PROMPTS["casual"])
        prompt = f"{settings.TRANSCRIBE_PROMPT} Тематика: {domain_desc}"
        if ctx.extra_vocabulary:
            prompt += f" Ключевые слова: {', '.join(ctx.extra_vocabulary)}"
        return prompt
```

`_transcribe_with_fallback` остаётся в `AudioEngine` без изменений — stageдаёт ему правильный вход и читает выход.

---

### 3.3 DiarizationStage

```python
# KrabEar/core/pipeline/stages/diarization.py

class DiarizationStage:
    name = "diarization"

    def __init__(self, engine: AudioEngine) -> None:
        self._engine = engine

    def should_run(self, ctx: PipelineContext) -> bool:
        from core.config import settings
        return not ctx.is_preview and settings.DIARIZATION_ENABLED

    def process(self, ctx: PipelineContext) -> PipelineContext:
        audio = ctx.normalized_audio if ctx.normalized_audio is not None else ctx.audio_input
        ctx.diarization = self._engine._maybe_run_diarization(
            audio, ctx.segments, is_preview=False
        )
        return ctx
```

Делегирует в существующий `_maybe_run_diarization` — никаких изменений внутренней логики.

---

### 3.4 TextCleanupStage

```python
# KrabEar/core/pipeline/stages/text_cleanup.py

from core.utils import TextUtils

class TextCleanupStage:
    name = "text_cleanup"

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.raw_text)

    def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.cleaned_text = TextUtils.cleanup_transcript(
            ctx.raw_text, profile=ctx.cleanup_profile
        )
        return ctx
```

---

### 3.5 LLMRewriteStage

```python
# KrabEar/core/pipeline/stages/llm_rewrite.py

class LLMRewriteStage:
    name = "llm_rewrite"

    def __init__(self, rewriter: Optional[LLMRewriter], settings_get: Callable) -> None:
        self._rewriter = rewriter
        self._settings_get = settings_get

    def should_run(self, ctx: PipelineContext) -> bool:
        if self._rewriter is None:
            return False
        return bool(self._settings_get("llm_rewrite_enabled", False))

    def process(self, ctx: PipelineContext) -> PipelineContext:
        text_in = ctx.cleaned_text or ctx.raw_text
        result = self._rewriter.rewrite(text_in)
        ctx.llm_applied = result.ok
        ctx.llm_latency_ms = result.latency_ms
        if result.ok:
            ctx.rewritten_text = result.text
        else:
            ctx.llm_fallback_reason = result.fallback_reason
            ctx.rewritten_text = text_in
        return ctx
```

---

### 3.6 TranslationStage

```python
# KrabEar/core/pipeline/stages/translation.py

class TranslationStage:
    name = "translation"

    def __init__(self, translator) -> None:  # backend.translator.Translator
        self._translator = translator

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.translation_mode != "off"

    def process(self, ctx: PipelineContext) -> PipelineContext:
        text_in = ctx.rewritten_text or ctx.cleaned_text or ctx.raw_text
        try:
            ctx.translation = self._translator.translate(
                text_in, mode=ctx.translation_mode
            )
        except Exception as e:
            ctx.errors.append(f"translation_failed: {e}")
        return ctx
```

---

## 4. Pipeline Executor

```python
# KrabEar/core/pipeline/executor.py

import logging
import time
from .base import PipelineStage
from .context import PipelineContext, StageMetric

logger = logging.getLogger("KrabEar.Pipeline")


class PipelineExecutor:
    """Выполняет стадии последовательно, собирает метрики, чистит temp-файлы."""

    def __init__(self, stages: list[PipelineStage]) -> None:
        self._stages = stages

    def run(self, ctx: PipelineContext) -> PipelineContext:
        try:
            for stage in self._stages:
                if not stage.should_run(ctx):
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=0, skipped=True)
                    )
                    continue
                t0 = time.monotonic()
                try:
                    ctx = stage.process(ctx)
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=duration_ms)
                    )
                    logger.debug("Stage %s: %d ms", stage.name, duration_ms)
                except Exception as exc:
                    duration_ms = int((time.monotonic() - t0) * 1000)
                    ctx.errors.append(f"{stage.name}_exception: {exc}")
                    ctx.stage_metrics.append(
                        StageMetric(stage=stage.name, duration_ms=duration_ms, error=str(exc))
                    )
                    logger.exception("Unhandled exception in stage %s", stage.name)
                    # Продолжаем: стадия не смогла — следующие могут работать
        finally:
            self._cleanup(ctx)

        # Финальный текст: LLM rewrite > cleaned > raw
        ctx.final_text = ctx.rewritten_text or ctx.cleaned_text or ctx.raw_text
        return ctx

    def _cleanup(self, ctx: PipelineContext) -> None:
        if ctx._temp_path:
            import os
            try:
                os.unlink(ctx._temp_path)
            except OSError:
                pass
            ctx._temp_path = None

    def to_legacy_dict(self, ctx: PipelineContext) -> dict:
        """Конвертирует PipelineContext в dict-формат AudioEngine.transcribe()."""
        return {
            "text": ctx.final_text,
            "raw_text": ctx.raw_text,
            "cleaned_text": ctx.cleaned_text,
            "llm_applied": ctx.llm_applied,
            "llm_latency_ms": ctx.llm_latency_ms,
            "llm_fallback_reason": ctx.llm_fallback_reason,
            "confidence": round(ctx.confidence, 3),
            "duration_ms": sum(m.duration_ms for m in ctx.stage_metrics),
            "engine": "pipeline_v2",
            "model": ctx.model_used,
            "language": ctx.language_detected,
            "segments": ctx.segments if not ctx.is_preview else [],
            "diarization": ctx.diarization,
        }
```

`to_legacy_dict()` — точка совместимости: результат идентичен старому выводу
`AudioEngine.transcribe()`, поэтому `BackendService` не меняется.

---

## 5. Миграция из engine.py без поломки тестов

### Принцип: wrapper-first

Новый pipeline никогда не удаляет код из `engine.py` — только создаёт
тонкие обёртки. Удаление — только после снятия feature-flag.

### Шаги

**Шаг 1 — Создать файлы** (не трогая engine.py):
```
KrabEar/core/pipeline/__init__.py
KrabEar/core/pipeline/context.py
KrabEar/core/pipeline/base.py
KrabEar/core/pipeline/executor.py
KrabEar/core/pipeline/stages/__init__.py
KrabEar/core/pipeline/stages/audio_normalization.py
KrabEar/core/pipeline/stages/stt.py
KrabEar/core/pipeline/stages/diarization.py
KrabEar/core/pipeline/stages/text_cleanup.py
KrabEar/core/pipeline/stages/llm_rewrite.py
KrabEar/core/pipeline/stages/translation.py
```

**Шаг 2 — Добавить `AudioEngine.transcribe_pipeline()`** (новый метод рядом):

```python
# engine.py — добавить метод, НЕ трогая transcribe()

def transcribe_pipeline(self, ctx: PipelineContext) -> PipelineContext:
    """Pipeline-путь. Использует те же internal методы что и transcribe()."""
    from core.pipeline.executor import PipelineExecutor
    from core.pipeline.stages.audio_normalization import AudioNormalizationStage
    from core.pipeline.stages.stt import STTStage
    from core.pipeline.stages.diarization import DiarizationStage
    from core.pipeline.stages.text_cleanup import TextCleanupStage
    from core.pipeline.stages.llm_rewrite import LLMRewriteStage
    executor = PipelineExecutor([
        AudioNormalizationStage(),
        STTStage(self),
        DiarizationStage(self),
        TextCleanupStage(),
        LLMRewriteStage(self._llm_rewriter, self._settings_get),
    ])
    return executor.run(ctx)
```

**Шаг 3 — Feature flag в BackendService**:

```python
# backend/service.py — в handle_request или _do_transcribe

USE_PIPELINE_V2 = os.environ.get("KRAB_EAR_PIPELINE_V2", "0") == "1"

if USE_PIPELINE_V2:
    from core.pipeline.context import PipelineContext
    ctx = PipelineContext(audio_input=audio_data, ...)
    ctx = self.engine.transcribe_pipeline(ctx)
    result = PipelineExecutor.to_legacy_dict(ctx)  # доступно через executor
else:
    result = self.engine.transcribe(audio_data, ...)
```

**Шаг 4 — Тесты**:
- Все существующие 411 тестов продолжают тестировать `AudioEngine.transcribe()` — без изменений.
- Новые тесты (`test_pipeline_context.py`, `test_pipeline_stages.py`,
  `test_pipeline_executor.py`) работают с `PipelineContext` напрямую через fake-стадии.

**Шаг 5 — Снятие feature-flag** (после acceptance в production):
- `transcribe()` помечается `@deprecated`, вызывает `transcribe_pipeline()` внутри.
- `KRAB_EAR_PIPELINE_V2` удаляется из кода.
- Логика iCloud workaround и normalization удаляется из `transcribe()`.

---

## 6. Раскладка файлов

```
KrabEar/core/pipeline/
├── __init__.py               # экспорт PipelineContext, PipelineStage, PipelineExecutor
├── base.py                   # Protocol PipelineStage
├── context.py                # PipelineContext dataclass, StageMetric
├── executor.py               # PipelineExecutor
└── stages/
    ├── __init__.py
    ├── audio_normalization.py  # AudioNormalizationStage
    ├── stt.py                  # STTStage
    ├── diarization.py          # DiarizationStage
    ├── text_cleanup.py         # TextCleanupStage
    ├── llm_rewrite.py          # LLMRewriteStage
    └── translation.py          # TranslationStage

KrabEar/tests/
├── test_pipeline_context.py    # PipelineContext поля, StageMetric
├── test_pipeline_stages.py     # каждая стадия в изоляции (fake-engine)
└── test_pipeline_executor.py   # полная цепочка с mock-стадиями
```

`core/pipeline/` — не `backend/pipeline/`: стадии зависят от `AudioEngine` (core),
а не от `BackendService` (backend). `BackendService` импортирует из `core.pipeline`.

---

## 7. Тест-стратегия для новых стадий

Каждая стадия тестируется через `FakeAudioEngine` (уже есть паттерн в `test_backend_service.py`):

```python
# test_pipeline_stages.py — пример

class FakeEngine:
    def _transcribe_with_fallback(self, audio, prompt, language):
        return {"text": "тест", "segments": [], "language": "ru", "model_used": "balanced"}
    def _maybe_run_diarization(self, audio, segments, is_preview):
        return {"enabled": False, "speaker_segments": [], "annotated_segments": [], "speaker_turns": []}
    current_model = "balanced"
    quality_profile = "balanced"
    DOMAIN_PROMPTS = AudioEngine.DOMAIN_PROMPTS

class STTStageTest(unittest.TestCase):
    def test_fills_raw_text(self):
        ctx = PipelineContext(audio_input=np.zeros(16000, dtype=np.float32))
        stage = STTStage(engine=FakeEngine())
        out = stage.process(ctx)
        self.assertEqual(out.raw_text, "тест")
        self.assertEqual(out.model_used, "balanced")
```

---

## Definition of Done для этого плана

- [ ] `core/pipeline/` создан, все 6 стадий реализованы
- [ ] `AudioEngine.transcribe_pipeline()` добавлен, `transcribe()` не изменён
- [ ] `KRAB_EAR_PIPELINE_V2=1` активирует новый путь в `BackendService`
- [ ] `PipelineExecutor.to_legacy_dict()` возвращает dict, идентичный старому
- [ ] 411 существующих тестов зелёные
- [ ] ≥ 20 новых тестов покрывают pipeline-слой
- [ ] `TranslationStage` подключена к существующему `Translator` из `backend/translator.py`
