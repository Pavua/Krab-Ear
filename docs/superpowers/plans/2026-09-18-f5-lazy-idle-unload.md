# F5: ленивая выгрузка модели семантического поиска в простое — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** модель e5-base (~1.9 ГБ RAM) не висит вечно: выгружается при простое ≥ порога и лениво поднимается следующим запросом. IPC-поверхность и индекс не меняются.

**Architecture:** (1) `SemanticSearcher` учится `last_used_ts` (monotonic, отметка в `_get_model` — единственная точка входа для search/index), `unload_model()` (гасит ТОЛЬКО модель/флаг; индекс в RAM и на диске не трогается — прецедент `reset_model_error:261-283`, его docstring прямо фиксирует, что индекс сохраняется намеренно) и `unload_if_idle(threshold)`. (2) `MemoryConductor` получает два опциональных провайдера (`semantic_unload_if_idle`, `semantic_idle_sec_fn`) и шаг `_semantic_step()` в `tick_once` рядом с `_gigaam_step`/`_rewriter_step`. 🔴 Выгрузка ЗАВСЕГДА ВКЛЮЧЕНА (без `enforce_for`): семантика — CPU-модель, НЕ GPU-резидент, D5 одобрен владельцем; порог `semantic_search_idle_unload_sec` (дефолт 1800 с, **0 = выключено**). `_RESIDENTS` НЕ трогаем (никакой pressure/shadow-семантики). (3) `service.py` проводит провайдеры (зеркало gigaam-пары :712-730), `core/config.py` + `settings_validator.py` — ключ и диапазон.

**Tech Stack:** Python `unittest` + `MagicMock` (fake-model паттерн `test_semantic_search.py:33`). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/f5-lazy-unload`, ветка `feat/f5-lazy-unload`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не трогать `_RESIDENTS`, `enforce_for`, существующие шаги кондуктора и brain-политику**; не менять IPC-поверхность (новых методов НЕ добавлять — `semantic_search_status` уже отдаёт `model_loaded`); `semantic_search_enabled` остаётся `False` (включает владелец отдельно); не трогать `purge_all`/`_load_from_disk`/`_save_locked`; сети в тестах нет (fake-модель, `sys.modules`-патч).

---

## Проверенные факты (координатор, 18.09, file:line)

- `SemanticSearcher` (`backend/semantic_search.py`): `__init__:29` (поля `_model/_model_loaded/_model_error:45-48`, `_embeddings/_index:51-53`, `_model_lock:46`, `_index_lock:53`); свойства `is_enabled:74-76`, `model_loaded:78-80`; `status():86-95` (уже отдаёт `model_loaded`); `reset_model_error:261-283` — прецедент «сбросить `_model`/`_model_loaded` БЕЗ `_model_error`, индекс сохранить»; `_get_model:355-377` (ранний `return` если `_model_loaded`; свежая загрузка → `_load_from_disk:368`); `search:228-258` (гейт `enabled` → `_get_model` → копия матрицы под `_index_lock`; пустой индекс → `[]`); `index_item:108`, `index_all:149` — тоже зовут `_get_model`; `_save_locked` после каждой мутации → диск == RAM (важно для переживания unload).
- Кондуктор (`backend/memory_conductor.py`): `_RESIDENTS:35` (`gigaam/rewriter/brain`); `__init__:71+` принимает `gigaam_close_if_idle`/`gigaam_idle_sec_fn:75-76`, `tick_sec=30:83`; `tick_once:218-243` зовёт `_gigaam_step(recording)`, `_rewriter_step(recording)`, `_brain_step(...)`; `_gigaam_step:306-327` — образец (порог из `_get(...)`, `idle = idle_sec_fn()`, исключение → return, `enforce_for` → shadow-счётчик); `_get:161-166` через `self._settings()`.
- Проводка (`backend/service.py:712-730`): `_gigaam_idle_sec()`/`_gigaam_close_if_idle()` — локальные функции-замыкания; `MemoryConductor(...)` конструируется там же (`:723+`). `self._semantic_searcher` создаётся `:1394-1399` (`enabled` — снапшот настроек на старте; смена — рестарт, вне scope).
- Конфиг: семантика — `core/config.py:1128-1130` (`semantic_search_enabled/model/auto_index`), `MAX_ITEMS=5000`; валидатор — `backend/settings_validator.py:105-114` (диапазоны conductor-порогов; формат `(low, high, default, type)`).
- Тесты-прецеденты: `test_semantic_search.py` (`_make_fake_model:33`, прямая подмена `_model`+`_encode/_encode_batch`, lazy-load через `sys.modules`-патч `sentence_transformers`); кондуктор — `test_memory_conductor_2026_08_19.py` + `test_memory_conductor_wiring_2026_08_19.py` (переиспользовать их фикстуры; `_semantic_step` тестировать прямым вызовом, как принято для шагов).

---

### Task 1: Тесты (RED)

**Files:**
- Create: `KrabEar/tests/test_semantic_idle_unload.py`

- [ ] **Step 1: Написать тесты** (секция Searcher — дословно; секция кондуктора — по образцу существующей фикстуры `test_memory_conductor_2026_08_19.py`, минимальный скелет ниже):

```python
"""F5: ленивая выгрузка модели семантического поиска в простое (D5).

D5 (владелец): выгрузка ~1.9 ГБ при простое + ленивый ре-подъём.
Индекс (RAM/диск) при unload НЕ трогается — прецедент reset_model_error.
"""
from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.semantic_search import SemanticSearcher  # noqa: E402

