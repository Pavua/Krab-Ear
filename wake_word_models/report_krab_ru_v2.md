# Отчёт обучения wake-word модели «Краб» (krab_ru_v2)

- Дата: 2026-07-13 00:23 UTC
- input_shape: (16, 96)
- steps: 50000, model_type: dnn, layer_size: 128
- device: mps
- Режим: FINE-TUNE от artifacts/krab_ru_checkpoint.pt
- Гейт: max_fp_per_hour<=1.0, min_recall>=0.75, val_set_hrs=11.3

## Результат гейта

Гейт max_fp_per_hour<=1.0 не нашёл кандидата с recall>=0.75 среди контрольных точек -- использована комбинированная модель auto_train (усреднение >90-го перцентиля). Метрики истории обучения см. ниже; перед owner-валидацией (T5) рассмотрите больше --steps или мягче --max-fp-per-hour/--min-recall.

## История обучения (val_* метрики по контрольным точкам)

- **val_accuracy** (последние 5 из 55): 0.7924, 0.7924, 0.7935, 0.7924, 0.7924
- **val_recall** (последние 5 из 55): 0.7063, 0.7038, 0.7038, 0.7038, 0.7038
- **val_fp_per_hr** (последние 5 из 55): 0.0000, 0.0000, 0.0000, 0.0000, 0.0000
- **val_n_fp** (последние 5 из 55): 118.0000, 116.0000, 114.0000, 116.0000, 116.0000

## Артефакты

- Чекпоинт: `/Users/pablito/Antigravity_AGENTS/Krab Ear/wake_word_models/artifacts/krab_ru_v2_checkpoint.pt`

Следующий шаг: `--stage export`, затем `--stage install`, затем T5 (живая owner-валидация голосом -- см. README «Протокол owner-валидации»).
