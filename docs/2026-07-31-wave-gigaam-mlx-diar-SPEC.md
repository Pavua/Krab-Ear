# Спек волны v2: GigaAM-MLX транспорт + диаризация community-1 + диаризованный конвейер

Дата: 2026-07-31, v2 (после адверсариального ревью; v1-вердикт «требует переработки» —
все CRITICAL/MAJOR учтены). Ветка: `claude/gigaam-mlx-diar-wave`
(worktree от `origin/codex/krab-ear-v2` @ 5f7af381). Владелец делегировал
ревью самопроверке + адверсариальному критику; статус: **готов к исполнению
после зелёного прото-смока (P0)**.

## Контекст (что уже есть — НЕ переделываем)

- GigaAM v3 интегрирован (PyTorch, subprocess, venv `~/.venv_krab_ear_gigaam`)
  и ВКЛЮЧЁН в проде (`stt_gigaam_enabled: true`, `v3_e2e_rnnt`, `cpu`,
  `subprocess`) — работает, лог 31.07 «worker готов».
- pyannote 4.0.4 в проде (модель 3.1, MPS), community-1 в HF-кеше.
- PoC diarize→slice→transcribe: `poc_diarization/` — ⚠️ ТОЛЬКО в главной
  чекаушке (untracked, чужая занятая ветка `claude/s3-inprocess-enable`);
  в этом worktree его НЕТ.

## P0 (гейт до старта кода): прото-смок gigaam-mlx

`pip install git+https://github.com/aystream/gigaam-mlx.git` в чистый venv
Python 3.14 (как main venv: mlx 0.31.x рядом, librosa→numba придут транзитивно)
→ `load_model('rnnt')` → transcribe 20-сек wav. Зелёный смок = несущее
предположение W-A подтверждено. Красный — W-A перепроектируется (изолированный
venv убивает смысл «в главном процессе под локом»); стоп и доклад владельцу.

## Цели

- **W-A. MLX-транспорт GigaAM**: `stt_gigaam_transport="mlx"` — инференс
  gigaam-mlx в главном процессе. Эффект: минуты вместо CPU-тягомотины на
  длинных файлах и уход от Metal-конфликта (инцидент backend-j 2026-05-19) —
  инференс под общей блокировкой с whisper.
- **W-B. Диаризация community-1** через список кандидатов, дефолт не меняется.
- **W-C. Диаризованный конвейер** длинных ФАЙЛОВЫХ записей (opt-in).

## Не-цели

- Не менять поведение ни одного включённого сегодня пути. В частности:
  существующий PyTorch-GigaAM путь НЕ трогаем вообще (его холостая
  постобработка — известный долг, отдельная волна).
- Не гейтить NumberNormalizer/DateTimeNormalizer: нормализация чисел/дат —
  не пунктуация, это отдельные opt-in фичи пользователя.
- PunctuationFixer — вне скоупа: он вызывается из backend TextPostProcessor
  (user-invoked IPC), а не из engine-цепочки STT.
- Streaming-путь `transcribe_chunked` — вне скоупа (в проде выключен);
  пометить в коде, что `native_punctuation` там не пробрасывается.
- Не подключать Phase D.2 pipeline к engine.

## Дизайн

### W-A: MLX-транспорт

**Инварианты блокировок — ядро дизайна, не деталь:**

1. Лок берётся **по-чанково**: `mlx_inter_process_lock()` → `mlx_lock()`
   вокруг КАЖДОГО инференс-вызова одного чанка (≤20 c аудио ≈ доли секунды
   инференса при ~77× RT). Между чанками лок ОТПУСКАЕТСЯ — живая диктовка
   ждёт максимум один чанк, а не весь файл. (RLock без таймаута; межпроцессный
   flock таймаутится через 5 с с raise — длительное удержание роняет чужие
   вызовы.)
2. Загрузка модели и скачивание весов — **вне** лока.
3. Каждый инференс-вызов — под `MLXWatchdog.run_with_timeout` (как whisper,
   engine.py:2296): зависший Metal-вызов без watchdog повесит RLock навечно
   (freeze-class).
4. Тест-инвариант: «лок не удерживается между чанками» (mock-лок со счётчиком
   захватов == числу чанков).

**Файлы:**

1. `KrabEar/core/pipeline/stt_gigaam_mlx.py` (новый): адаптер по образцу
   `stt_gigaam.py`. **Lazy-import `gigaam_mlx` внутри методов** (py3.12
   ubuntu-parity CI не имеет библиотеки). Тип модели MLX выводится из
   существующего `stt_gigaam_mode` (v3_e2e_rnnt→"rnnt", v3_e2e_ctc→"ctc") —
   отдельный ключ модели НЕ вводим. Чанкинг >20 c через `core/audio_chunker.py`
   (константа `_GIGAAM_MAX_CHUNK_SEC = 20.0` — НЕ 25: upstream отвергает
   массивы >25×16000, запас осознанный, engine.py:2989). Вход — файл или
   ndarray (ndarray пишется во временный wav: API библиотеки файловый).
   Контракт результата: `{"text","language","confidence","engine",
   "native_punctuation": True}`. `close()` выгружает модель.
2. `KrabEar/core/pipeline/stt_gigaam.py`: `"mlx"` в `_VALID_TRANSPORTS` (:87);
   `_resolve_transport()` НЕ трогаем (он про in_process/subprocess внутри
   PyTorch-адаптера).
