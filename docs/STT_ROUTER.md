# STT Language-Aware Router

Этот документ описывает архитектуру language-aware STT router'а в Krab Ear.
Scaffold реализован в PR "feat: STT language-aware router scaffold (no integration yet)".
Реальная интеграция в `AudioEngine.transcribe()` запланирована на follow-up PR
после завершения research по RU-специализированным STT моделям.

## Мотивация

Пользователь говорит 80%+ на русском. Whisper-large-v3 — generalist-модель:
на русском языке её превосходят русско-специализированные модели
(GigaAM-CTC, Parakeet-RU fine-tunes, и другие). Цель router'а — автоматически
выбирать лучшую модель под язык входящего аудио, без изменений для пользователя.

## Архитектура

```
AudioEngine.transcribe(audio_data, ...)
        │
        ▼  (follow-up PR)
STTRouter.select_model(audio_data, sample_rate, hint_language)
        │
        ├── routing disabled?  → STT_OTHER_PRIMARY_MODEL (generalist, обратная совместимость)
        │
        ├── hint_language задан? → напрямую в _lang_to_model()
        │
        └── audio detection   → _resolve_language() → _lang_to_model()
                                   (placeholder: "ru" для ненулевого аудио)
                                   (follow-up: реальный audio-level LID)
```

### Файлы

| Файл | Описание |
|---|---|
| `KrabEar/core/stt_router.py` | Основной класс `STTRouter` |
| `KrabEar/core/config.py` | Новые settings: `STT_LANGUAGE_ROUTING_ENABLED`, `STT_*_PRIMARY_MODEL` |
| `KrabEar/tests/test_stt_router.py` | 14+ unit тестов |
| `KrabEar/core/engine.py` | Заглушка `self._router = None` в `AudioEngine.__init__` |

### Конфиг-параметры

Все параметры переопределяются через `KRAB_EAR_*` env vars:

```
STT_LANGUAGE_ROUTING_ENABLED=false   # мастер-переключатель (off until model chosen)
STT_RU_PRIMARY_MODEL=mlx-community/whisper-large-v3-mlx
STT_EN_PRIMARY_MODEL=mlx-community/whisper-large-v3-mlx
STT_ES_PRIMARY_MODEL=mlx-community/whisper-large-v3-mlx
STT_OTHER_PRIMARY_MODEL=mlx-community/whisper-large-v3-mlx
```

### Маппинг языков

| ISO 639-1 | Атрибут конфига |
|---|---|
| `ru` | `STT_RU_PRIMARY_MODEL` |
| `uk` | `STT_RU_PRIMARY_MODEL` (ближайшая модель) |
| `en` | `STT_EN_PRIMARY_MODEL` |
| `es` | `STT_ES_PRIMARY_MODEL` |
| всё остальное | `STT_OTHER_PRIMARY_MODEL` |

## Где уже есть adapter pattern

Pipeline-адаптеры для STT находятся в `KrabEar/core/pipeline/`:

```
core/pipeline/
    stt_whisper.py         — MLX whisper-large-v3 (основной)
    stt_sensevoice.py      — FunASR SenseVoice (50+ языков)
    stt_parakeet.py        — NVIDIA Parakeet-TDT (EN)
    stt_whisperx.py        — WhisperX (word timestamps)
    stt_voxtral.py         — Mistral Voxtral-Mini (multilingual + reasoning)
```

Каждый адаптер реализует интерфейс `BasePipelineStage`:
- `process(audio_data, context) -> StageResult`
- Мягкий fallback при `ImportError` (библиотека не установлена)

Для регистрации нового адаптера в router'е — см. раздел ниже.

## Как добавить новую RU-специализированную модель

После того как research (`/tmp/krab-ear-research/ru_stt_models_2026-04-25.md`)
выберет конкретную модель, выполнить 5 шагов:

### Шаг 1 — Создать адаптер

```python
# KrabEar/core/pipeline/stt_gigaam.py
from core.pipeline.base import BasePipelineStage, StageResult

class GigaAMAdapter(BasePipelineStage):
    """GigaAM-CTC — RU-специализированный STT от SberDevices."""

    def process(self, audio_data, context):
        try:
            import gigaam  # или другой пакет модели
            # ... inference ...
            return StageResult(text=text, confidence=conf)
        except ImportError:
            return StageResult(error="gigaam_not_installed")
```

### Шаг 2 — Зарегистрировать в adapter_factory

В `BackendService.__init__` или отдельном `STTAdapterRegistry`:

```python
from core.pipeline.stt_gigaam import GigaAMAdapter

_ADAPTER_REGISTRY = {
    "gigaam-ctc": GigaAMAdapter,
    "mlx-community/whisper-large-v3-mlx": WhisperMLXAdapter,
    # ...
}

def _adapter_factory(model_id: str):
    cls = _ADAPTER_REGISTRY.get(model_id)
    if cls is None:
        raise ValueError(f"Unknown model: {model_id}")
    return cls.get_or_load()
```

### Шаг 3 — Изменить default в конфиге

```python
# KrabEar/core/config.py
STT_RU_PRIMARY_MODEL: str = "gigaam-ctc"   # был: mlx-community/whisper-large-v3-mlx
```

Или через env var без изменения кода:
```bash
export KRAB_EAR_STT_RU_PRIMARY_MODEL="gigaam-ctc"
```

### Шаг 4 — Включить routing

```bash
export KRAB_EAR_STT_LANGUAGE_ROUTING_ENABLED=true
```

Или в `~/.krab_ear_data/.env`:
```
KRAB_EAR_STT_LANGUAGE_ROUTING_ENABLED=true
KRAB_EAR_STT_RU_PRIMARY_MODEL=gigaam-ctc
```

### Шаг 5 — Интегрировать router в AudioEngine.transcribe()

```python
# KrabEar/core/engine.py — в follow-up PR
# Заменить: self._router = None
# На:
from core.stt_router import STTRouter
self._router = STTRouter(settings, adapter_factory=_adapter_factory)

# В transcribe():
if self._router is not None:
    model_id = self._router.select_model(
        audio_data=audio_data,
        sample_rate=sample_rate,
        hint_language=settings.TRANSCRIBE_LANGUAGE,
    )
    # использовать model_id для выбора адаптера
```

## Audio-level Language Detection (будущее)

Текущий placeholder в `_resolve_language()` возвращает `"ru"` для любого
ненулевого аудио (пользователь говорит 80%+ по-русски — статистически лучший guess).

Для реального audio-level LID (когда появится потребность):
- **Вариант A**: короткий whisper inference на первых 3 секундах с `task="transcribe"`
  → из ответа брать `language` поле (Whisper умеет определять язык).
- **Вариант B**: FastLangDetect / lingua-py на транскрипте первых 5 секунд.
- **Вариант C**: silero-VAD + audio embedding similarity (если появятся компактные модели).

## Ссылки

- Research backlog: см. memory `[Research backlog 2026-04]` в MEMORY.md
- STT adapters (Phase 4): `docs/archive/2026-05-26-pre-marathon/PHASE_4_ADAPTER_COMPARISON.md` (archived; Phase 4 shipped)
- IPC API Reference: `docs/IPC_API_REFERENCE.md`
- Конфиг: `KrabEar/core/config.py` — секция `STT Language-Aware Router`
