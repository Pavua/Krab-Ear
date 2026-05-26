# Phase 4: Deterministic Pipeline

Дата: 2026-04-12  
Статус: Proposal  
Автор: Claude (Sonnet 4.6)

---

## Vision

Krab Ear как детерминированный, композируемый pipeline для обработки аудио:

```
Audio Source → Ingest → STT → Language Detect → Chunking → Timeline Events → Output
```

Каждый этап — независимый, тестируемый, заменяемый блок. Результат одного этапа — валидный вход для следующего. Сбой в одном этапе не ломает весь поток — есть явные fallback-стратегии на каждом уровне.

Цель фазы: устранить монолитность `AudioEngine.transcribe()` (~500 строк) и превратить его в цепочку явных, верифицируемых стадий, которая масштабируется на новые источники аудио (звонки, стриминг, batch) без дублирования логики.

---

## Current State

### Что уже работает

| Аспект | Реализация |
|---|---|
| Audio ingest | `AudioRecorder` (mic) + file import с iCloud workaround (errno 11, copy to /tmp) |
| STT | `mlx-whisper` через `AudioEngine.transcribe()`, fallback chain: balanced → max candidates → remote |
| Language | `lang_hint` параметр (ISO 639-1), авто-определение через whisper если `None`/`"auto"` |
| Diarization | `pyannote.audio` на Metal GPU, опционально, soft-fail при ошибке |
| Chunking | Отсутствует — файл обрабатывается целиком |
| Timeline | Только Call Assist (отдельный поток) |
| Events | `EventBus` + SSE, EVENT_CONTRACT_V1, типы в `contracts/registry.py` |
| Output | Paste (accessibility) + StateStore NDJSON + .md transcripts + SRT |

### Что мешает масштабированию

1. `AudioEngine.transcribe()` — монолит: нормализация, STT, cleanup, diarization, перевод — всё в одном методе.
2. Нет явного контракта между стадиями — промежуточный результат не типизирован.
3. Chunking отсутствует: длинные аудио (>10 мин) обрабатываются как один блок → память, латентность, нет прогресса.
4. Language detection — side-effect внутри STT, не отдельная стадия.
5. Timeline events генерируются ad-hoc в `BackendService`, не pipeline-driven.

---

## Proposed Pipeline Architecture

### Контракт между стадиями

```python
@dataclass
class AudioChunk:
    id: str                  # uuid
    source_id: str           # идентификатор источника (file path, mic session id)
    audio_data: np.ndarray   # float32, 16kHz, mono
    sample_rate: int         # всегда 16000 после Ingest
    duration_sec: float
    offset_sec: float        # смещение в исходном файле/потоке
    metadata: dict           # произвольные поля (device, icloud_path, call_id, ...)

@dataclass
class STTResult:
    chunk_id: str
    text: str
    segments: list[dict]     # whisper-формат: {start, end, text, avg_logprob, ...}
    confidence: float        # среднее exp(avg_logprob) по сегментам
    language: str            # ISO 639-1, определённый whisper
    model_used: str          # "balanced", "max", "remote"
    duration_ms: int

@dataclass
class TimelineEvent:
    chunk_id: str
    speaker: str | None      # "SPEAKER_0", "SPEAKER_1", None если diarization выкл
    text: str
    lang: str
    confidence: float
    ts_start: float          # секунды от начала источника
    ts_end: float
    translation: str | None  # если translation_mode != "off"
```

---

### Stage 1: Ingest

**Ответственный модуль:** новый `backend/ingest.py`

**Задача:** унифицировать все источники аудио в поток `AudioChunk`.

```
Mic (sounddevice)   ─┐
File (mp3/m4a/wav)  ─┤→ IngestAdapter → AudioChunk stream (16kHz, float32, mono)
Call stream         ─┤
iCloud file         ─┘
```

**Конкретные deliverables:**

