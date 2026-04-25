# GigaAM-RNNT v2 — Русскоязычный STT адаптер

## Что это

**GigaAM** (Giga Audio Model) — Conformer-based модель для распознавания русской речи от Sber (salute-developers).
Версия **v2-RNNT** (244M параметров) обучена на 50 000 часов русскоязычных аудиоданных.

Адаптер интегрирован в Krab Ear как opt-in замена whisper-large-v3 для русского языка.

## Сравнение WER (Word Error Rate)

| Модель | Common Voice RU WER | Params | Размер |
|--------|--------------------:|-------:|-------:|
| GigaAM-RNNT v2 | **~3.8%** | 244M | ~1 GB |
| GigaAM-CTC v2 | ~4.2% | 244M | ~1 GB |
| whisper-large-v3 | ~9.8% | 1.5B | ~3 GB |
| whisper-large-v3-turbo | ~11.2% | 809M | ~1.6 GB |

GigaAM-RNNT v2 даёт примерно **2.5× улучшение WER** по сравнению с whisper-large-v3 на русскоязычном тесте.

## Как включить

### 1. Установить пакет

```bash
pip install gigaam
```

Для длинных аудио (>30 секунд, требует HF_TOKEN для pyannote VAD):

```bash
pip install gigaam[longform]
```

### 2. Включить в настройках

Через переменные окружения:

```bash
export KRAB_EAR_STT_GIGAAM_ENABLED=true
export KRAB_EAR_STT_GIGAAM_MODE=rnnt       # rnnt | ctc | v2_rnnt | v2_ctc | v1_rnnt | v1_ctc
export KRAB_EAR_STT_GIGAAM_DEVICE=mps      # mps | cpu
```

Или через IPC `update_settings`:

```json
{
  "stt_gigaam_enabled": true,
  "stt_gigaam_mode": "rnnt",
  "stt_gigaam_device": "mps"
}
```

### 3. Включить языковой роутинг (опционально)

Чтобы GigaAM автоматически выбирался для русского языка:

```bash
export KRAB_EAR_STT_LANGUAGE_ROUTING_ENABLED=true
```

## Потребление памяти

| Компонент | RAM |
|-----------|-----|
| GigaAM модель (float32) | ~950 MB |
| PyTorch MPS runtime | ~200 MB |
| **Итого** | **~1.1 GB** |

На M4 Max 36 GB это незначительная нагрузка. При активном whisper-large-v3 (~3 GB) GigaAM
может работать параллельно без memory pressure.

## Режимы модели

| mode | Декодер | WER | Latency |
|------|---------|-----|---------|
| `rnnt` (default) | RNN Transducer | ~3.8% | умеренная |
| `ctc` | CTC | ~4.2% | быстрее |
| `v2_rnnt` / `v2_ctc` | v2 веса (те же, что rnnt/ctc) | — | — |
| `v1_rnnt` / `v1_ctc` | v1 веса (старая версия) | хуже | — |

Рекомендуется `rnnt` (default) для наилучшего качества.

## Fallback chain

```
Аудио с detected_lang == "ru"
    └─► GigaAM-RNNT v2  (если STT_GIGAAM_ENABLED=true)
        └─► fallback: mlx-whisper RU fine-tune (если задан STT_RU_PRIMARY_MODEL)
            └─► fallback: whisper-large-v3 (STT_OTHER_PRIMARY_MODEL)
```

✅ Wired into AudioEngine via STT router — адаптер полностью интегрирован в
`AudioEngine._transcribe_with_fallback_impl()` (PR feat/gigaam-audio-engine-integration).
Адаптер пробуется первым в fallback chain когда `STT_GIGAAM_ENABLED=True` и `lang=ru`.

## Лицензия

MIT License — коммерческое использование разрешено без ограничений.

- Код: https://github.com/salute-developers/GigaAM
- PyPI: https://pypi.org/project/gigaam/
- Model card: https://huggingface.co/salute-developers/GigaAM

## HuggingFace login

GigaAM — **не gated репо**, HuggingFace login не требуется для базового использования.

Для long-form режима (gigaam[longform]) нужен HF_TOKEN для загрузки pyannote VAD:

```bash
huggingface-cli login
# или
export KRAB_EAR_HF_TOKEN=hf_xxxxxxx
```

## Технические детали реализации

- Файл адаптера: `KrabEar/core/pipeline/stt_gigaam.py`
- Класс: `GigaAMAdapter(device="mps", mode="rnnt")`
- Lazy load: модель загружается при первом вызове `transcribe()`, не в `__init__`
- Формат входа: `np.ndarray` float32, любая частота (ресемплируется до 16 кГц)
- Формат выхода: `{"text": str, "language": "ru", "confidence": float, "engine": "gigaam-rnnt"}`
- Thread safety: PyTorch MPS, не MLX — `mlx_lock` **не нужен**
- Временный WAV: аудио конвертируется в 16-bit mono WAV во временный файл, передаётся в `model.transcribe(path)`, файл удаляется после завершения