import numpy as np  # noqa: E402


def _make_fake_model(dim: int = 4):
    class _FakeModel:
        def encode(self, texts, **kwargs):
            return np.ones((len(texts), dim), dtype="float32")

    return _FakeModel()


class SearcherUnloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.searcher = SemanticSearcher(data_dir=Path(self.tmp.name), enabled=True)
        self.searcher._model = _make_fake_model()
        self.searcher._model_loaded = True

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unload_drops_model_keeps_index_and_allows_reload(self) -> None:
        marker = np.ones((1, 4), dtype="float32")
        self.searcher._embeddings = marker
        self.searcher._index = ["id1"]
        self.assertTrue(self.searcher.unload_model())
        self.assertIsNone(self.searcher._model)
        self.assertFalse(self.searcher.model_loaded)
        self.assertIsNone(self.searcher._model_error)  # ре-подъём разрешён
        self.assertIs(self.searcher._embeddings, marker)  # индекс не тронут
        self.assertEqual(self.searcher._index, ["id1"])

    def test_unload_idempotent(self) -> None:
        self.assertTrue(self.searcher.unload_model())
        self.assertFalse(self.searcher.unload_model())

    def test_unload_if_idle_keeps_fresh_model(self) -> None:
        self.searcher._last_used_ts = time.monotonic()
        self.assertFalse(self.searcher.unload_if_idle(600.0))
        self.assertTrue(self.searcher.model_loaded)

    def test_unload_if_idle_drops_stale_model(self) -> None:
        self.searcher._last_used_ts = time.monotonic() - 3600.0
        self.assertTrue(self.searcher.unload_if_idle(600.0))
        self.assertFalse(self.searcher.model_loaded)

    def test_get_model_marks_last_used(self) -> None:
        self.searcher._last_used_ts = 0.0
        self.searcher._get_model()
        self.assertGreater(self.searcher._last_used_ts, 0.0)

    def test_search_after_unload_relazy_loads(self) -> None:
        self.searcher._encode = lambda model, text: np.ones(4, dtype="float32")
        self.searcher._encode_batch = lambda model, texts: np.ones((len(texts), 4), dtype="float32")
        self.searcher.index_item("id1", "текст один")
        self.assertTrue(self.searcher._model_loaded)
        self.assertTrue(self.searcher.unload_model())
        fake_st = types.ModuleType("sentence_transformers")
        fake_st.SentenceTransformer = lambda name: _make_fake_model()
        with mock.patch.dict(sys.modules, {"sentence_transformers": fake_st}):
            results = self.searcher.search("текст")
        self.assertEqual([r["id"] for r in results], ["id1"])


