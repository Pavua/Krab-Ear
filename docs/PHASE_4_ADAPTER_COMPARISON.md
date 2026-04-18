# Phase 4 STT Adapter Comparison

Krab Ear поддерживает 5-уровневую fallback chain из STT адаптеров. По умолчанию работает только
whisper-balanced (Level 1). Остальные четыре — opt-in через переменные окружения или `core/config.py`.

**Порядок цепочки (все включены):**
```
balanced → Parakeet → SenseVoice → WhisperX → Voxtral → max-candidates
```

---

## Сравнительная таблица адаптеров

| Адаптер | Лучше всего для | Языки | Уникальные поля | RAM | Скорость | Лицензия | Установка |
|---|---|---|---|---|---|---|---|
| **whisper-turbo** (baseline) | Универсальное использование RU/ES/EN | 99 языков | `text`, `segments`, `language` | ~2 GB | Быстрая (MLX) | MIT | Встроен |
| **Parakeet-TDT-1.1B** | Лучший WER для English | EN only | `text`, `language` | ~3 GB | Быстрая | CC-BY-4.0 | `pip install nemo_toolkit` |
| **SenseVoice Small** | Русский + определение эмоций | RU, ZH, JA, KO, YUE, EN | `text`, `emotion`, `language` | ~1.5 GB | Очень быстрая | Apache 2.0 | `pip install funasr` |
| **WhisperX** | Временны́е метки слов + диаризация | 99 языков | `text`, `word_timestamps`, `speaker_turns` | ~4.5 GB | Средняя | BSD-2 | `pip install whisperx` |
| **Voxtral Mini 4B** | STT + reasoning на 13 языках | RU, ES, EN, FR, DE, IT, PT, AR, ZH, JA, KO, HI, NL | `text`, `reasoning`, `language` | ~3 GB | Средняя | Mistral Research | `pip install mistral-inference` |

---

## Детальное описание

### 1. whisper-turbo (всегда активен)

**Модель:** `mlx-community/whisper-large-v3-turbo`

Базовый адаптер на основе MLX-оптимизированного Whisper. Активен всегда, служит первым
кандидатом в fallback chain. Хорошо справляется с RU/ES/EN, поддерживает 99 языков.

```python
# Результат:
{"text": "привет мир", "segments": [...], "language": "ru"}
```

**Включение:** всегда ON.

---

### 2. Parakeet-TDT-1.1B (opt-in)

**Модель:** `nvidia/parakeet-tdt-1.1b`  
**Позиция в chain:** 2 (после balanced, перед SenseVoice)

OpenASR Leaderboard #1 для английского языка (WER ~3.5%). Оптимизирован
исключительно для EN — на других языках не работает. Рекомендуется для
транскрипции технических докладов, подкастов, митингов на английском.

```python
# Результат:
{"text": "hello world", "language": "en", "segments": [...]}
```

**Включение:**
```bash
export KRAB_EAR_PARAKEET_ENABLED=true
```

---

### 3. SenseVoice Small (opt-in)

**Модель:** `iic/SenseVoiceSmall`  
**Позиция в chain:** 3 (после Parakeet, перед WhisperX)

Специализированная модель от Alibaba DAMO Academy. Определяет эмоцию говорящего
(`happy`, `neutral`, `sad`, `angry`, `disgusted`, `surprised`). Лучший вариант
для дневника, личных заметок, транскрипции разговоров на русском.

```python
# Результат:
{"text": "привет", "emotion": "happy", "language": "ru", "segments": [...]}
```

**Включение:**
```bash
export KRAB_EAR_SENSEVOICE_ENABLED=true
# Сохранять emotion в history:
export KRAB_EAR_SENSEVOICE_EMOTION_TO_HISTORY=true
```

---

### 4. WhisperX (opt-in)

**Модель:** `large-v3` через whisperx  
**Позиция в chain:** 4 (после SenseVoice, перед Voxtral)

