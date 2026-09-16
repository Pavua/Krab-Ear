# W1 Wake-word mic-hold при выключенной фиче Implementation Plan

**Goal:** при `wake_word_enabled=False` микрофон wake word не открывается
и не удерживается ни одним путём (IPC, restore после reinit, watchdog).

**Architecture:** F5 (`handle_wake_word_start`) покрывает только IPC-вход —
классическая sibling-gate asymmetry. Три bypass: прямые `adapter.start()`
(`AudioReinitCoordinator._restore_listener`), отсутствие остановки живого
слушателя при выключении тумблера, воскрешение мёртвого watchdog'ом.
Фикс тремя точками без новой межмодульной проводки и без смены дефолтов
(fail-open default True как в F5 — отсутствие ключа = «агент ещё не
синхронизировал», иначе сломаем работающих пользователей):
1. `OpenWakeWordAdapter.start()` — source-gate с новым исключением
   `WakeWordDisabledError` (первым в `with self._lock`).
2. `_restore_listener` — ловит `WakeWordDisabledError` → `True`
   («восстанавливать нечего», симметрично epoch-guard).
3. `WakeWordWatchdog.check_once` — при выключенной фиче: живой → `stop()`
   + `"stopped_disabled"`, мёртвый → `None` (не воскрешать, не
   эскалировать). Не лезем в слушатель во время активной записи
   (return выше) — resume-логика диктовки не затрагивается.
   Глитч чтения настроек = assume-enabled (fail-open как `_enabled()`:
   убить работающий слушатель дороже).

**Tech Stack:** то, что в репо. Тесты — расширение
`KrabEar/tests/test_wake_word_enabled_gate.py` (те же фикстуры
`_make_adapter`), фейк-слушатель + фейк-часы для watchdog
(конструктор принимает `clock=`).

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/w1-wake-mic-hold`,
ветка `fix/wake-mic-hold-20260916` (откат = удалить ветку/worktree).

**Баны:** вставь список из `docs/EXECUTOR_PLAYBOOK.md` §1. Плюс: дефолты
не менять; новая проводка settings→модули запрещена (только существующие
`_settings_get`); `git add` явными путями; мерж в колею после зелени
(юнит + py312 + repo-flake8); деплой (рестарт) — отдельным решением
с окном и busy-check.

---

### Task 1: source-gate + restore-gate

**Files:**
- Modify: `KrabEar/backend/openwakeword_adapter.py` (класс-исключение
  на уровне модуля + гейт первым в `start()` под `with self._lock`)
- Modify: `KrabEar/backend/audio_reinit.py` (import исключения +
  `except WakeWordDisabledError → True` в `_restore_listener`)
- Test: `KrabEar/tests/test_wake_word_enabled_gate.py`

- [ ] **Step 1: Write the failing tests**

```python
from backend.openwakeword_adapter import (
    OpenWakeWordAdapter,
    WakeWordDisabledError,
)
from backend.audio_reinit import AudioReinitCoordinator


def _make_coordinator(adapter):
    return AudioReinitCoordinator(
        reinit_audio_backend=lambda: None,
        is_recording=lambda: False,
        wake_word_adapter=adapter,
    )


class TestWakeWordStartGate(unittest.TestCase):
    """Прямые вызовы start() подчиняются тому же гейту, что IPC (F5b)."""

    def test_start_raises_when_disabled(self) -> None:
        adapter = _make_adapter(self._tmp, settings={"wake_word_enabled": False})
        with self.assertRaises(WakeWordDisabledError):
            adapter.start("hey_jarvis", lambda *a: None)

    def test_start_gate_missing_key_defaults_to_allowed(self) -> None:
        """Нет ключа — не DisabledError (дальше штатный отказ движка)."""
        adapter = _make_adapter(self._tmp, settings={})
        with self.assertRaises(RuntimeError) as ctx:
            adapter.start("hey_jarvis", lambda *a: None)
        self.assertNotIsInstance(ctx.exception, WakeWordDisabledError)

    def test_restore_skips_when_disabled(self) -> None:
        """Restore при выключенной фиче = нечего восстанавливать (True)."""
        adapter = _make_adapter(self._tmp, settings={"wake_word_enabled": False})
        coordinator = _make_coordinator(adapter)
        self.assertTrue(
            coordinator._restore_listener(adapter, True, "hey_jarvis", None, None)
        )
        self.assertFalse(adapter.is_running())
```

(setUp с `self._tmp = tempfile.mkdtemp()` — как в файле; `import tempfile`
уже есть.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=<worktree>/KrabEar <venv>/bin/python -m pytest KrabEar/tests/test_wake_word_enabled_gate.py -v` (cwd = worktree)
Expected: 3 новых FAIL — `start` не кидает DisabledError (падает на
oww-RuntimeError); restore возвращает False; старые 4 PASS.

- [ ] **Step 3: Write minimal implementation**

```python
# openwakeword_adapter.py, уровень модуля:
class WakeWordDisabledError(RuntimeError):
    """Фича wake word выключена владельцем (wake_word_enabled=False)."""
```

