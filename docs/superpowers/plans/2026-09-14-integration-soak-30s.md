# R2 Integration Soak 30s Implementation Plan

**Goal:** `test_integration_1000_cycles` проходит локально под нагрузкой,
не упираясь в W957-гард 30 с (`KrabEar/pytest.ini`), без ослабления гарда
и без потери покрытия soak.

**Architecture:** Корень — НЕ history-сканы и НЕ лики превью-потока (оба
проверены и чистые: wave-1770 lifecycle, scan-терм ~7 мс). Корень —
per-cycle `unload_model_async` на КАЖДЫЙ `start_recording`
(`llm_brain_unload_on_recording`, default True): поток + REST-unload, при
отказе — `lms unload` CLI-субпроцесс. При живом Studio (dev-машина) это
1000 HTTP+CLI + lingering retry-треды (+7 живых за 120 циклов): замер
15.5→28.4 мс/цикл с ростом против 5–7 мс flat без unload. Итог ~37 с wall
при 192 с CPU (631%) против гарда 30 с → Timeout-death; после убийства
тест-цикл продолжает крутиться в фоне (thread-метод не прерывает), и
`close()` виснет на join — suite stalls. Бонус: soak 1000× выгружает живой
brain владельца. CI зелёный, потому что там Studio offline (fast-refuse).
Фикс — conftest autouse-fixture по образцу волн 1705/1748 (никаких
исключений по nodeid не нужно — см. ниже). W957-гвард, маркеры и число
циклов не трогаем.

**Tech Stack:** `unittest.mock.patch` строкой
`"backend.lm_studio_lifecycle.unload_model_async"` (RecordingCore импортирует
имя внутри функции при каждом вызове — перехват работает; замерено).
Исключения НЕ нужны: тесты реального unload биндят свои референсы
(`test_lm_studio_lifecycle.py` импортирует имена напрямую;
`test_oom_action_real_unload*` / `test_error_actions.py` патчат
`backend.error_actions.unload_model_async`) — патч атрибута lifecycle-модуля
для них невидим. Регрессия реального пути = эти три файла остаются зелёными.

**База:** `origin/codex/krab-ear-v2`. Ветка `fix/soak-30s-20260914`
(параллельных сессий нет).

**Баны:** вставь список из `docs/EXECUTOR_PLAYBOOK.md` §1. Плюс: не трогать
`timeout = 30` в `KrabEar/pytest.ini` (W957 defense-in-depth); не менять
число циклов; не включать `history_encryption_enabled`; мерж в колею —
отдельное решение (CI-гейт), этот план заканчивается коммитом в ветку;
`git add` явными путями.

---

### Task 1: neuter unload в тестах (conftest fixture)

**Files:**
- Modify: `KrabEar/tests/conftest.py` (новый autouse-fixture после
  `_suppress_startup_diagnostics`, ~строка 431)
- Modify: `KrabEar/tests/test_backend_service.py` (1 строка комментария
  к W1748-блоку: актуальный замер после R2)
- Test: существующий soak
  `BackendServiceTestCase::test_integration_1000_cycles` — он и есть
  RED/GREEN (Timeout-death 33 с при load ~21-27 зафиксирована 14.09).
  Отдельный micro-тест не пишем сознательно: llave-наблюдение
  (вызовы lifecycle-функции) ломается порядком патчей (тест перепишет
  no-op фикстуры своим счётчиком и насчитает вызовы всегда); soak
  чувствителен напрямую (превышение 30 с = RED), а реальный путь
  сторожат три lifecycle-файла ниже.

- [ ] **Step 1: RED уже зафиксирован — перепроверка не нужна**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest "KrabEar/tests/test_backend_service.py::BackendServiceTestCase::test_integration_1000_cycles" -p no:warnings -q`
Expected (до фикса): `+++ Timeout +++` ~30-33 с, FAIL. Повторять прогон
не обязательно — evidence выше; сразу к фиксу.

- [ ] **Step 2: Write the fixture**

```python
# ---------------------------------------------------------------------------
# R2 (2026-09-14): neuter per-cycle LM Studio brain-unload in tests.
#
# RecordingCore fires unload_model_async() on EVERY start_recording
# (llm_brain_unload_on_recording, default True): thread + REST-unload,
# on refusal `lms unload` CLI subprocess. With live Studio (dev box) the
# 1000-cycle soak burns ~37 s wall / 192 s CPU (631%) vs the W957 30 s
# guard (measured 15.5→28.4 ms/cycle growing vs 5-7 ms flat neutered),
# and unloads the owner's live brain 1000x as a side effect. CI stays
# green only because Studio is offline there (fast-refuse).
#
# No nodeid exclusions (unlike the warmup guards above): every test that
# asserts real unload behaviour binds its own reference
# (test_lm_studio_lifecycle imports the names directly;
# test_oom_action_real_unload* / test_error_actions patch
# backend.error_actions.unload_model_async), so patching the lifecycle
# module attribute is invisible to them. RecordingCore imports the name
# inside the function per call, so the patch intercepts the soak path.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _disable_studio_unload_in_tests():
    try:
        from unittest.mock import patch

        with patch(
            "backend.lm_studio_lifecycle.unload_model_async",
            lambda *a, **k: None,
        ):
            yield
    except Exception:
        yield
```

Плюс 1 строка в W1748-комментарий soak-теста (после
`# In solo runs (no xdist) the full 1000 cycles run as before.`):
`# R2 (2026-09-14): conftest neuters per-cycle LM Studio unload — ~7 s loaded.`

- [ ] **Step 3: Run soak to verify it passes**

Run: та же команда pytest
Expected: `1 passed` за ~7-15 с wall (margin 2-4× до гарда 30 с).

- [ ] **Step 4: Regression — реальный unload-путь цел**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_lm_studio_lifecycle.py KrabEar/tests/test_oom_action_real_unload_2026_08_19.py KrabEar/tests/test_error_actions.py -p no:warnings -q`
Expected: все PASS (фикстура их не касается — свои биндинги).

- [ ] **Step 5: Gate + commit (без мержа в колею)**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_backend_service.py KrabEar/tests/conftest.py
git add KrabEar/tests/conftest.py KrabEar/tests/test_backend_service.py docs/superpowers/plans/2026-09-14-integration-soak-30s.md
git commit -m "fix(tests): neuter per-cycle LM Studio unload в тестах (R2)"
```

Дополнительно (дешёвый пояс): полный файл `test_backend_service.py`
целиком один раз — убедиться, что фикстура не красит соседей.
Мерж в `origin/codex/krab-ear-v2` — отдельным решением после CI.