- `IngestAdapter` ABC с методом `read_chunks(chunk_duration_sec: float) -> Iterator[AudioChunk]`
- `MicIngestAdapter` — обёртка над существующим `AudioRecorder`
- `FileIngestAdapter` — ffmpeg нормализация + iCloud copy workaround (сейчас раскидан по `engine.py` и `service.py`)
- Извлечение метаданных из ffprobe: длительность, sample rate, каналы, codec
- Нормализация громкости (текущая логика из `engine.py: _normalize_audio`) переносится сюда

**Что НЕ меняется:** `AudioRecorder` остаётся как есть, `MicIngestAdapter` просто оборачивает его.

---

### Stage 2: STT

**Ответственный модуль:** рефакторинг `core/engine.py`

**Задача:** принять `AudioChunk`, вернуть `STTResult`.

**Конкретные deliverables:**

- `AudioEngine.transcribe_chunk(chunk: AudioChunk) -> STTResult` — новая публичная точка входа
- Существующий fallback chain (balanced → max → remote) остаётся без изменений
- `_unavailable_models` set сохраняется
- Confidence score уже вычисляется (`exp(avg_logprob)`), нужно только перенести в `STTResult`
- Hallucination cleanup (`TextUtils.cleanup_soft/strict`) переносится из `transcribe()` в отдельный post-processor (Stage 2.5)
- Обратная совместимость: старый `transcribe(audio_data, ...)` остаётся как deprecated wrapper

---

### Stage 3: Language Detection

**Ответственный модуль:** новый `backend/lang_detector.py`

**Задача:** определить язык сегмента из данных whisper, проставить тег к каждому `STTResult`.

**Текущее состояние:** whisper уже возвращает `language` в результате — но это поле не используется явно, buried в `result` dict.

**Конкретные deliverables:**

- `LanguageDetector.detect(stt_result: STTResult) -> str` — извлекает `language` из whisper output
- Per-segment language tagging: если в `segments` разные `language` поля — мультиязычный документ
- `lang_hint` остаётся как override (пользователь знает лучше)
- Мультиязычный режим: `bilingual_ru_es` получает split сегментов по языку вместо одного блока

**Почему важно:** без явной стадии language detect нельзя добавить per-segment перевод в chunking-режиме.

---

### Stage 4: Chunking

**Ответственный модуль:** новый `backend/chunker.py`

**Задача:** разбить `STTResult` (или поток `STTResult` от длинного файла) на логические единицы.

**Конкретные deliverables:**

- `SentenceChunker` — разбивка по `.?!` с учётом языка (RU/ES пунктуация)
- `SpeakerTurnChunker` — граница чанка = смена спикера (из diarization `annotated_segments`)
- `SilenceChunker` — граница по паузе >N сек (из whisper segments `start`/`end` gaps)
- `OverlapMerger` — для стримингового режима: склеить хвост предыдущего чанка с началом нового

**Стратегия выбора chunker:**

```python
# Автоматически: если diarization включён → SpeakerTurnChunker первым
# Fallback: SentenceChunker
# Для длинных файлов (>5 мин): SilenceChunker как первичный split, SentenceChunker внутри
```

**Почему важно для core-сценария:** chunking нужен не только для длинных файлов — он даёт промежуточный прогресс и позволяет начать перевод/вставку не дожидаясь конца транскрибации.

---

### Stage 5: Timeline Events

**Ответственный модуль:** существующий `backend/event_bus.py` + новый `backend/timeline_builder.py`

**Задача:** преобразовать поток чанков в `TimelineEvent` и опубликовать в EventBus.

**Конкретные deliverables:**

- `TimelineBuilder.emit(chunk: STTResult, diarization: dict) -> list[TimelineEvent]`
- Интеграция с существующим `EventBus.emit_typed()` — новый `EventType.STT_TIMELINE_EVENT`
- Контракт в `contracts/registry.py`: `TimelineEventPayload` Pydantic-модель
- SSE streaming к подключённым клиентам без изменений `rest_server.py`
- Call Assist получает timeline events вместо собственного парсинга

