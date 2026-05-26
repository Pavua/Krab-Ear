# Аудит: SmartSilenceSkipper (W1096)

**Дата:** 2026-05-26  
**Файл:** `KrabEar/core/smart_silence_skipper.py`  
**Зависимости:** `KrabEar/core/silence_detector.py`, `KrabEar/core/config.py`  
**Тесты:** `KrabEar/tests/test_smart_silence_skipper.py` (34 теста, все проходят)

---

## Краткое описание

`SmartSilenceSkipper` удаляет длинные внутренние паузы (>1 с) из середины аудиозаписи перед STT, сохраняя первые и последние 0.3 с без изменений. Использует `SilenceDetector.detect_silence` для поиска регионов тишины. Управляется флагом `SMART_SILENCE_SKIP_ENABLED` (по умолчанию `False`).

---

## Findings

### F1 — CRITICAL: модуль не подключён ни к одному production-пути (мёртвый код)

`SMART_SILENCE_SKIP_ENABLED` существует в `config.py` (строки 200, 826) и упоминается в двух тестовых файлах (`test_engine_multipass.py:164`, `test_engine_streaming.py:73`), где его **мокают в `False`**. В `core/engine.py` нет ни одного импорта `SmartSilenceSkipper`, ни чтения флага `SMART_SILENCE_SKIP_ENABLED`.

**Вывод:** флаг включения существует, класс написан, тесты есть — но вызова `SmartSilenceSkipper.process()` нигде в production-коде нет. Изменение `SMART_SILENCE_SKIP_ENABLED=True` не произведёт никакого эффекта.

**Место подключения:** в `engine.py` после шага 2.5 (`_maybe_denoise`, строка 842) и до VAD prefilter (строка 853–869):
```python
if settings.SMART_SILENCE_SKIP_ENABLED and isinstance(audio_data, np.ndarray) and not is_preview:
    from core.smart_silence_skipper import SmartSilenceSkipper
    skip_result = SmartSilenceSkipper().process(audio_data, 16000)
    audio_data = skip_result.processed_audio
```

---

### F2 — HIGH: удаление сэмплов сдвигает временные метки Whisper (W1016-паттерн)

`SmartSilenceSkipper` физически удаляет сэмплы из массива. После конкатенации оставшихся фрагментов Whisper получает сжатый буфер. Все временны́е метки в `result["segments"]` будут относиться к сжатому таймлайну, а не к оригинальному.

**Последствия:** SRT-экспорт, закладки (`bookmarks.py`), диаризация (`pyannote`), `word_timing.py` — все получат смещённые метки. Это тот же риск, что описан для VAD prefilter (W1016), который явно задокументирован и принят как компромисс.

**SmartSilenceSkipper** этот компромисс в docstring не документирует, а `SkipResult` не возвращает `skipped_offsets` для возможной коррекции меток.

**Рекомендация:** при подключении добавить предупреждение в docstring; опционально — возвращать список `(original_start_sec, removed_duration_sec)` для коррекции сегментов Whisper на стороне вызывающего кода.

---

### F3 — MEDIUM: пересечение с VAD prefilter — дублирование логики

`_apply_vad_prefilter` в `engine.py` (строки 1117–1210) также удаляет длинные паузы: конкатенирует речевые сегменты с padding и обрезает паузы > `STT_VAD_SILENCE_TRIM_THRESHOLD_SEC`. `SmartSilenceSkipper` делает принципиально то же самое через другой детектор (`SilenceDetector` вместо `VoiceActivityDetector`).

Если оба механизма подключены одновременно:
- `SmartSilenceSkipper` сначала удаляет тишину → на выходе уже сжатый буфер
- VAD prefilter применяется к нему повторно → двойное сжатие, двойной сдвиг меток

VAD prefilter включён по умолчанию (`STT_VAD_PREFILTER_ENABLED=True`). `SmartSilenceSkipper` отключён по умолчанию. **Необходим explicit guard**: не запускать `SmartSilenceSkipper`, если VAD prefilter включён, или документировать ожидаемый порядок.