3. Выбор КЛАССА адаптера — в обоих реестрах:
   `stt_router._get_gigaam_adapter_locked` (stt_router.py:441-455) и
   `stt_router_factory.build_router` (:50-60): при `transport=="mlx"`
   конструируется MLX-адаптер, иначе — как сейчас. **Factory расширить
   catch на ValueError** (сейчас ловит только ImportError — ValueError из
   `__init__` вылетел бы наружу в rest_server.py:1763).
4. `core/config.py`: допустимое значение `"mlx"`; **создать** (сейчас
   отсутствуют) `stt_gigaam_transport` в `DEFAULT_SETTINGS` (default
   "subprocess" — текущее прод-значение) и enum-валидацию в
   `backend/settings_validator.py` рядом с `stt_gigaam_mode` (:63).
5. Маркер в каскаде: `_GIGAAM_MLX_MARKER` рядом с `_GIGAAM_MARKER`
   (engine.py:1930); gate — как у GigaAM (lang=ru, enabled, `_skip_gigaam`
   покрывает ОБА маркера — расширить `test_rest_no_duplicate_gigaam` на MLX,
   включая chunk-repro правило); dispatch (2065-2109); семантика ошибок
   1:1 с PyTorch-GigaAM (2133-2155): аварийный ответ → raise + блэклист 300 c,
   пустой текст → continue без блэклиста.
6. Гейт постобработки: `native_punctuation=True` отключает **только**
   punctuation-LLM-pass (engine.py:1215-1227) и только для НОВОГО MLX-пути.
   Number/DateTime normalizer работают как раньше. (Результат адаптера
   доживает до постобработки без пересборки — проверено ревью.)

### W-B: community-1

- Один ключ: `DIARIZATION_MODEL_CANDIDATES: str = ""` (пусто = сегодняшнее
  поведение, единственная `DIARIZATION_MODEL`). Непустой список — цикл
  кандидатов в `_load_diarization_pipeline()` (engine.py:3396-3431), первая
  загрузившаяся побеждает. Отдельного bool-флага НЕТ (перекрывающиеся ключи
  = источник конфликтов).
- Латч ошибок `_diarization_load_error` (3403-3413) сделать пер-кандидатным:
  ошибка кандидата не должна навсегда блокировать следующих.
- Фактически загруженная модель сохраняется в атрибут и отдаётся в
  health/status (backend/service.py:2850-2852 сейчас показывает конфиг, а не
  факт — поправить, иначе UI врёт).
- E2e-проверка эмбеддингов: `diarize_window.speaker_embeddings` при
  community-1 — сопоставление спикеров между окнами встречи не должно
  деградировать (если деградирует — community-1 только для батч-пути W-C).

### W-C: диаризованный конвейер

- **Гейт: ТОЛЬКО файловые входы** (`isinstance(audio_data, (str, Path))`) +
  `DIARIZED_TRANSCRIPTION_ENABLED` (default False) + длительность >
  `DIARIZED_MIN_DURATION_SEC` (120) + `num_speakers<=DIARIZED_MAX_SPEAKERS` (2).
  Живые диктовки (ndarray) не затрагиваются никогда.
- Ранний return ДО rewrite/cleanup/paste (образец `transcribe_chunked`,
  engine.py:930-940): диаризованный `[mm:ss] SPEAKER_N:` транскрипт НЕ идёт
  в LLM-rewrite и автовставку. Контракт результата — полный набор ключей
  консюмеров (llm_applied=False, confidence, engine, model, language,
  segments, diarization, emotion — образец 1061-1068).
- Первый шаг исполнения: скопировать `poc_diarization/full_transcription.py`
  (+ `test_call*.wav` для e2e) из главной чекаушки в worktree (чтение чужой
  чекаушки — read-only, допустимо) и закоммитить в ветку волны.
- Склейка соседних сегментов одного спикера — до **20 c** (та же константа).
- `_diarization_run_lock` на весь прогон диаризации файла: удержание на
  часовой записи — минуты; **осознанно принимаем для v1** (батч-режим,
  конфликт только с live-диаризацией активной встречи; в доке модуля — WARN
  и TODO на почанковый прогон отдельной волной).
- `gc.collect()` + `torch.mps.empty_cache()` после прогона — внутри critical
  section (образец 3466-3475).

## Тесты

Как в v1, плюс: лок-инвариант по-чанково (счётчик захватов); ValueError-путь
factory; W-C не срабатывает на ndarray; контракт ключей раннего return;
пер-кандидатный латч W-B; все тесты — без реального `gigaam_mlx`
(lazy-import + fake-модуль), совместимо с py3.12 ubuntu-parity.

## Definition of Done

1. P0-смок зелёный (лог приложен к PR).
2. Все новые тесты зелёные; `make test` без новых падений; flake8 CI-строгий;
   `make audit-all` (новые модули в core/pipeline/); `scripts/pre_merge_py312_check.sh`.
3. Живой e2e на M4 Max — без запуска второго прод-инстанса (прямые вызовы
   движка из worktree): (а) transport=mlx ≥5× быстрее subprocess-cpu на
   том же файле при сравнимом тексте; (б) W-C выдаёт `[mm:ss] SPEAKER_N:`
   на двухголосой записи (test_call + длинная ночная запись владельца);
   (в) с выключенными флагами — прежнее поведение байт-в-байт.
4. `docs/ROADMAP-2026H2.md` обновлён записью волны.
5. Прод-флаги не переключаем; включение — решение владельца после канарейки.
6. PR в `codex/krab-ear-v2`.

## Порядок

P0 смок → W-A (тесты → код) → W-B → W-C → e2e → ROADMAP → PR.