**Формат события (расширение EVENT_CONTRACT_V1):**

```json
{
  "type": "stt.timeline_event",
  "ts": 1712345678.0,
  "data": {
    "chunk_id": "uuid",
    "speaker": "SPEAKER_0",
    "text": "Добрый день",
    "lang": "ru",
    "confidence": 0.87,
    "ts_start": 12.4,
    "ts_end": 14.1,
    "translation": null
  }
}
```

---

### Stage 6: Output

**Ответственный модуль:** существующие + новые выходы

| Выход | Статус | Изменение |
|---|---|---|
| Paste (accessibility) | Существует | Без изменений |
| History (StateStore NDJSON) | Существует | Добавить `timeline_events[]` в запись |
| SRT subtitles | Существует | Генерировать из `TimelineEvent` вместо raw segments |
| Markdown transcripts | Существует | Добавить speaker turns из timeline |
| **JSON export** | Новое | `export_transcript(id) -> {chunks, timeline, metadata}` IPC-метод |
| **Webhook/API push** | Новое | POST на configurable URL при завершении транскрибации |

**JSON export schema:**

```json
{
  "id": "session-uuid",
  "source": "file.m4a",
  "duration_sec": 127.4,
  "language": "ru",
  "created_at": "2026-04-12T10:00:00Z",
  "timeline": [
    {"speaker": "SPEAKER_0", "text": "...", "ts_start": 0.0, "ts_end": 3.2, "lang": "ru", "confidence": 0.91}
  ],
  "full_text": "..."
}
```

---

## Implementation Plan

### Спринт S-P4A: Foundation (2-3 сессии)

**Цель:** определить контракты, не ломая ничего существующего.

- [ ] Создать `backend/pipeline_types.py`: `AudioChunk`, `STTResult`, `TimelineEvent` dataclasses
- [ ] Создать `backend/ingest.py`: `IngestAdapter` ABC + `FileIngestAdapter` (перенос iCloud workaround из `service.py`)
- [ ] Добавить `AudioEngine.transcribe_chunk(chunk: AudioChunk) -> STTResult` (тонкая обёртка над существующим кодом)
- [ ] Тесты: `test_pipeline_types.py` — валидация контрактов, `test_ingest.py` — FileIngestAdapter с temp файлами

**Acceptance:** существующие 264+ тестов зелёные, новые тесты проходят.

---

### Спринт S-P4B: Language + Chunking (2-3 сессии)

**Цель:** добавить language detection и базовый chunking без изменения core flow.

- [ ] Создать `backend/lang_detector.py`: `LanguageDetector` с extract из whisper output
- [ ] Создать `backend/chunker.py`: `SentenceChunker` + `SpeakerTurnChunker`
- [ ] Интегрировать в `BackendService.handle_transcribe()` под feature-flag `KRAB_EAR_PIPELINE_V2=1`
- [ ] Per-segment language tagging в `bilingual_ru_es` mode
- [ ] Тесты: `test_lang_detector.py`, `test_chunker.py`

**Acceptance:** при `PIPELINE_V2=0` поведение идентично pre-P4. При `PIPELINE_V2=1` — chunking виден в `segments[]` ответа.

---

### Спринт S-P4C: Timeline Events (2 сессии)

**Цель:** wire timeline events в EventBus, обновить Call Assist.

- [ ] Добавить `EventType.STT_TIMELINE_EVENT` в `contracts/registry.py`
- [ ] Создать `TimelineEventPayload` Pydantic модель в `contracts/`
- [ ] Создать `backend/timeline_builder.py`
- [ ] Обновить `BackendService` — вместо ad-hoc emit использовать `TimelineBuilder`
- [ ] Call Assist подписывается на `STT_TIMELINE_EVENT` вместо polling
- [ ] Тесты: `test_timeline_builder.py`

**Acceptance:** Call Assist получает events через EventBus, SSE клиенты видят timeline stream.

---