---

### F4 — MEDIUM: all-silence аудио передаётся в Whisper без изменений

Если весь входной буфер — тишина (например, записи с заглушённым микрофоном), `detect_silence` возвращает один регион `[0, n_samples]`. Этот регион НЕ проходит фильтр `r_start < inner_start` (строка 136), так как `r_start=0 < edge_samples=4800`. Итого: `skippable` пуст → `process()` возвращает аудио без изменений.

**Это корректное поведение** — `edge_keep_sec=0.3` намеренно защищает края. Но ни в docstring, ни в комментарии это не объяснено. Пользователь класса может ожидать, что all-silence вернёт пустой массив.

**Рекомендация:** добавить комментарий: «Если тишина выходит за пределы inner_start/inner_end — она сохраняется (edge protection). Для обрезки краевой тишины используйте `SilenceDetector.trim_silence`.»

---

### F5 — LOW: производительность на длинных аудиозаписях (часовые файлы)

`SilenceDetector.detect_silence` использует `np.array_split(audio, n_frames)` (строка 70 в `silence_detector.py`). Для 1-часового аудио 16 кГц: 57,600,000 семплов / 512 = **112,500 подмассивов**. `np.array_split` создаёт Python-объект на каждый фрейм — высокое давление на GC.

Более эффективный вариант — `np.lib.stride_tricks.as_strided` или `audio.reshape(-1, 512)` с padding. Для короткого потокового аудио (< 30 с) это несущественно. **Актуально при включении в batch-reprocess длинных файлов** (`BulkReprocessor`).

---

### F6 — LOW: идемпотентность только при параметрах по умолчанию

При дефолтных `min_silence_sec=1.0`, `speech_pad_sec=0.1`: после первого прохода между фрагментами остаётся 0.2 с тишины (2 × pad). Так как 0.2 с < 1.0 с, второй проход не изменяет буфер → **идемпотентно**.

При нестандартных параметрах (`min_silence_sec=0.05, speech_pad_sec=0.1`): остаток 0.2 с > 0.05 с → второй проход удалит его. Класс **не гарантирует идемпотентность** в общем случае, что не задокументировано.

---

## Суммарная таблица

| # | Severity | Тема | Статус |
|---|----------|------|--------|
| F1 | CRITICAL | Модуль не подключён (`SMART_SILENCE_SKIP_ENABLED` игнорируется) | Требует wire в `engine.py` |
| F2 | HIGH | Временны́е метки Whisper сдвигаются (W1016-паттерн) | Требует документирования / `skipped_offsets` |
| F3 | MEDIUM | Дублирует VAD prefilter, двойное сжатие при совместном включении | Требует guard или документации |
| F4 | MEDIUM | All-silence аудио возвращается без изменений (не задокументировано) | Добавить комментарий |
| F5 | LOW | `np.array_split` медленно на часовых файлах | Оптимизировать при batch-reprocess |
| F6 | LOW | Идемпотентность не гарантирована при кастомных параметрах | Документировать |

---

## W912 / W1080 совместимость

- **W912 (-40 dB):** `_DEFAULT_THRESHOLD_DB = -40.0` совпадает с дефолтом `SilenceDetector.detect_silence`. Консистентность соблюдена.
- **W1080 (audio_denoiser whisper preserve):** Вопрос порядка **неактуален** до завершения F1 (wiring). При подключении рекомендуемый порядок: denoiser (2.5) → SmartSilenceSkipper (2.6) → VAD prefilter (3). SmartSilenceSkipper на денойзированном аудио будет находить более чёткие границы тишины.

---

## Тестовое покрытие

34 теста в `test_smart_silence_skipper.py` охватывают: пустой буфер, нулевой sample_rate, короткое аудио, отсутствие пауз, одну/несколько длинных пауз, stereo, edge-тишину, padding, кастомные параметры, параллельные вызовы. **Отсутствуют тесты:**
- all-silence входной буфер (F4)
- поведение при `min_silence_sec < speech_pad_sec` (F6 edge)
- временны́е метки после skip (F2)
