# Волна 3a — обучение русской wake-модели «Краб» (openWakeWord)

Дата: 2026-07-09. Статус: мини-спека (роадмап-волна пре-одобрена владельцем в Roadmap H2;
порядок 3c → 2.80 → 3a задан владельцем 2026-07-09). Разведка: read-only агент, факты ниже
сверены с кодом.

## 1. Контекст (из разведки)

- `wake_word_models/` в репозитории НЕ существует (и никогда не существовала — «инструкция в
  wake_word_models/» из роадмапа написана в будущем времени). `_CUSTOM_MODELS_DIR`
  (`openwakeword_adapter.py:35`) — это runtime-поддиректория `{data_dir}/wake_word_models/`.
- Движок готов: `openwakeword_adapter.py` сканирует `{data_dir}/wake_word_models/*.onnx|*.tflite`
  (`list_models()`), path-containment/symlink-гарды есть, IPC `wake_word_list_models/start/stop/status`
  в диспетчере. Текущий дефолт — английская `hey_jarvis`.
- Swift-пикер моделей УЖЕ есть (`HistoryPanelController+Settings.swift:1289-1399`): динамически
  перезаполняется из `wake_word_list_models`, выбор → `UserDefaults KrabEar_WakeWordModel` →
  `wake_word_start {model, threshold}`. Слайдер порога есть. **Новый Swift-код не нужен.**
- Тренировочный стек УЖЕ установлен в `.venv_krab_ear` (py3.14): openwakeword 0.6.0 (train.py:
  `auto_train`/`_select_best_model(max_fp_per_hour, min_recall)`/`export_to_onnx`), torch 2.11+MPS
  (подтверждён), torch-audiomentations, pytorch-lightning. Jupyter НЕ установлен — вместо notebook
  пишем обычный `.py`-скрипт теми же API (осознанное отклонение от «Jupyter ~15 мин» роадмапа).
- Сырых датасетов в пакете нет: позитивы и негативы добываются снаружи (см. §3).

## 2. Цель и не-цели

**Цель:** кандидат-модель `krab_ru.onnx`, которая (а) появляется в Settings-пикере автоматически,
(б) проходит офлайн-гейт `max_fp_per_hour ≤ 1.0` на негативном корпусе, (в) готова к живой
owner-валидации.

**Не-цели:** Swift-изменения (пикер есть); дообучение на реальном голосе владельца (шаг 7 роадмапа,
опционален, отдельно); tflite-экспорт (onnx достаточно, adapter принимает оба); идеальный recall
на чужих голосах (v1 — для владельца).

## 3. Ключевые решения

1. **Фраза:** «Краб» (как в роадмапе/USER_MANUAL). Риск: односложное слово → повышенный
   false-positive класс («краба», «корабль», «прораб», «крабы»…). Митигация: adversarial-негативы
   через `openwakeword.data.generate_adversarial_texts` + жёсткий офлайн-гейт fp/час + порог в
   Settings. Вариант «эй, Краб» добавляем в позитивы как вторичную форму (одна модель, один label).
2. **Позитивы — Silero RU TTS, НЕ piper-sample-generator.** Piper latent-sampling генератор
   EN-центричен; у Silero 5 русских спикеров (aidar/baya/kseniya/xenia/eugene) + SSML
   prosody/rate/pitch → с аугментацией даёт достаточное разнообразие для v1. Переиспользуем
   уже существующий стек проекта (`tts_service.py`-класс движка), но скрипт самодостаточен
   (прямой silero через torch.hub) — не зависит от запущенного backend. Объём: ~3-5k клипов
   (все спикеры × вариации просодии × 2 формы фразы).
3. **Негативы — гибрид:** (а) официальные предвычисленные negative-features openWakeWord
   (HF, тяжёлый one-time download в ГБ — URL сверить на месте, разведка офлайн не проверяла);
   (б) RU-синтетика: Silero читает случайный русский текст (без «краб»-корней) → featurize;
   (в) adversarial-тексты (краба/корабль/прораб/крабы/…) → Silero → featurize как негативы.
4. **Аугментация:** штатная `openwakeword.data.augment_clips` (шум/RIR/громкость) + RIR/noise
   датасеты по официальному рецепту (скачиваются на шаге корпусов).
5. **Гейт качества (офлайн):** `_select_best_model(..., max_fp_per_hour=1.0)` (роадмап: ≤1/час;
   дефолт пакета 0.5 — берём роадмаповский потолок, порог отдадим слайдеру).
6. **Артефакты:** скрипты+README — в репо `wake_word_models/` (модели/датасеты — gitignored,
   только код и инструкция); готовая модель кладётся в `{data_dir}/wake_word_models/krab_ru.onnx`
   вручную/скриптом (prod data_dir: `~/Library/Application Support/KrabEar/`).
7. **CI:** тренировочный скрипт — ручной инструмент, в CI не гоняется (нет GPU/датасетов);
   flake8 применяется; лёгкий unit-тест только на чистые хелперы (генерация текстов вариаций,
   парсинг аргументов) без сети/GPU.

## 4. Поставка (этапы, все [автономно] кроме последнего)

- T1: `wake_word_models/README.md` (инструкция end-to-end) + `train_krab.py` (CLI: этапы
  `--stage corpora|positives|negatives|features|train|export|install`, resume-friendly,
  прогресс в stdout) + `.gitignore` на датасеты/артефакты + flake8 + юнит на хелперы.
- T2: прогон корпусов (download, ГБ — фоновый), синтез позитивов/негативов, featurization.
- T3: тренировка на MPS + экспорт `krab_ru.onnx` + офлайн-отчёт (fp/час, recall на held-out
  синтетике) в `wake_word_models/report_krab_ru.md`.
- T4: установка в data_dir, живая проверка `wake_word_list_models` → модель видна; смок
  `wake_word_start {model: krab_ru}` на throwaway-backend (НЕ прод) — грузится без ошибок.
- T5 [owner-assisted]: живая валидация — recall голосом владельца + ≤1 ложного/час фоновой
  речи в реальной комнате; подстройка порога слайдером; вердикт в роадмап.

## 5. Риски

- Негативный корпус: URL/формат официальных feature-датасетов могут дрейфануть — сверяем на
  месте (HF hub), при недоступности fallback: собственная RU/EN-синтетика больших объёмов +
  открытые шумовые датасеты (качество гейта ниже, отметить в отчёте).
- Silero-разнообразие < piper-latent: recall на нестандартной просодии может просесть — лечится
  шагом 7 (дообучение на реальных фразах владельца), не блокер v1.
- Py3.14 + openwakeword 0.6.0 train-путь: если train-API упрётся в несовместимость (пакет
  тестировался на ≤3.11), fallback — отдельный лёгкий venv 3.11/3.12 только для тренировки
  (инференс-адаптер остаётся на .venv_krab_ear, ему это не мешает).
