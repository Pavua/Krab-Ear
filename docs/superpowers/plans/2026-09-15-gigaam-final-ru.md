# GigaAM-final для RU Implementation Plan

**Goal:** финалы русской диктовки идут через прогретый GigaAM-MLX,
Whisper остаётся fallback. Убрать cold-start проигрыш; conf-заглушка
больше не единственный гейт доверия.

**Architecture:** Evidence 14–15.09 (live): cold-start MLX после загрузки
теряет кусок 0.0–1.0 с → каскад (текст цел за счёт Whisper); прогретый
MLX ведёт целую диктовку сам (0.3–1.0 с/пасс, ноль ошибок);
`confidence=0.9 constant` — цепь не может доверять GigaAM финалы
(#1985 ретрай мёртв); финалы сейчас принудительно через max/Whisper
(conf 0.42–0.67, дроп безударного «НЕ» — классика Whisper).
Волна: T1 замер (bake-off harness, синтетика), T2 прогрев/warmup-путь,
T3 роутинг финалов (GigaAM-first + Whisper-fallback, настоящий health-gate
вместо conf-константы), T4 гейты. Только синтетическое аудио
(`say -v Milena` + μ-law деградация) — живые диктовки владельца в тесты
не несут (privacy).

**Tech Stack:** то, что в репо: `stt_gigaam.py` (MLX-адаптер),
`stt_router.py`/`engine.py` (chain/fallback), `KrabEar/tests/` hermetic.
Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Ветка исполнения: `feat/gigaam-final-ru`
от колеи (параллельных сессий нет — проверить `git status` перед стартом).

**Баны:** вставь список из `docs/EXECUTOR_PLAYBOOK.md` §1. Плюс: живые
диктовки/аудио владельца — не в тесты и не в логиwave (только синтетика);
прод-рестарты — только `safe_backend_restart.command` и вне его диктовки;
мерж в колею — только после ревью чисел владельцем (смена поведения chain);
`git add` явными путями.

---

### Task 1: bake-off harness (RED)

**Files:**
- Create: `KrabEar/tests/test_gigaam_final_bakeoff_2026_09_15.py`
- Modify: нет source-правок в этом таске

- [ ] **Step 1: Write the failing test**

Синтетический корпус (генерируется в `setUpClass` один раз, в tmp):
3–5 RU-фраз через `say -v Milena` → 16 кГц mono + μ-law-вариант
(деградация `scripts/`-хелпером или инлайн через `audioop`-замену
проекта). Эталоны — исходные строки (известны заранее, не STT).

Тест гоняет каждую фразу двумя путями ( directly adapter calls, без
сокетов/прод-состояния):
1. `GigaAM-MLX` (subprocess worker запрещён в этом тесте — только MLX;
   пропустить весь файл, если MLX недоступен: `pytest.importorskip`).
2. Текущий Whisper-final путь (как baseline).
Метрики на фразу: точное совпадение с эталоном (строго; фиксирует дроп
«НЕ» и подобные), wall-latency, chunk-loss (пустые куски при sounding
audio — класс cold-start 14.09).
Агрегат: `assert` суммарных ошибок GigaAM <= ошибок Whisper И p95
латентности GigaAM <= Whisper — сейчас ПАДАЕТ (финалы идут через
Whisper по дизайну + cold chunk-loss), это и есть RED.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_gigaam_final_bakeoff_2026_09_15.py -v`
Expected: FAIL — GigaAM-ветка либо теряет cold-кусок, либо вообще не
вызывается финальным путём (по дизайну), либо хуже baseline.

Outcome 15.09 (честно): на тёплой машине — 3/3 PASS за ~6 с, cold-loss
НЕ воспроизвёлся (Metal/кэши горячие после ночных диктовок; W957-гард
поймал первую попытку — сеть запрещена, добавлен офлайн-режим HF как
у прод-процесса). Harness валиден как regression-lock, но НЕ как RED:
детерминированный RED волны — шов роутинга (финал RU идёт в Whisper
по дизайну, см. T3). По пути поправлена W957-дыра harness (offline).
T2/T3 идут от шва роутинга, не от cold-repro.

### Task 2: прогрев (warmup-путь)

**Files:**
- Modify: прогрев GigaAM-MLX при смене транспорта / старте записи
  (кандидаты: `stt_gigaam.py`, `recording_core_service.py` —
  точное место по RED из T1)
- Test: тот же bake-off + отдельный unit на «первый кусок после
  прогрева не теряется»

- [ ] Прогрев обязан быть bounded (бюджет из существующего модуля
  бюджетов, не magic number) и не грузить модель при пустом Studio
  (политика #1999: пустой каталог ≠ недоступность — не `lms load`).
- [ ] Expected: cold chunk-loss = 0 на прогретом адаптере.

### Task 3: роутинг финалов

**Files:**
- Modify: chain/fallback (`stt_router.py` / `engine.py` — по RED)
- Test: bake-off GREEN + существующие `test_gigaam_*`, `test_stt_*`,
  `test_whisper_*` без регрессий

- [ ] Финал RU: GigaAM-first при warm+healthy, Whisper-fallback сохранён.
- [ ] Health-gate вместо conf-константы: `confidence_source=constant`
  нельзя использовать как сигнал доверия (#1985). Доверять готовности
  адаптера (warm, recent success), не числу.
- [ ] Expected: bake-off PASS (ошибки GigaAM <= Whisper, p95 <=),
  все смежные suites зелёные.

### Task 4: Gate + commit (без мержа)

- [ ] `scripts/pre_merge_py312_check.sh` на новые/изменённые test-файлы.
- [ ] `make audit-all`, если тронуты `core/pipeline/` или сервисы.
- [ ] Полный прогон затронутых test-файлов.
- [ ] Commit явными путями. Мерж — только после ревью чисел владельцем.

```bash
git add <явные пути>
git commit -m "feat(stt): GigaAM-final для RU с Whisper-fallback"
```
