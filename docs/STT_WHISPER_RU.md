# Russian Whisper Fine-Tune (antony66/whisper-large-v3-russian)

## Что это

Drop-in апгрейд для русского STT — тот же `mlx-whisper` pipeline, но вместо базового
`mlx-community/whisper-large-v3-mlx` используется checkpoint, дообученный на русском
Common Voice / OpenSTT. Архитектура идентична (`whisper-large-v3`), поэтому адаптер
не нужен — только смена `path_or_hf_repo`.

Модель: [`antony66/whisper-large-v3-russian`](https://huggingface.co/antony66/whisper-large-v3-russian)  
Лицензия: Apache 2.0 (коммерческое использование разрешено)  
Размер: ~3 GB (те же веса что у large-v3, другие коэффициенты)

## Ожидаемое улучшение качества

| Benchmark | Базовый whisper-large-v3 | RU fine-tune |
|-----------|--------------------------|--------------|
| WER (Common Voice ru) | ~14% | ~12% |
| WER (разговорная речь) | ~18–22% | ~16–20% |

Ожидаемое улучшение: **~2 pp WER** на типичной русскоязычной диктовке.
Для технических терминов и имён — дополнительно используйте `STT_HOTWORDS`.

Примечание: GigaAM (Sber) даёт ~5–8 pp улучшения, но требует отдельного адаптера
и другого inference stack. Этот fine-tune — промежуточный шаг: минимальный код,
тот же fallback chain, Apache 2.0.

## Как включить

### Через переменную окружения

```bash
export KRAB_EAR_STT_USE_RU_FINETUNE=true
python KrabEar/main.py
```

### Через `.secrets` файл

```
# ~/Library/Application Support/KrabEar/.secrets
KRAB_EAR_STT_USE_RU_FINETUNE=true
```

### Через IPC (runtime, без рестарта)

```json
{"id": "1", "method": "update_settings", "params": {"stt_use_ru_finetune": true}}
```

## Первый запуск

При первом использовании `mlx-whisper` автоматически скачивает и конвертирует
HuggingFace safetensors в MLX-формат (~3 GB). Последующие запуски используют
кеш из `~/.cache/huggingface/`.

## Fallback chain

При `STT_USE_RU_FINETUNE=true` и `language="ru"` порядок попыток:

```
RU fine-tune → balanced whisper-turbo → [SenseVoice] → [WhisperX] → max whisper-large-v3
```

Если fine-tune модель недоступна (не скачана, OOM, сетевая ошибка) — движок
автоматически переходит к следующему кандидату. Никакого краша, пользователь
получает транскрипцию из стандартного chain'а.

При `language != "ru"` (испанский, английский, другие) fine-tune не активируется
и chain остаётся без изменений.

## Настройка

| Параметр | Дефолт | Описание |
|----------|--------|----------|
| `STT_USE_RU_FINETUNE` | `false` | Включить RU fine-tune |
| `STT_RU_FINETUNE_MODEL` | `antony66/whisper-large-v3-russian` | HuggingFace repo |

Для смены checkpoint достаточно изменить `STT_RU_FINETUNE_MODEL` на любой
совместимый по архитектуре Whisper large-v3 репозиторий.
