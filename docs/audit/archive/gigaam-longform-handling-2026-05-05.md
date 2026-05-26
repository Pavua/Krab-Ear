# GigaAM Longform Handling Audit

**Date:** 2026-05-05
**Trigger:** User question «у нас там вроде бы до 25 секунд секунд запись максимум через него можно сделать, больше или это навсегда»

---

## Hard limit

GigaAM-RNNT v2 (Conformer-based, 244M параметров) имеет архитектурный hard limit
~25–30 секунд на один вызов `transcribe()`. При превышении модель возвращает
обрезанный или повреждённый вывод (WER резко растёт). Бенчмарк 2026-04-26 подтвердил:
реальный порог отказа ~26s+; threshold в коде — **24s** (консервативный запас).

## Компоненты

### `core/audio_chunker.py`

Существует. Полноценная реализация:
- `AudioChunker.chunk(audio, sample_rate, max_chunk_sec)` — silence-based split через
  `SilenceDetector`. Жёсткий разрез если паузы нет в окне.
- `AudioChunker.merge_results(chunks)` — объединяет текст, усредняет confidence,
  сдвигает временны́е метки Whisper-сегментов по offset чанка.
- Не требует pyannote / HF token — работает на любом аудио без внешних зависимостей.

### `core/engine.py` — `_transcribe_gigaam()` (строка ~2244)

До этого PR: для аудио > 24s код переходил на `adapter.transcribe(longform=True)`,
что вызывает `model.transcribe_longform()` — требует **pyannote.audio** + принятие
TOS на `huggingface.co/pyannote/segmentation-3.0` (known blocker, см. memory
`blocker_pyannote_gated_2026-04-26.md`). `AudioChunker` в engine.py **не импортировался**.

### `core/pipeline/stt_gigaam.py` — `GigaAMAdapter`

Адаптер принимает `longform=bool` параметр и пробрасывает его в
`_transcribe_in_process` / `_transcribe_subprocess`. Subprocess path увеличивает
timeout в 8× для longform (120s → 960s).

## Verification (до PR)

`AudioChunker` **НЕ использовался** для GigaAM пути:

```
$ grep -n "AudioChunker\|audio_chunker" KrabEar/core/engine.py
(нет результатов)
```

Для записей > 24s engine падал на `transcribe_longform()` → ошибка pyannote
gated repo если HF token не установлен / TOS не приняты.

## Что изменено (этот PR)

`_transcribe_gigaam()` переработан: для аудио > 24s теперь **двухуровневый** подход:

1. **AudioChunker path (предпочтительный)** — silence-based split на чанки ≤ 20s
   (5s запас до hard limit). Каждый чанк транскрибируется отдельно через
   `adapter.transcribe()`. Результаты объединяются через `AudioChunker.merge_results()`.
   Не требует pyannote / HF token. Seamless для пользователя.

2. **transcribe_longform() fallback** — если AudioChunker упал по любой причине,
   код падает обратно на pyannote VAD path (требует HF token + TOS).

Изменённые строки: `engine.py` ~2299–2356.

## Gaps (оставшиеся)

- Если и AudioChunker, и longform упали → engine возвращает `{"engine": "gigaam-error"}`
  и fallback chain переключается на Whisper. Корректное поведение.
- Subprocess worker (`gigaam_worker.py`) получает чанки через temp WAV файлы.
  Для очень длинных записей (1h+) это создаёт O(N) temp файлов — приемлемо,
  очищаются через `os.unlink` в GigaAMAdapter.
- `STT_GIGAAM_HF_TOKEN` нужен только для longform fallback path, не для chunker path.
  Это снимает блокер для большинства пользователей.

## Recommendation

- AudioChunker path решает hard limit навсегда без внешних зависимостей.
- Для диктовки (монолог) чанки 20s дают отличное качество с минимальными артефактами
  на границах (GigaAM хорошо обрабатывает короткие высказывания).
- Принять HF TOS для pyannote всё равно рекомендуется для longform fallback надёжности.
