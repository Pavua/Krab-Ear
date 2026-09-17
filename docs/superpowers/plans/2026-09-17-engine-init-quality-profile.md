# engine читает сохранённый quality_profile при конструировании — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `AudioEngine.__init__` берёт `quality_profile` из сохранённых настроек вместо хардкода `"balanced"`.

**Architecture:** цепочка `BackendService._get_runtime_setting` → `Transcriber(settings_get=...)` (`KrabEar/backend/transcriber.py:61`) → `AudioEngine(settings_get=...)` уже существует и несёт сохранённые значения (`service.py:512-514`, `transcriber.py:50-67`). Фикс — прочитать ключ в `__init__` через уже инжектированный колбэк. Ловушка порядка: `self._settings_get` присваивается только на строке 530, а хардкод стоит на 488 — читать профиль можно только ПОСЛЕ 530.

**Tech Stack:** Python, `unittest`. Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/engine-init-quality-profile`, ветка `fix/engine-init-quality-profile`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не менять выбор модели** (`current_model`, `set_quality_profile`, warmup) — только начальную строку профиля; не менять `Transcriber`/`BackendService`.

---

## Проверенные факты (координатор, 17.09, file:line)

- `KrabEar/core/engine.py:488` — `self.quality_profile = "balanced"` (хардкод).
- `KrabEar/core/engine.py:530` — `self._settings_get = settings_get or (lambda k, d: d)`.
- `KrabEar/core/config.py:837` — `"quality_profile": "balanced"` в `DEFAULT_SETTINGS` (ключ существует).
- `KrabEar/backend/service.py:4431` — diagnostics читает `settings.get("quality_profile", "balanced")` (сохранённое) — вот почему diagnostics врёт до первой записи.
- `KrabEar/backend/service.py:512-514` — `BackendService` передаёт `settings_get=self._get_runtime_setting` (сохранённые настройки, не дефолты).
- Конструкция движка в тестах: `AudioEngine(skip_gigaam_warmup=True)` (прецедент `test_engine_edge_cases.py:30-33`, MLX мокируется, `service.close()` не нужен — это не `BackendService`).

---

### Task 1: Тест инициализации профиля (RED)

**Files:**
- Create: `KrabEar/tests/test_engine_init_quality_profile.py`

- [ ] **Step 1: Написать тест**

```python
"""engine читает сохранённый quality_profile при конструировании (волна 0.3).

Долг §1.4 Q4-плана: engine.py хардкодил "balanced" до первой записи, а
diagnostics (service.py:4431) читает сохранённое значение — расхождение,
видимое пользователю до первой диктовки.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine


def _make_engine(settings_get=None):
    """Как в test_engine_edge_cases: без GigaAM-worker'а, MLX мокируется."""
    return AudioEngine(settings_get=settings_get, skip_gigaam_warmup=True)


class EngineInitQualityProfileTests(unittest.TestCase):
    def test_saved_max_is_applied(self) -> None:
        eng = _make_engine(settings_get=lambda k, d: "max")
        self.assertEqual(eng.quality_profile, "max")

    def test_default_without_callback_is_balanced(self) -> None:
        eng = _make_engine()
        self.assertEqual(eng.quality_profile, "balanced")

    def test_junk_value_falls_back_to_balanced(self) -> None:
        for junk in ("ultra", "", None, 123):
            with self.subTest(junk=junk):
                eng = _make_engine(settings_get=lambda k, d, j=junk: j)
                self.assertEqual(eng.quality_profile, "balanced")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Прогнать — ждём RED ровно по сохранённому max**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_engine_init_quality_profile.py -v -p no:cacheprovider
```

Ожидаемо: `test_saved_max_is_applied` — FAILED (`'balanced' != 'max'`), остальные 2 — passed. Если упали по другой причине (импорт, MLX) — **стоп**, доложить координатору.

### Task 2: Фикс `__init__` (GREEN)

**Files:**
- Modify: `KrabEar/core/engine.py` (строки 488 и 530, сверь `sed -n '486,490p;528,532p'` перед правкой)

- [ ] **Step 1:** удалить строку 488 `self.quality_profile = "balanced"`.

- [ ] **Step 2:** сразу после строки 530 (`self._settings_get = ...`) вставить:

```python
        # 0.3: сохранённый профиль вместо хардкода — иначе diagnostics
        # (читает сохранённое, service.py:4431) врёт до первой записи.
        _saved_profile = self._settings_get("quality_profile", "balanced")
        self.quality_profile = _saved_profile if _saved_profile in {"balanced", "max"} else "balanced"
```

- [ ] **Step 3: GREEN** — та же команда, что в Task 1 Step 2. Ожидаемо: `3 passed`.

- [ ] **Step 4: Регрессия соседей** (профиль трогает инициализацию — гоняем файлы, строящие движок):

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_engine_edge_cases.py -q -p no:cacheprovider
```

Ожидаемо: зелёный (тот же результат, что на чистой базе; сверь при сомнении `git stash` — но stash в worktree с тестом ок, не в общем чекауте).

- [ ] **Step 5: Гейт**

```bash
python -m flake8 --max-line-length=120 KrabEar/tests/test_engine_init_quality_profile.py KrabEar/core/engine.py
scripts/pre_merge_py312_check.sh KrabEar/tests/test_engine_init_quality_profile.py
```

Ожидаемо: без ошибок; ubuntu-parity — PASS.

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `fix/engine-init-quality-profile`
- [ ] `git add` **явными путями**: `KrabEar/tests/test_engine_init_quality_profile.py`, `KrabEar/core/engine.py`
- [ ] Коммит: `fix(engine): quality_profile из сохранённых настроек при init`
- [ ] PR в `codex/krab-ear-v2`; НЕ мержить (мержит координатор после гейта).

## Definition of Done

- 3 теста RED→GREEN; регрессия `test_engine_edge_cases.py` зелёная.
- `current_model`, `set_quality_profile`, warmup, `Transcriber`, `BackendService` не менялись (проверить `git diff --stat`).
- Прод-backend, REST и агент не перезапускались.

## Вне scope (записать в отчёт, не чинить)

`current_model` при старте всегда `settings.MODEL_BALANCED` (модульная константа, не сохранённое) — выбор модели по сохранённому профилю происходит только в `set_quality_profile`. Если это тоже врёт diagnostics — отдельной карточкой.