```python
# openwakeword_adapter.py, start(), первым в `with self._lock:`:
            # F5b (2026-09-16, sibling asymmetry F5): IPC-гейт
            # handle_wake_word_start — не единственный вход. Прямые вызовы
            # start() обходили проверку и открывали микрофон при False.
            # Тот же fail-open default True, что в F5.
            if self._settings_get("wake_word_enabled", True) is False:
                logger.info(
                    "OpenWakeWordAdapter.start: отклонён — wake_word_enabled=False"
                )
                raise WakeWordDisabledError("wake word disabled in settings")
```

```python
# audio_reinit.py, _restore_listener, новый except ПЕРЕД generic:
        try:
            adapter.start(...)
        except WakeWordDisabledError:
            logger.info(
                "AudioReinitCoordinator: restore пропущен — "
                "wake_word_enabled=False (восстанавливать нечего)"
            )
            return True
        except Exception:
            ... (существующий)
```

```python
# audio_reinit.py, top-level import (цикла нет: адаптер не импортирует координатор):
from backend.openwakeword_adapter import WakeWordDisabledError
```

- [ ] **Step 4: Run tests to verify they pass**

Run: та же команда. Expected: 7 PASS.

### Task 2: watchdog enforcement

**Files:**
- Modify: `KrabEar/backend/wake_word_watchdog.py` (хелпер
  `_wake_feature_enabled` + гейт в `check_once` после recording-блока)
- Test: тот же файл

- [ ] **Step 1: Write the failing tests**

```python
class _FakeListener:
    def __init__(self, running=True, model="hey_jarvis"):
        self._running = running
        self._model = model
        self.stopped = 0

    def is_running(self):
        return self._running

    def active_model(self):
        return self._model

    def heartbeat(self):
        return {}

    def set_wedged(self, value):
        pass

    def stop(self, timeout=3.0):
        self.stopped += 1
        self._running = False
        self._model = None
        return True


def _make_watchdog(listener, settings, clock=None):
    from unittest.mock import MagicMock
    from backend.wake_word_watchdog import WakeWordWatchdog
    kw = dict(adapter=listener, reinit_coordinator=MagicMock(),
              settings_get=lambda k, d: settings.get(k, d))
    if clock is not None:
        kw["clock"] = clock
    return WakeWordWatchdog(**kw)


class TestWatchdogDisabledFeature(unittest.TestCase):
    def test_stops_live_listener_when_disabled(self):
        wd = _make_watchdog(_FakeListener(running=True),
                            {"wake_word_enabled": False})
        self.assertEqual(wd.check_once(), "stopped_disabled")
        self.assertFalse(wd._adapter.is_running())
        self.assertEqual(wd._adapter.stopped, 1)

    def test_no_resurrect_when_disabled(self):
        now = [1000.0]
        wd = _make_watchdog(_FakeListener(running=False),
                            {"wake_word_enabled": False},
                            clock=lambda: now[0])
        self.assertIsNone(wd.check_once())
        now[0] += 10000.0
        self.assertIsNone(wd.check_once())
        self.assertFalse(wd._escalated_this_episode)

    def test_ignores_healthy_when_enabled(self):
        listener = _FakeListener(running=True)
        wd = _make_watchdog(listener, {})
        self.assertIsNone(wd.check_once())
        self.assertEqual(listener.stopped, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Expected: 3 FAIL — `stopped_disabled` vs None; эскалация вместо None;
(guard-тест PASS и до, и после).

- [ ] **Step 3: Write minimal implementation**

```python
    def _wake_feature_enabled(self) -> bool:
        """Включена ли сама фича wake word (не путать с _enabled())."""
        try:
            return self._settings_get("wake_word_enabled", True) is not False
        except Exception:
            logger.exception("WakeWordWatchdog: чтение wake_word_enabled упало")
            return True
```

```python
        # в check_once(), сразу после recording_active-блока (до опроса сессии):
        if not self._wake_feature_enabled():
            # Фича выключена владельцем: живой слушатель = mic-hold баг,
            # мёртвый не воскрешаем и не эскалируем. Сюда не доходим во
            # время активной записи (return выше).
            try:
                if bool(self._adapter.is_running()):
                    self._adapter.stop()
                    logger.info(
                        "WakeWordWatchdog: слушатель остановлен — "
                        "wake_word_enabled=False"
                    )
                    return "stopped_disabled"
            except Exception:
                logger.exception(
                    "WakeWordWatchdog: остановка при выключенной фиче упала"
                )
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Expected: все новые PASS. Плюс регрессия соседей:
`test_openwakeword_adapter.py`, `test_wake_word_polling_contract.py`,
`test_wake_word_blocked_start_observability_2026_08_18.py` — все PASS.

- [ ] **Step 5: Gate + commit (без мержа в колею)**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_wake_word_enabled_gate.py
<repo-flake8 на 3 изменённых .py>
git add KrabEar/backend/openwakeword_adapter.py KrabEar/backend/audio_reinit.py KrabEar/backend/wake_word_watchdog.py KrabEar/tests/test_wake_word_enabled_gate.py docs/superpowers/plans/2026-09-16-wake-mic-hold.md
git commit -m "fix(wake): mic-hold при выключенной фиче — гейты start/restore/watchdog (W1)"
```

Мерж в колею + push — после зелени и отдельной отмашки (ветка/worktree
сохраняются для отката).