Расширенный Whisper с пословными временны́ми метками и опциональной диаризацией
(кто говорил и когда). Идеален для субтитров, стенограмм совещаний с несколькими
участниками.

```python
# Результат:
{
    "text": "hello world",
    "word_timestamps": [{"word": "hello", "start": 0.0, "end": 0.5, "confidence": 0.95}],
    "speaker_turns": [...],
    "language": "en"
}
```

**Включение:**
```bash
export KRAB_EAR_WHISPERX_ENABLED=true
export KRAB_EAR_WHISPERX_WORD_TIMESTAMPS=true
export KRAB_EAR_WHISPERX_DIARIZATION=false  # true требует HuggingFace token
```

---

### 5. Voxtral Mini 4B Realtime (opt-in)

**Модель:** `mistralai/Voxtral-Mini-3B-2507`  
**Позиция в chain:** 5 (после WhisperX, перед max-candidates)

Первая модель Mistral для голоса. Поддерживает 13 языков включая RU/ES/EN/FR/DE.
При `VOXTRAL_REASONING_ENABLED=true` дополнительно генерирует краткое резюме или
ответы на вопросы по транскрипту.

```python
# Результат (reasoning выключен):
{"text": "bonjour", "reasoning": None, "language": "fr", "segments": [...]}

# Результат (reasoning включён):
{"text": "bonjour", "reasoning": "Краткое содержание: ...", "language": "fr"}
```

**Включение:**
```bash
export KRAB_EAR_VOXTRAL_ENABLED=true
export KRAB_EAR_VOXTRAL_REASONING_ENABLED=false  # true = STT + summary
```

---

## Дерево решений: какой адаптер включить?

```
Какая задача?
│
├── Лучшее качество для РУССКОГО?
│   └── → SenseVoice  (SENSEVOICE_ENABLED=true)
│
├── Определение эмоций?
│   └── → SenseVoice  (SENSEVOICE_ENABLED=true, SENSEVOICE_EMOTION_TO_HISTORY=true)
│
├── Пословные временны́е метки / субтитры?
│   └── → WhisperX  (WHISPERX_ENABLED=true, WHISPERX_WORD_TIMESTAMPS=true)
│
├── Диаризация (кто говорил)?
│   └── → WhisperX  (WHISPERX_ENABLED=true, WHISPERX_DIARIZATION=true)
│
├── Лучший WER для АНГЛИЙСКОГО?
│   └── → Parakeet  (PARAKEET_ENABLED=true)
│
├── STT + автоматическое резюме / reasoning?
│   └── → Voxtral  (VOXTRAL_ENABLED=true, VOXTRAL_REASONING_ENABLED=true)
│
└── Не уверен / общее использование?
    └── → оставь whisper-balanced по умолчанию (ничего не менять)
```

---

## Совместимость и конфликты

- Все адаптеры можно включить **одновременно** — они выстраиваются в цепочку и
  активируются последовательно при сбое предыдущего.
- Суммарный RAM при одновременной загрузке всех: **~14 GB** — в пределах M4 Max 36 GB.
- Parakeet **не обрабатывает** не-английские аудио. При обнаружении ошибки маркер
  помечается недоступным, chain продолжается на SenseVoice/WhisperX.
- WhisperX с диаризацией требует `HUGGINGFACE_TOKEN` и принятия условий лицензии
  pyannote на HuggingFace Hub.

---

## Быстрый старт: включить SenseVoice + Parakeet

```bash
# ~/.krab_ear.env или export в терминале:
export KRAB_EAR_SENSEVOICE_ENABLED=true
export KRAB_EAR_SENSEVOICE_EMOTION_TO_HISTORY=true
export KRAB_EAR_PARAKEET_ENABLED=true

# Перезапуск backend:
python KrabEar/main.py --data-dir ~/.krab_ear_data
```

---

*Документ соответствует Phase 4 fallback chain (PR b8dd974 + ae52ea8).*
