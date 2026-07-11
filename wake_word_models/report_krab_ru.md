# Отчёт обучения wake-word модели «Краб» (krab_ru)

- Дата: 2026-07-09 10:07 UTC
- input_shape: (16, 96)
- steps: 50000, model_type: dnn, layer_size: 128
- device: auto
- Гейт: max_fp_per_hour<=1.0, min_recall>=0.2, val_set_hrs=11.3

## Результат гейта

Гейт max_fp_per_hour<=1.0 УДОВЛЕТВОРЁН выбранной контрольной точкой.

## История обучения (val_* метрики по контрольным точкам)

- **val_accuracy** (последние 5 из 55): 0.7523, 0.7531, 0.7523, 0.7523, 0.7523
- **val_recall** (последние 5 из 55): 0.8000, 0.8012, 0.8025, 0.8012, 0.8012
- **val_fp_per_hr** (последние 5 из 55): 0.0000, 0.0000, 0.0000, 0.0000, 0.0000
- **val_n_fp** (последние 5 из 55): 157.0000, 157.0000, 159.0000, 158.0000, 158.0000

## Артефакты

- Чекпоинт: `/Users/pablito/Antigravity_AGENTS/Krab Ear/wake_word_models/artifacts/krab_ru_checkpoint.pt`

Следующий шаг: `--stage export`, затем `--stage install`, затем T5 (живая owner-валидация голосом -- см. README «Протокол owner-валидации»).