if __name__ == "__main__":
    unittest.main()
```

🔴 Две проверки перед прогоном (сверить `rg`, при несовпадении — править ТЕСТ под код): `index_item(id, text)` — точная сигнатура (`semantic_search.py:108`); `SemanticSearcher.enabled=True` в конструкторе достаточен для `search` (гейт `_enabled`).

- [ ] **Step 2: Секция кондуктора** — `SemanticStepTests` в том же файле: `_semantic_step` вызывается напрямую; фикстура кондуктора — из `test_memory_conductor_2026_08_19.py` (найти, как он строит `MemoryConductor` + мок settings-сервиса с `cached_settings()`; переиспользовать). Ассерты:
  - idle_fn → `99999.0`, провайдер `semantic_unload_if_idle` вызван с порогом `1800.0` (порог задать через `cached_settings().get("semantic_search_idle_unload_sec")`);
  - idle_fn кидает → шаг молчит (провайдер не вызван);
  - idle_fn → `10.0` (< порога) → провайдер не вызван;
  - 🔴 `enforce_for` НЕ влияет: при любых `memory_conductor_enforce*` = False провайдер всё равно вызван (это и есть отличие от gigaam-шага — зафиксировать комментарием).
  - порог 0 → `_semantic_step` не вызывает провайдер (выключено; если реализация проверяет `threshold <= 0 → return` — так и задокументировать).

- [ ] **Step 3: RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_semantic_idle_unload.py -v -p no:cacheprovider
```
Ожидаемо: FAIL/ERROR по отсутствию `unload_model`/`unload_if_idle`/`last_used_ts`/`_semantic_step` (классификацию каждого теста — в отчёт). Если падает иначе (фикстура кондуктора) — стоп координатору.

### Task 2: Реализация (GREEN)

**Files:**
- Modify: `KrabEar/backend/semantic_search.py`
- Modify: `KrabEar/backend/memory_conductor.py`
- Modify: `KrabEar/backend/service.py`
- Modify: `KrabEar/core/config.py`
- Modify: `KrabEar/backend/settings_validator.py`

- [ ] **Step 1: Searcher** — в `__init__` рядом с `_model_loaded`: `self._last_used_ts: float = 0.0`; в `_get_model` ПЕРВОЙ строкой под `_model_lock`: `self._last_used_ts = time.monotonic()` (импорт `time` уже есть — сверить). Новые методы рядом с `reset_model_error`:

```python
    def unload_model(self) -> bool:
        """Гасит ТОЛЬКО модель (память); индекс в RAM/на диске не трогает.

        Идемпотентен. `_model_error` НЕ выставляется — следующий запрос
        лениво поднимет модель заново (прецедент reset_model_error:261).
        """
        with self._model_lock:
            if self._model is None and not self._model_loaded:
                return False
            self._model = None
            self._model_loaded = False
        logger.info("semantic_search: модель выгружена (idle unload)")
        return True

    def unload_if_idle(self, idle_sec: float) -> bool:
        """Выгружает модель, если она простаивает >= idle_sec секунд."""
        try:
            idle = time.monotonic() - float(self._last_used_ts)
            threshold = float(idle_sec)
        except (TypeError, ValueError):
            return False
        if threshold <= 0 or idle < threshold:
            return False
        return self.unload_model()
```

- [ ] **Step 2: Кондуктор** — в `__init__` сигнатуре рядом с gigaam-парой: `semantic_unload_if_idle: Callable[[float], bool] = None`, `semantic_idle_sec_fn: Callable[[], float] = None`; в теле — присваивания (`self.semantic_unload_if_idle = ...`, `self._semantic_idle_sec_fn = ...`). Новый шаг:

