# wake_word_models — тренировка кастомной wake-word модели «Краб»

Этот каталог содержит **самодостаточный CLI-инструмент** (`train_krab.py`) для
обучения кастомной русской wake-word модели «Краб» под движок
[openWakeWord](https://github.com/dscripka/openWakeWord), который уже живёт в
проде (`KrabEar/backend/openwakeword_adapter.py`).

Спека: `docs/superpowers/specs/2026-07-09-wake-model-krab-training-design.md`
(Волна 3a). Это T1 — инфраструктура (код + инструкция). Реальный прогон
(скачивание датасетов, синтез, тренировка) — T2/T3, отдельные сессии/шаги.

**В репозитории коммитятся только код, эта инструкция и итоговые отчёты
(`report_*.md`)** — датасеты, синтезированные аудиоклипы, промежуточные
`.npy`-фичи и обученные веса/ONNX **не коммитятся** (см. `.gitignore` рядом).

---

## 1. Пререквизиты

### 1.1 Окружение

Тренировочный CLI запускается из основного backend-venv проекта:

```bash
source .venv_krab_ear/bin/activate   # python3.14, из корня репозитория
```

**Уже установлено** в `.venv_krab_ear` (проверено 2026-07-09):

| Пакет | Версия | Для чего |
|---|---|---|
| `openwakeword` | 0.6.0 | сам движок + `train.py`/`data.py`/`utils.py` |
| `torch` | 2.11 (MPS доступен) | тренировка на Apple Silicon GPU |
| `torch_audiomentations` | — | часть аугментации (`augment_clips`) |
| `torchmetrics`, `pytorch_lightning` | — | метрики внутри `openwakeword.train.Model` |
| `onnxruntime` | — | ONNX-инференс фичеризатора (`AudioFeatures`) |
| `huggingface_hub` | 1.8.0 | скачивание официальных датасетов (этап `corpora`) |
| `scipy`, `torchaudio`, `numpy` | — | чтение WAV, расчёт длины клипа |

**🔴 РЕАЛЬНО НЕ УСТАНОВЛЕНО (проверено импортом, не по докам!)** — без этих
пакетов `openwakeword.data` и `openwakeword.train` **не импортируются вообще**
(они тянутся на уровне модуля пакета, до любого вызова функции):

```bash
pip install pronouncing audiomentations speechbrain mutagen acoustics
```

Это нужно для этапов `negatives` (опционально, только если используется
`generate_adversarial_texts`), `features` и `train`. `torchinfo` **не нужен** —
`train_krab.py` никогда не вызывает `Model.summary()`.

⚠️ **Риск (см. §5 спеки, риск #3):** `.venv_krab_ear` — Python 3.14, очень
свежий; часть из пяти пакетов выше может ещё не иметь официальных wheels под
3.14 на момент вашей сессии. Если `pip install` выше падает или
`--stage features`/`--stage train` падают на импорте — соберите отдельный
лёгкий venv под 3.11/3.12 **только для тренировки** (инференс-адаптер
(`openwakeword_adapter.py`) остаётся на `.venv_krab_ear`, ему это не мешает —
он использует только `openwakeword.model.Model`, который не тянет эти пакеты):

```bash
# На этой машине уже есть системные 3.11/3.12 (Homebrew):
/opt/homebrew/bin/python3.11 -m venv .venv_wake_training
source .venv_wake_training/bin/activate
pip install torch torchaudio openwakeword huggingface_hub scipy \
    pronouncing audiomentations speechbrain mutagen acoustics torch_audiomentations \
    torchmetrics pytorch_lightning onnxruntime
python wake_word_models/train_krab.py --stage features ...
```

### 1.2 Диск

| Что | Размер |
|---|---|
| `validation_set_features.npy` (fp-валидация, обязателен) | ~0.18 GB |
| MIT RIR (271 файлов, опционален) | несколько МБ |
| Синтезированные позитивы+негативы (WAV) | сотни МБ, зависит от `--positives-count`/`--*-target-count` |
| `.npy` фичи после аугментации | сотни МБ – единицы ГБ (зависит от `--augmentation-rounds`) |
| `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` (`--fetch-acav100m`, **опционально**) | **~17.3 GB** |
| Экспортированная модель (`krab_ru.onnx`) | единицы МБ |

Проверьте свободное место перед `--fetch-acav100m` — это самый тяжёлый файл в
пайплайне. Без него `train` всё равно работает, используя только
RU-синтетику как негативы (см. §4 «Гейт качества», риск ниже качества).

### 1.3 Сеть

Этап `corpora` качает с HuggingFace Hub (публичные датасеты, **не gated**, не
требуют токена/логина) — см. §6 «Откуда взяты датасеты» ниже для точных
репозиториев и URL.

---

## 2. Быстрый старт

```bash
source .venv_krab_ear/bin/activate
cd wake_word_models

# Справка по всем флагам (по этапам сгруппированы)
python train_krab.py --help

# Один этап за раз (рекомендуется для первого прогона — проще диагностировать)
python train_krab.py --stage corpora
python train_krab.py --stage positives
python train_krab.py --stage negatives
python train_krab.py --stage features
python train_krab.py --stage train
python train_krab.py --stage export
python train_krab.py --stage install

# Или всё сразу (та же последовательность, resume-friendly)
python train_krab.py --all
```

Каждый этап **resume-friendly**: если уже выполнялся (маркер-файл
`.done_<stage>.json` в рабочей директории этапа), он пропускается с сообщением
в лог. Добавьте `--force`, чтобы повторить этап заново (например, после
изменения флагов).

Прогресс печатается в stdout через `logging` (человекочитаемый формат,
`-v`/`--verbose` для DEBUG-уровня).

### Smoke-прогон на малом объёме

Перед полным прогоном (часы синтеза + обучения) можно проверить, что стадия
вообще работает, с `--limit`:

```bash
python train_krab.py --stage positives --limit 5
```

Это ограничивает количество генерируемых клипов сверху (не увеличивает —
если базовый target уже меньше `--limit`, используется меньшее).

---

## 3. Этапы подробно

| Этап | Что делает | Тяжёлые зависимости | Типичное время |
|---|---|---|---|
| `corpora` | Качает fp-валидацию (обязательно), опционально ACAV100M и MIT RIR | `huggingface_hub` | секунды–минуты (без `--fetch-acav100m`); +час(ы) на медленном канале с ним |
| `positives` | Синтезирует «Краб»/«эй, Краб» через Silero RU TTS (5 спикеров x SSML rate/pitch) | `torch` (только `torch.hub`, без openwakeword) | минуты–десятки минут на MPS/CPU для ~4000 клипов |
| `negatives` | Нейтральная RU-речь (~100+ предложений) + adversarial-слова (краба/корабль/прораб/крап/раб/граб...) | `torch` (+опц. `openwakeword.data` для доп. adversarial-текстов) | сопоставимо с `positives` |
| `features` | Аугментация (`augment_clips`: EQ/дисторшн/pitch-shift/band-stop/шум/громкость/реверб) + featurization в `.npy` | `openwakeword.data`+`openwakeword.utils` (требует доп. пакеты §1.1) | десятки минут – часы (зависит от `--augmentation-rounds` и объёма клипов) |
| `train` | `openwakeword.train.Model.auto_train()` + гейт `max_fp_per_hour` через `_select_best_model()` | `torch`+`openwakeword.train` | **часы** (реальная тренировка нейросети; `--steps 50000` — дефолт пакета) |
| `export` | Экспорт чекпоинта в `krab_ru.onnx` | `torch`+`openwakeword.train` | секунды |
| `install` | Копия в `{data_dir}/wake_word_models/krab_ru.onnx` + постпроверка загрузки | `openwakeword.model` (лёгкий, инференс-класс) | секунды |

Каждый этап можно тонко настроить флагами своей группы — см.
`python train_krab.py --help` (сгруппировано: Общие / corpora / positives /
negatives / features / train / install).

### Ключевые флаги (полный список — `--help`)

- `--phrase "Краб"` / `--secondary-phrase "эй, Краб"` (повторяемый флаг)
- `--speakers aidar,baya,kseniya,xenia,eugene` — Silero RU спикеры
- `--positives-count 4000`, `--neutral-target-count 1200`,
  `--adversarial-target-count 1200`
- `--max-fp-per-hour 1.0` (роадмаповский потолок), `--min-recall 0.20`,
  `--steps 50000`
- `--device auto|cpu|mps|cuda` (train-этап; `auto` предпочитает MPS на Apple
  Silicon — см. риск §5)
- `--work-dir PATH` — переопределить корень рабочих директорий (по умолчанию
  — сам `wake_word_models/`)
- `--data-dir PATH` — куда ставить готовую модель (по умолчанию prod:
  `~/Library/Application Support/KrabEar`)

---

## 4. Гейт качества (fp/час) и офлайн-отчёт

После `--stage train` в `wake_word_models/report_<model_name>.md`
(по умолчанию `report_krab_ru.md`) появляется отчёт с:

- метриками истории обучения (`val_accuracy`, `val_recall`, `val_fp_per_hr`,
  `val_n_fp` — последние 5 контрольных точек);
- результатом гейта: удовлетворён ли `max_fp_per_hour <= 1.0` (дефолт,
  соответствует потолку из роадмапа) при `min_recall >= 0.20`.

**Что такое "fp/час"**: сколько раз в среднем модель ложно сработает на **час**
речи/шума, которая НЕ содержит целевую фразу — измеряется прогоном модели по
fp-валидационному набору (~11 часов реальной речи/музыки/бытового шума,
скачивается на этапе `corpora`). Чем ниже — тем меньше модель "дёргается" на
случайную речь. `openwakeword.train.Model._select_best_model(...,
max_fp_per_hour=1.0, min_recall=0.20)` перебирает все сохранённые контрольные
точки тренировки и выбирает лучшую **среди тех, что укладываются в бюджет
fp/час**, максимизируя recall.

Если гейт не находит подходящего кандидата (`gate_satisfied: false` в
`.done_train.json` и явная пометка в отчёте) — используется усреднённая модель
из `auto_train()` (обычная логика пакета, топ-10% контрольных точек), а отчёт
подсказывает попробовать больше `--steps` или мягче `--max-fp-per-hour`/
`--min-recall`.

---

## 5. Известные риски (читайте перед прогоном)

1. **`generate_adversarial_texts` — English-only.** Функция
   `openwakeword.data.generate_adversarial_texts` официально документирована
   как работающая только для английского текста (использует CMUdict через
   пакет `pronouncing`). Для кириллического слова CMUdict вернёт `[]`, что
   уводит в OOV-ветку с англоязычной моделью DeepPhonemizer (качается с S3
   при первом вызове) — результат для «краб» непредсказуем. `train_krab.py`
   вызывает её **best-effort** (флаг `--use-oww-adversarial-texts`,
   default on) и **никогда не роняет пайплайн** при сбое — основная защита
   негативов остаётся вшитый список из ~49 фонетически близких RU-слов
   (краба/крабы/корабль/прораб/крап/раб/граб/трап/храп/скраб/...).
2. **MPS не автоопределяется пакетом.** `openwakeword.train.Model.__init__`
   выбирает `device` только между CUDA и CPU — на Apple Silicon без явного
   оверрайда тренировка тихо осталась бы на CPU. `train_krab.py` сам
   проставляет `oww.device = torch.device("mps")` при `--device auto|mps`
   (см. `_resolve_torch_device`). `--model-type rnn` (LSTM) может не иметь
   полной поддержки MPS в вашей версии PyTorch — дефолт `dnn` полностью
   MPS-совместим.
3. **SSML rate/pitch — best-effort.** Не все версии Silero-хаба поддерживают
   `ssml_text=` в свободной функции `apply_tts` (распаковка из
   `torch.hub.load`, тот же паттерн, что `backend/tts_service.py`). Если
   текущая версия не поддерживает — синтез автоматически деградирует на
   обычный текст без вариации просодии (в логе будет одно предупреждение).
   Не блокирует пайплайн; проверьте вживую после `--stage positives`,
   прослушав пару клипов.
4. **Общий негативный корпус (ACAV100M) опционален и тяжёлый (~17.3 GB).**
   Без `--fetch-acav100m` на этапе `corpora` тренировка использует ТОЛЬКО
   RU-синтетику (нейтральные предложения + adversarial-слова) как негативы —
   рабочий вариант для v1, но потенциально более слабый гейт качества
   (отмечается в логе `train`-этапа и косвенно видно по итоговому
   `val_fp_per_hr` в отчёте).
5. **RIR (реверберация) опциональны.** Без них `augment_clips()` штатно
   пропускает шаг реверберации (пакет поддерживает пустой `RIR_paths=[]` из
   коробки) — клипы будут звучать более «сухо», без имитации разных комнат.
6. **`torch.load(weights_only=False)` в `--stage export`.** PyTorch ≥2.6
   поменял дефолт на `weights_only=True`; наш чекпоинт (`*_checkpoint.pt`) —
   результат `Model.save_model()`, который сохраняет весь объект `nn.Module`
   целиком (не только веса), поэтому `export` явно передаёт
   `weights_only=False`. Это безопасно **только** для доверенного локального
   файла, созданного этим же скриптом на этапе `train` — не подсовывайте сюда
   чужие/скачанные `.pt`.
7. **URL/датасеты могут дрейфовать со временем.** Все репозитории и имена
   файлов в §6 ниже сверены напрямую на HuggingFace Hub API 2026-07-09. Если
   `--stage corpora` падает на 404 — проверьте актуальность на
   https://huggingface.co/datasets/davidscripka/openwakeword_features и
   https://huggingface.co/datasets/davidscripka/MIT_environmental_impulse_responses,
   поправьте через `--features-repo`/`--fp-validation-file`/`--acav100m-file`/
   `--rir-repo`/`--rir-pattern`.

---

## 6. Откуда взяты датасеты

Сверено 2026-07-09 напрямую в исходниках
[dscripka/openWakeWord](https://github.com/dscripka/openWakeWord)
(`notebooks/automatic_model_training.ipynb`) и через HuggingFace Hub API
(`GET /api/datasets/<repo>`, `GET /api/datasets/<repo>/tree/main`). Пакет
`openwakeword` 0.6.0, установленный локально, **не содержит** этих URL в
коде (`openwakeword/data.py`/`train.py` ожидают, что пользователь сам
подготовит фичи/RIR/фон — см. `config["false_positive_validation_data_path"]`,
`config["rir_paths"]`, `config["background_paths"]` в `train.py::__main__`) —
поэтому источники ниже взяты из официального проектного ноутбука, не из
самого пакета.

| Ресурс | Репозиторий (HF dataset) | Файл/паттерн | Размер | Обязателен? |
|---|---|---|---|---|
| FP-валидация | `davidscripka/openwakeword_features` | `validation_set_features.npy` | ~0.18 GB (~11ч) | **Да** — используется как гейт `max_fp_per_hour` |
| ACAV100M негативы | `davidscripka/openwakeword_features` | `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` | ~17.3 GB | Нет (`--fetch-acav100m`) |
| MIT RIR | `davidscripka/MIT_environmental_impulse_responses` | `16khz/*.wav` (271 файл) | несколько МБ | Нет (`--skip-rir` чтобы пропустить) |

Фоновый шум (AudioSet/FMA из оригинального ноутбука) **не автоматизирован** —
`augment_clips()` штатно работает без него (`AddBackgroundNoise` просто не
подключается в `Compose`, если `background_clip_paths=[]`). Если хотите
подключить свой каталог фоновых WAV — `--background-dir /path/to/wavs` на
этапе `features`.

---

## 7. Установка и выбор модели в Settings

`--stage install` копирует `krab_ru.onnx` в
`{data_dir}/wake_word_models/krab_ru.onnx` (prod: `~/Library/Application
Support/KrabEar/wake_word_models/`) и проверяет, что `openwakeword.model.Model`
может её загрузить (`--skip-load-check` чтобы пропустить эту проверку).

**Swift-код менять не нужно** — пикер моделей в Settings уже существует
(`HistoryPanelController+Settings.swift`) и **динамически** перезаполняется
из IPC `wake_word_list_models`, который сканирует именно эту директорию
(`backend/openwakeword_adapter.py::list_models()`). После установки:

1. Перезапустите backend (или подождите — `list_models()` сканирует
   директорию при каждом вызове, кэша нет).
2. Откройте Krab Ear → Настройки → секция Wake Word.
3. В выпадающем списке появится `krab_ru` (source: `custom`).
4. Выберите её, настройте порог слайдером, «Включить».

---

## 8. Протокол owner-валидации (T5, вручную владельцем)

T1–T4 (эта инфраструктура + прогон + экспорт + установка) не требуют участия
владельца. **T5 — обязательно живой тест голосом**, автоматизировать нечем:

1. Установите модель (§7 выше), выберите `krab_ru` в Settings, порог по
   умолчанию 0.5 (слайдер).
2. **Recall-тест**: произнесите «Краб» (и «эй, Краб») ~20 раз в обычной для
   вас манере (разная громкость/дистанция от микрофона/скорость речи).
   Считайте, сколько раз сработало (`wake_word_status.last_detection`
   обновляется — видно в UI или через `wake_word_status` IPC).
3. **False-positive тест**: включите детекцию и займитесь обычными делами
   (разговор, музыка, ТВ) на фоне **час** (или используйте кратный интервал
   и экстраполируйте) — считайте ложные срабатывания. Цель роадмапа:
   **≤ 1 ложного/час**.
4. Если recall низкий — попробуйте снизить порог слайдером (осторожно,
   поднимает fp/час). Если fp/час высокий — поднимите порог, либо вернитесь
   к `--stage train` с более жёстким `--max-fp-per-hour` (например, `0.5`)
   и большим `--steps`.
5. Зафиксируйте вердикт в `docs/ROADMAP-2026H2.md` (статус Волны 3a) —
   этим шагом Волна 3a закрывается.

Опционально (не блокер v1): дообучение на реальных фразах владельца для
повышения recall на нестандартной просодии — отдельный шаг, не входит в эту
поставку.

---

## 9. Структура директорий (создаются командами, не коммитятся)

```
wake_word_models/
├── train_krab.py          # эта поставка -- коммитится
├── README.md               # эта инструкция -- коммитится
├── .gitignore               # коммитится
├── report_krab_ru.md        # генерируется --stage train -- коммитится (см. .gitignore)
├── corpora/                 # --stage corpora   (гитигнор)
│   ├── validation_set_features.npy
│   ├── openwakeword_features_ACAV100M_2000_hrs_16bit.npy   # опционально
│   └── rir/16khz/*.wav                                      # опционально
├── positives/                # --stage positives (гитигнор)
│   ├── train/*.wav
│   └── test/*.wav
├── negatives/                # --stage negatives (гитигнор)
│   ├── ru_synthetic/*.wav
│   └── adversarial/*.wav
├── features/                 # --stage features  (гитигнор)
│   ├── positive_features_train.npy
│   ├── positive_features_test.npy
│   ├── negative_features_train.npy
│   └── negative_features_test.npy
└── artifacts/                 # --stage train/export (гитигнор)
    ├── krab_ru_checkpoint.pt
    └── krab_ru.onnx
```

---

## 10. Troubleshooting

- **`ImportError: openwakeword.data недоступен` на `features`/`negatives`/`train`** —
  доустановите пакеты из §1.1 (`pronouncing audiomentations speechbrain
  mutagen acoustics`). Если это упирается в несовместимость с Python 3.14 —
  см. fallback-venv в §1.1.
- **`--stage corpora` падает на скачивании** — см. риск #7 в §5, датасеты
  могли переехать; проверьте URL вручную на HuggingFace.
- **Тренировка не использует GPU** — проверьте лог `train: device=...`;
  `--device auto` должен вывести `mps` на Apple Silicon. Если видите `cpu` —
  проверьте `torch.backends.mps.is_available()` в вашем окружении
  (`python -c "import torch; print(torch.backends.mps.is_available())"`).
- **`torch.load` ругается на `weights_only`** — актуально только если вы
  подсунули export-этапу чужой чекпоинт; со своим (созданным `--stage train`)
  это не должно происходить (см. риск #6 в §5).
- **Юнит-тесты** (только чистые хелперы, без сети/GPU/torch):
  ```bash
  PYTHONPATH=$(pwd)/KrabEar python -m pytest \
      KrabEar/tests/test_wake_training_helpers_W3a.py -v
  ```