### Спринт S-P4D: Output Expansion (1-2 сессии)

**Цель:** JSON export + webhook.

- [ ] IPC метод `export_transcript_json(id: str) -> dict` в `BackendService`
- [ ] Webhook: `KRAB_EAR_WEBHOOK_URL` env var, POST при завершении транскрибации
- [ ] GUI: кнопка "Экспорт JSON" в history panel (Swift)
- [ ] Тесты: `test_export.py`

**Acceptance:** `export_transcript_json` возвращает валидный JSON с timeline.

---

### Спринт S-P4E: Cleanup + Feature Flag Removal (1 сессия)

**Цель:** снять feature-flag, удалить дублирующий код из `AudioEngine.transcribe()`.

- [ ] Снять `KRAB_EAR_PIPELINE_V2` flag, pipeline v2 — единственный путь
- [ ] Deprecated wrapper `transcribe(audio_data, ...)` оставить с warning
- [ ] Удалить дублирующую логику iCloud workaround из `service.py`
- [ ] Обновить `ARCHITECTURE.md` и `API.md`

**Acceptance:** `ARCHITECTURE.md` описывает pipeline stages, все тесты зелёные.

---

## Migration Strategy

### Обратная совместимость

- Все существующие IPC методы (`transcribe`, `transcribe_file`, `start_recording`, ...) остаются без изменений в сигнатуре.
- Внутренне они переходят на pipeline stages — это детали реализации.
- `AudioEngine.transcribe(audio_data, ...)` остаётся как deprecated wrapper минимум до конца Phase 4.

### Feature Flag

`KRAB_EAR_PIPELINE_V2` (default: `0`) — при `1` активирует новый pipeline path в `BackendService`. Позволяет A/B сравнение и безопасный rollback.

### История

Существующие записи в `history.ndjson` не мигрируются — поле `timeline_events` появляется только в новых записях. Старые записи читаются без изменений (NDJSON, tombstone model).

---

## Non-goals

- **Real-time streaming STT** — это Voice Gateway. Krab Ear pipeline работает с завершёнными чанками, не с live байтами.
- **Video processing** — только аудиодорожка, ffmpeg извлечение остаётся в Ingest.
- **Multi-machine distributed pipeline** — всё локально на одном Mac.
- **Изменение формата хранения StateStore** — NDJSON остаётся, `timeline_events` добавляется как новое поле без миграции.
- **Новый движок STT** — mlx-whisper остаётся, fallback chain не меняется.

---

## Risks и Mitigation

| Риск | Вероятность | Mitigation |
|---|---|---|
| Рефакторинг `engine.py` ломает существующие тесты | Средняя | Feature flag + deprecated wrapper + тесты запускаются на каждом спринте |
| `AudioChunk` dataclass несовместим с существующими типами numpy/sounddevice | Низкая | Используем `np.ndarray` напрямую, без Pydantic в hot path |
| Chunking увеличивает латентность core-сценария (диктовка <30 сек) | Низкая | Для коротких записей chunker возвращает один чанк = нет overhead |
| Webhook добавляет сетевую зависимость | Низкая | Webhook opt-in через env var, ошибки логируются и игнорируются |

---

## Definition of Done (Phase 4)

- [ ] `backend/pipeline_types.py`, `backend/ingest.py`, `backend/chunker.py`, `backend/lang_detector.py`, `backend/timeline_builder.py` созданы
- [ ] `AudioEngine.transcribe_chunk()` принимает `AudioChunk`, возвращает `STTResult`
- [ ] `EventType.STT_TIMELINE_EVENT` в контрактах, SSE клиенты получают timeline stream
- [ ] IPC метод `export_transcript_json` работает
- [ ] Все 264+ существующих тестов зелёные
- [ ] Минимум 20 новых тестов покрывают pipeline stages
- [ ] `ARCHITECTURE.md` обновлён с описанием pipeline stages
- [ ] Feature flag снят, единый pipeline path в production