```python
    def _semantic_step(self) -> None:
        """F5/D5: idle-выгрузка CPU-модели семантического поиска.

        🔴 Всегда включено (без enforce_for): не GPU-резидент, не участвует
        в pressure/shadow-политике; порог 0 = выключено. memory_conductor_enabled
        остаётся общим гейтом (tick_once).
        """
        threshold = self._get("semantic_search_idle_unload_sec", 1800.0)
        if threshold <= 0 or self.semantic_unload_if_idle is None:
            return
        try:
            idle = float(self._semantic_idle_sec_fn())
        except Exception:
            return
        if idle < threshold:
            return
        try:
            if self.semantic_unload_if_idle(threshold):
                self._note("unloaded semantic model (idle %.0fs)" % idle)
        except Exception:
            logger.exception("semantic unload failed")
```

и вызов в `tick_once` после `self._rewriter_step(recording)` (перед `_brain_step`). 🔴 `_RESIDENTS`, `enforce_for`, `_gigaam_step`, `_rewriter_step`, `_brain_step` — не трогать.

- [ ] **Step 3: Проводка** (`service.py` рядом с gigaam-замыканиями :712-730, ДО конструирования `MemoryConductor`):

```python
        def _semantic_idle_sec() -> float:
            searcher = self._semantic_searcher
            if searcher is None or not searcher.model_loaded:
                raise RuntimeError("semantic searcher model not loaded")
            return max(0.0, time.monotonic() - float(searcher._last_used_ts))

        def _semantic_unload_if_idle(threshold: float) -> bool:
            searcher = self._semantic_searcher
            return bool(searcher is not None and searcher.unload_if_idle(threshold))
```

и в конструктор `MemoryConductor(...)` добавить `semantic_unload_if_idle=_semantic_unload_if_idle, semantic_idle_sec_fn=_semantic_idle_sec,`. ⚠️ `self._semantic_searcher` создаётся ПОЗЖЕ (`:1394`) — проверить порядок в `__init__`: если замыкание читает атрибут при вызове (а не при создании) — порядок не важен; проверь и запиши в отчёт.

- [ ] **Step 4: Конфиг** — `core/config.py` рядом с semantic-ключами (~:1128):
  `"semantic_search_idle_unload_sec": 1800.0,  # F5/D5: idle-выгрузка модели семантики; 0 = выключено` + `settings_validator.py` рядом с conductor-порогами (`:105-114`): `"semantic_search_idle_unload_sec": (0.0, 86400.0, 1800.0, float),`.
- [ ] **Step 5: GREEN** — команда Task 1 Step 3. Ожидаемо: все зелёные.
- [ ] **Step 6: Регрессия** — `test_semantic_search.py`, `test_semantic_search_wave31.py`, `test_memory_conductor_2026_08_19.py`, `test_memory_conductor_wiring_2026_08_19.py`, `test_conductor_shadow_since_persists_2026_09_04.py` (+ `rg -ln 'MemoryConductor\(' KrabEar/tests/` — все файлы). Ожидаемо: зелёные без правок; любая правка старых тестов — стоп координатору.
- [ ] **Step 7: Гейт** — flake8 (max-120) изменённых; `scripts/pre_merge_py312_check.sh` на новый тест; `make audit-all`.

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `feat/f5-lazy-unload`
- [ ] `git add` **явными путями** (6 файлов Task 2 + новый тест)
- [ ] Коммит: `feat(search): ленивая выгрузка модели семантики в простое (F5/D5)`
- [ ] PR в `codex/krab-ear-v2`; НЕ мержить (гейт координатора).

## Definition of Done

- Тесты searcher + кондуктор RED→GREEN; регрессия зелёная; audit-all зелёный.
- `_RESIDENTS`/`enforce_for`/существующие шаги/brain-политика не тронуты (проверить диффом); новых IPC нет; `semantic_search_enabled` остаётся False.
- `unload_model` не трогает индекс и не выставляет `_model_error`; `unload_if_idle` при `threshold<=0` — не выгружает.
- В отчёт: порядок `_semantic_searcher` vs кондуктор-конструктор; классификация RED по каждому тесту.

## Вне scope (записать в отчёт, не чинить)

- Включение `semantic_search_enabled` / построение индекса (~2-3 мин) — владелец отдельно.
- Смена модели/флага на лету (снапшот на старте) — существующий дизайн, не трогать.
- Watchdog-выгрузка при давлении (mlx.oom и т.п.) — не делать: семантика не GPU-резидент.
