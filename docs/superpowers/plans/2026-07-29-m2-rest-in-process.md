# M2 — in-process REST за рубильником: план исполнения

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend-процесс умеет поднимать REST-сервер внутри себя (один процесс вместо двух) за рубильником `rest_in_process_enabled`, дефолт — выключено.

**Architecture:** M1 уже сделал `create_app(deps)` настоящей фабрикой, кладущей зависимости в `app.config["REST_DEPS"]`. M2 добавляет три вещи: (1) module-level синглтоны `rest_server.py` уходят за guard-функцию, чтобы импорт модуля из `service.py` не создавал второй `AudioEngine`; (2) новый модуль `backend/rest_inprocess.py` поднимает `make_server` в daemon-треде; (3) `BackendService` собирает `StaticDeps` из своих коллабораторов и стартует сервер, а `EventBridge` при этом не включается — в слитом процессе шина одна.

**Tech Stack:** Python 3.12 (ubuntu-parity) / 3.14 (dev-venv), Flask, `werkzeug.serving.make_server`, unittest, pytest.

## Global Constraints

- Спека: `docs/superpowers/specs/2026-07-16-m-series-rest-merge-design.md` §4. Расхождения со спекой отмечены в задачах явно — следовать плану, не спеке.
- Рубильник читается **один раз при старте** backend-процесса. НЕ live-toggle через `set_settings`.
- Дефолт `rest_in_process_enabled = False`. Волна не меняет поведение прода до отдельного решения владельца.
- Standalone-режим `rest_server.py` НЕ удаляется и НЕ меняет поведение: 27 тест-файлов / ≈752 теста категорий A/B/C обязаны остаться зелёными **без правок**.
- Ни один эндпойнт, auth, CORS, rate-limit, схема ответа не меняются.
- Каждый тест, создающий `BackendService(...)`, ОБЯЗАН звать `service.close()` в `tearDown` (иначе daemon-треды роняют весь чанк — `feedback_backendservice_teardown_ci.md`).
- Перед мержем: `make pre-merge-check` (ubuntu-parity py3.12 без mlx), `make audit-all`, flake8 CI-флагами, `swift build -c release` не требуется (Swift не затрагивается).
- Коммиты — по задаче, с трейлером `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## Карта файлов

| Файл | Ответственность | Задача |
|---|---|---|
| `KrabEar/core/config.py` | Pydantic-поле `REST_IN_PROCESS_ENABLED` + ключ `rest_in_process_enabled` в `DEFAULT_SETTINGS` | 1 |
| `KrabEar/backend/error_codes.py` | код `rest.port_conflict` в `ERROR_REGISTRY` | 2 |
| `KrabEar/backend/rest_server.py` | `_ensure_standalone_singletons()` — ленивое создание standalone-комплекта | 3 |
| `KrabEar/backend/rest_inprocess.py` | **НОВЫЙ**: `InProcessRestServer` — make_server в daemon-треде, EADDRINUSE, shutdown, статус | 4 |
| `KrabEar/backend/event_bridge.py` | мост не включается при in-process (одна строка в `__init__`) | 5 |
| `KrabEar/backend/service.py` | сборка `StaticDeps`, старт/останов сервера | 6 |
| `KrabEar/backend/health_check_service.py` | секция `get_diagnostics.rest_in_process` | 7 |
| `scripts/rest_inprocess_load_smoke.py` | **НОВЫЙ**: нагрузочный смок против живого in-process REST | 8 |

Порядок задач: 1 → 2 → 3 (рискованная, гейт полного пласта) → 4 → 5 → 6 → 7 → 8.

---

### Task 1: Рубильник `rest_in_process_enabled`

**Files:**
- Modify: `KrabEar/core/config.py` (два места: Pydantic-класс рядом с `EVENT_BRIDGE_ENABLED` ~строка 93; `DEFAULT_SETTINGS`-часть рядом с `DISK_MONITOR_ENABLED` ~строка 662)
- Test: `KrabEar/tests/test_rest_in_process_setting_M2.py` (создать)

**Interfaces:**
- Consumes: ничего.
- Produces: `settings.REST_IN_PROCESS_ENABLED: bool` (дефолт `False`, env `KRAB_EAR_REST_IN_PROCESS_ENABLED`); ключ `rest_in_process_enabled` в `DEFAULT_SETTINGS` со значением `False`. Обоими пользуются задачи 4, 5, 6, 7.

⚠️ **Расхождение со спекой, читать внимательно.** Спека §4.1 ссылается на `event_bridge_enabled` как на образец «`DEFAULT_SETTINGS` + Pydantic». Живая проверка 2026-07-29: ключа `event_bridge_enabled` в нижнем регистре **не существует** — у моста только Pydantic-поле `EVENT_BRIDGE_ENABLED`. Для M2 делаем **оба** места, как и требует спека, но читать в коде будем **только** `settings.REST_IN_PROCESS_ENABLED` (Pydantic). Ключ в `DEFAULT_SETTINGS` нужен, чтобы настройка была видима в GUI/`get_settings` и не выглядела «скрытым env-флагом».

- [ ] **Step 1: Написать падающий тест**

Создать `KrabEar/tests/test_rest_in_process_setting_M2.py`:

```python
"""M2: рубильник in-process REST присутствует в обоих источниках правды."""
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.config import DEFAULT_SETTINGS, Settings  # noqa: E402


class RestInProcessSettingTest(unittest.TestCase):
    def test_pydantic_field_defaults_to_false(self):
        s = Settings()
        self.assertIs(s.REST_IN_PROCESS_ENABLED, False)

    def test_env_override_turns_it_on(self):
        old = os.environ.get("KRAB_EAR_REST_IN_PROCESS_ENABLED")
        os.environ["KRAB_EAR_REST_IN_PROCESS_ENABLED"] = "true"
        try:
            self.assertIs(Settings().REST_IN_PROCESS_ENABLED, True)
        finally:
            if old is None:
                os.environ.pop("KRAB_EAR_REST_IN_PROCESS_ENABLED", None)
            else:
                os.environ["KRAB_EAR_REST_IN_PROCESS_ENABLED"] = old

    def test_default_settings_key_present_and_false(self):
        self.assertIn("rest_in_process_enabled", DEFAULT_SETTINGS)
        self.assertIs(DEFAULT_SETTINGS["rest_in_process_enabled"], False)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тест падает по правильной причине**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_in_process_setting_M2.py -v
```

Ожидание: три FAIL. Первые два — `AttributeError: 'Settings' object has no attribute 'REST_IN_PROCESS_ENABLED'`, третий — `AssertionError: 'rest_in_process_enabled' not found in ...`. Если падение по другой причине (ImportError, ошибка пути) — чинить харнесс, а не бежать дальше.

- [ ] **Step 3: Добавить Pydantic-поле**

В `KrabEar/core/config.py`, сразу после блока `EVENT_BRIDGE_ENABLED` (перед `model_config = SettingsConfigDict(...)`):

```python
    # --- In-process REST (backend/rest_inprocess.py, spec 2026-07-16 §4) ------
    # True = backend-процесс поднимает REST-сервер ВНУТРИ себя (один процесс
    # вместо двух), при этом EventBridge не включается — шина общая.
    # Killswitch, читается ОДИН РАЗ при старте (как EVENT_BRIDGE_ENABLED) —
    # НЕ live-toggle через set_settings. Дефолт False: пока прод на двух
    # процессах, включение — отдельное решение владельца (канарейка §4.5).
    REST_IN_PROCESS_ENABLED: bool = False
```

- [ ] **Step 4: Добавить ключ в DEFAULT_SETTINGS**

В `KrabEar/core/config.py`, в блоке `DEFAULT_SETTINGS` сразу после `AUTO_CLEANUP_AFTER_DAYS`:

```python
    # --- In-process REST (см. REST_IN_PROCESS_ENABLED выше) ---
    # Видимость настройки в GUI/get_settings. Источник правды при СТАРТЕ —
    # Pydantic-поле; этот ключ ничего не включает сам по себе.
    "rest_in_process_enabled": False,
```

⚠️ Проверить фактический синтаксис блока: если `DEFAULT_SETTINGS` — это класс с аннотациями (как выглядит участок 655–670), а не dict-литерал, добавлять строкой `REST_IN_PROCESS_ENABLED: bool = False` в том же стиле, что соседний `DISK_MONITOR_ENABLED`, и подправить тест под фактический способ доступа. Форму определяет код, а не этот план.

- [ ] **Step 5: Убедиться, что тест проходит**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_in_process_setting_M2.py -v
```

Ожидание: 3 passed.

- [ ] **Step 6: Коммит**

```bash
git add KrabEar/core/config.py KrabEar/tests/test_rest_in_process_setting_M2.py
git commit -m "feat(config): M2 Task 1 — рубильник rest_in_process_enabled (default off)"
```

---

### Task 2: Код ошибки `rest.port_conflict`

**Files:**
- Modify: `KrabEar/backend/error_codes.py` (`ERROR_REGISTRY`)
- Test: `KrabEar/tests/test_rest_port_conflict_code_M2.py` (создать)

**Interfaces:**
- Consumes: ничего.
- Produces: код `"rest.port_conflict"` в `ERROR_REGISTRY`. Использует задача 4 при EADDRINUSE.

- [ ] **Step 1: Написать падающий тест**

```python
"""M2: код rest.port_conflict зарегистрирован (EADDRINUSE при in-process старте)."""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.error_codes import ERROR_REGISTRY  # noqa: E402


class RestPortConflictCodeTest(unittest.TestCase):
    def test_code_registered(self):
        self.assertIn("rest.port_conflict", ERROR_REGISTRY)

    def test_entry_has_required_fields(self):
        entry = ERROR_REGISTRY["rest.port_conflict"]
        self.assertIn("user_msg_ru", entry)
        self.assertTrue(entry["user_msg_ru"].strip())
        self.assertIn("actionable", entry)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Прогнать — ожидается FAIL**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_port_conflict_code_M2.py -v
```

Ожидание: `AssertionError: 'rest.port_conflict' not found in ERROR_REGISTRY`.

- [ ] **Step 3: Зарегистрировать код**

В `KrabEar/backend/error_codes.py`, в `ERROR_REGISTRY`, рядом с прочими инфраструктурными кодами:

```python
    # rest.port_conflict — in-process REST не смог занять порт (M2).
    # Реальный сценарий: легаси launchd-агент ai.krab.ear.rest ещё живёт и
    # держит 5005, а backend уже стартует с rest_in_process_enabled=true.
    # Fail-open: backend продолжает работать БЕЗ in-process REST (диктовка
    # важнее), поэтому это не critical и не крутит crash-loop — в отличие от
    # standalone-пути, где EADDRINUSE завершает процесс через sys.exit(1).
    "rest.port_conflict": {
        "user_msg_ru": (
            "Порт REST уже занят — встроенный веб-сервер не запущен. "
            "Основные функции работают. Обычно причина в том, что старый "
            "отдельный REST-агент ещё не выгружен."
        ),
        "actionable": False,
        "severity": "warn",
        "category": "system",
    },
```

⚠️ Ключи `severity`/`category` скопировать из фактической соседней записи (`audio.wakeword_wedged`, ~строка 826) — если у соседей другой набор полей, повторить их набор, а не этот. Формат реестра определяет код.

- [ ] **Step 4: Прогнать — ожидается PASS**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_port_conflict_code_M2.py -v
```

Дополнительно прогнать существующий страж реестра (если он есть):

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "error_registry or error_codes" -q
```

Ожидание: всё зелёное. Если тест считает количество кодов — обновить ожидаемое число в нём (это легитимная правка существующего теста, единственная в волне).

- [ ] **Step 5: Коммит**

```bash
git add KrabEar/backend/error_codes.py KrabEar/tests/test_rest_port_conflict_code_M2.py
git commit -m "feat(errors): M2 Task 2 — код rest.port_conflict для EADDRINUSE in-process REST"
```

---

### Task 3: Ленивая инициализация standalone-синглтонов — ❌ ОТКАЧЕНА 2026-07-30

**Не исполнять.** Задача выполнена, прогнана и откачена: сработало собственное правило остановки (ниже, Step 8). Гейт с базовой линией дал 0 упавших файлов до правки и **20 после** — все регрессии.

Причина не в реализации, а в самой идее. Ленивость принципиально несовместима с обоими способами, которыми тесты патчат этот модуль:

- категория A (`test_rest_server_unit.py:79-82`) патчит **конструкторы вокруг импорта** — при отложенном создании объекты рождаются позже, когда патч уже снят, и получаются настоящими;
- категория B (там же, 142-146) делает `patch.object(_rest_mod, "store", …)` — атрибута ещё нет в пространстве имён модуля, `setUp` падает с `AttributeError`.

Починка требовала бы переписать 20 тест-файлов, что прямо запрещено не-целями серии M. Спека §4.2 этот сценарий предвидела; владелец выбрал замену — **Task 3B** (бриф: `.superpowers/sdd/task-3b-brief.md`): объекты по-прежнему создаются при импорте, а владелец процесса подменяет module-глобалы своими через новую функцию `adopt_external_singletons(*, engine, store, transcriber, translator, tts_service)`. Дубль тяжёлого состояния устраняется сборщиком мусора, патчабельность цела.

Артефакты откаченной попытки сохранены в скретчпаде (`task3_lazy_attempt.patch`, `task3_lazy_test.py.bak`).

<details>
<summary>Исходный текст задачи (для истории)</summary>

### Task 3 (архив): Ленивая инициализация standalone-синглтонов

**Files:**
- Modify: `KrabEar/backend/rest_server.py` (строки ~642–646 и `_ModuleGlobalsDeps.__getattr__` ~строка 87)
- Test: `KrabEar/tests/test_rest_lazy_singletons_M2.py` (создать)

**Interfaces:**
- Consumes: ничего.
- Produces: `rest_server._ensure_standalone_singletons() -> None` — идемпотентно создаёт `engine`/`store`/`transcriber`/`translator`/`tts_service` в `globals()` модуля. После этой задачи `import backend.rest_server` больше не конструирует `AudioEngine`. Нужна задаче 6 (импорт из `service.py`).

🔴 **Это самая рискованная задача волны.** Спека §4.2 прямо называет её единственным местом, где M2 может задеть старые тесты: категория A (6 файлов) патчит конструкторы **вокруг импорта**, а после правки конструирование переезжает на **первое обращение**. Гейт — прогон всего пласта (27 файлов). Если сломанных файлов **больше пяти** — НЕ чинить их по одному, а откатить задачу и применить fallback из §4.2 (оставить eager-импорт, передавать в `StaticDeps` уже созданные standalone-синглтоны). Решение о fallback принимает координатор, не исполнитель: остановиться и доложить.

- [ ] **Step 1: Зафиксировать базовую линию ДО правки**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "rest" -q 2>&1 | tail -5 > /tmp/m2_rest_baseline.txt
cat /tmp/m2_rest_baseline.txt
```

Записать число passed/failed в отчёт. Без базовой линии невозможно отличить «сломал я» от «было красным».

- [ ] **Step 2: Написать падающий тест**

Создать `KrabEar/tests/test_rest_lazy_singletons_M2.py`:

```python
"""M2: импорт backend.rest_server не конструирует тяжёлые синглтоны.

Корень: service.py будет импортировать rest_server ради create_app(). При
eager-инициализации это создало бы ВТОРОЙ AudioEngine в том же процессе.
"""
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RestLazySingletonsTest(unittest.TestCase):
    def test_import_does_not_build_audio_engine(self):
        # Отдельный процесс: в текущем rest_server мог быть импортирован ранее
        # другим файлом чанка, и тогда проверка ничего не покажет.
        code = (
            "import sys;"
            "import backend.rest_server as rs;"
            "print('ENGINE_BUILT' if 'engine' in vars(rs) else 'LAZY')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("LAZY", proc.stdout)

    def test_first_attribute_access_builds_them(self):
        code = (
            "import backend.rest_server as rs;"
            "_ = rs._MODULE_DEPS.store;"
            "print('BUILT' if 'store' in vars(rs) else 'STILL_LAZY')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("BUILT", proc.stdout)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Прогнать — ожидается FAIL первого теста**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_lazy_singletons_M2.py -v
```

Ожидание: `test_import_does_not_build_audio_engine` падает (`ENGINE_BUILT`), второй проходит уже сейчас.

- [ ] **Step 4: Найти ВСЕ module-level обращения к синглтонам**

```bash
grep -n "^_propagate_hf_token_to_env()\|^engine\.\|^store\.\|^transcriber\.\|^translator\.\|^tts_service\." KrabEar/backend/rest_server.py
```

Любой module-level **вызов**, трогающий эти объекты (например `_propagate_hf_token_to_env()`), обязан переехать внутрь `_ensure_standalone_singletons()` **после** создания объектов — иначе импорт снова станет тяжёлым или упадёт с `NameError`. Выписать найденное в отчёт.

- [ ] **Step 5: Заменить eager-блок на guard-функцию**

В `KrabEar/backend/rest_server.py` заменить строки 642–646:

```python
engine = AudioEngine(skip_gigaam_warmup=True)
store = StateStore(settings.DATA_DIR)
transcriber = Transcriber(engine=engine)
translator = Translator()
tts_service = TTSService()
```

на:

```python
# M2: standalone-комплект создаётся ЛЕНИВО. Причина — service.py импортирует
# этот модуль ради create_app(); при eager-инициализации импорт создал бы
# ВТОРОЙ AudioEngine/StateStore в том же процессе (спека §4.2).
#
# Патчабельность категорий A/B/C сохранена: объекты по-прежнему появляются
# как module-атрибуты (globals()), просто позже — при ПЕРВОМ обращении, то
# есть внутри патч-контекстов тестов.
_STANDALONE_LOCK = _threading.Lock()


def _ensure_standalone_singletons() -> None:
    """Идемпотентно создаёт standalone-комплект в globals() этого модуля.

    Двойная проверка под локом: два WSGI-треда могут одновременно обработать
    первый запрос, а AudioEngine дорогой и не терпит двойной постройки
    (сиблинг lazy-load-паттерна STT-адаптеров, CLAUDE.md).
    """
    g = globals()
    if "engine" in g:
        return
    with _STANDALONE_LOCK:
        if "engine" in g:
            return
        g["engine"] = AudioEngine(skip_gigaam_warmup=True)
        g["store"] = StateStore(settings.DATA_DIR)
        g["transcriber"] = Transcriber(engine=g["engine"])
        g["translator"] = Translator()
        g["tts_service"] = TTSService()
        # ВАЖНО: module-level вызовы, зависевшие от этих объектов (см. Step 4),
        # переезжают СЮДА — строго после создания.
        _propagate_hf_token_to_env()
```

Добавить в шапку импортов модуля (если `threading` ещё не импортирован под этим алиасом):

```python
import threading as _threading
```

⚠️ Если `_propagate_hf_token_to_env` определена **ниже** по файлу, чем guard-функция, — это нормально (вызов происходит в рантайме, не на импорте). Если она вызывалась на module-level — убрать тот вызов.

- [ ] **Step 6: Подключить guard к ленивому чтению**

В `_ModuleGlobalsDeps.__getattr__` (~строка 87) добавить вызов перед чтением:

```python
    def __getattr__(self, name):
        # M2: standalone-комплект строится при первом обращении (спека §4.2).
        # Дёргаем guard только для имён комплекта — прочие module-атрибуты
        # (например sse_stream) существуют с импорта и не должны тянуть
        # постройку AudioEngine.
        if name in _STANDALONE_NAMES:
            _ensure_standalone_singletons()
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None
```

И рядом с классом:

```python
# Имена, которые создаёт _ensure_standalone_singletons(). Держать в синхроне
# с телом guard-функции: лишнее имя здесь = ленивая постройка на пустом месте,
# недостающее = AttributeError на первом обращении.
_STANDALONE_NAMES = frozenset(
    {"engine", "store", "transcriber", "translator", "tts_service"}
)
```

- [ ] **Step 7: Прогнать новый тест — ожидается PASS**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_lazy_singletons_M2.py -v
```

Ожидание: 2 passed.

- [ ] **Step 8: 🔴 ГЕЙТ — прогнать весь rest-пласт и сравнить с базовой линией**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "rest" -q 2>&1 | tail -5
```

Сравнить с `/tmp/m2_rest_baseline.txt`. Затем — контракт-тест прокси из M1 (страж категории B) и ubuntu-parity по затронутому файлу:

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "module_globals or proxy or app_factory" -q
scripts/pre_merge_py312_check.sh KrabEar/tests/test_rest_lazy_singletons_M2.py
```

**Правило остановки:** если по сравнению с базовой линией сломалось **более пяти файлов** — остановиться, НЕ чинить их, доложить координатору с точным списком. Это триггер fallback-решения спеки §4.2.

- [ ] **Step 9: Коммит**

```bash
git add KrabEar/backend/rest_server.py KrabEar/tests/test_rest_lazy_singletons_M2.py
git commit -m "feat(rest): M2 Task 3 — ленивая инициализация standalone-синглтонов за guard"
```

---

</details>

---

### Task 4: `InProcessRestServer`

**Files:**
- Create: `KrabEar/backend/rest_inprocess.py`
- Test: `KrabEar/tests/test_rest_inprocess_server_M2.py`

**Interfaces:**
- Consumes: `settings.REST_IN_PROCESS_ENABLED`, `settings.REST_SERVER_PORT` (Task 1); код `rest.port_conflict` (Task 2); `rest_server.create_app`, `rest_server.StaticDeps` (M1).
- Produces:
  - `InProcessRestServer(deps, settings, error_push=None)` — конструктор.
  - `.start() -> bool` — True если сервер поднялся; False при выключенном рубильнике или EADDRINUSE (никогда не бросает).
  - `.stop(timeout: float = 5.0) -> None` — идемпотентный останов.
  - `.status() -> dict` — `{"enabled": bool, "running": bool, "port": int, "error": str | None}`.
  Всё это использует задача 6 (проводка) и задача 7 (диагностика).

- [ ] **Step 1: Написать падающий тест**

Создать `KrabEar/tests/test_rest_inprocess_server_M2.py`:

```python
"""M2: InProcessRestServer — старт/останов, выключенный рубильник, EADDRINUSE."""
import socket
import sys
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.rest_inprocess import InProcessRestServer  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _TinyApp:
    """Минимальное WSGI-приложение вместо настоящего Flask-app.

    Тест проверяет ТРАНСПОРТ (тред, порт, shutdown), а не REST-контракт —
    поднимать полный create_app() здесь значит тащить AudioEngine в юнит-тест.
    """

    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]


class InProcessRestServerTest(unittest.TestCase):
    def test_disabled_switch_does_not_start(self):
        cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=False, REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg)
        self.assertFalse(srv.start())
        self.assertFalse(srv.status()["running"])
        self.assertIs(srv.status()["enabled"], False)
        srv.stop()

    def test_starts_and_serves_then_stops(self):
        port = _free_port()
        cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=True, REST_SERVER_PORT=port)
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg)
        self.assertTrue(srv.start())
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            self.assertTrue(srv.status()["running"])
        finally:
            srv.stop()
        self.assertFalse(srv.status()["running"])

    def test_port_conflict_fails_open_and_pushes_error(self):
        port = _free_port()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        pushed = []
        try:
            cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=True, REST_SERVER_PORT=port)
            srv = InProcessRestServer(
                app=_TinyApp(), settings=cfg,
                error_push=lambda code, detail: pushed.append((code, detail)),
            )
            self.assertFalse(srv.start())          # fail-open, НЕ исключение
            self.assertFalse(srv.status()["running"])
            self.assertIsNotNone(srv.status()["error"])
            self.assertEqual([c for c, _ in pushed], ["rest.port_conflict"])
            srv.stop()
        finally:
            blocker.close()

    def test_stop_is_idempotent(self):
        cfg = SimpleNamespace(REST_IN_PROCESS_ENABLED=True, REST_SERVER_PORT=_free_port())
        srv = InProcessRestServer(app=_TinyApp(), settings=cfg)
        srv.start()
        srv.stop()
        srv.stop()   # второй раз не должен бросать


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Прогнать — ожидается FAIL**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_inprocess_server_M2.py -v
```

Ожидание: `ModuleNotFoundError: No module named 'backend.rest_inprocess'`.

- [ ] **Step 3: Написать модуль**

Создать `KrabEar/backend/rest_inprocess.py`:

```python
"""InProcessRestServer — REST-сервер внутри backend-процесса (волна M2).

Спека: docs/superpowers/specs/2026-07-16-m-series-rest-merge-design.md §4.2.

Зачем не app.run(): у Flask'а нет чистого останова — процесс завершается
вместе с сервером. make_server() отдаёт объект с .shutdown(), который можно
позвать из GracefulShutdownHandler и дождаться выхода треда.

Направление отказа — fail-open: любой сбой старта (занятый порт, ошибка
биндинга) НЕ роняет backend. Диктовка важнее веб-сервера, а порт может
держать ещё не выгруженный легаси-агент ai.krab.ear.rest.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.RestInProcess")

SHUTDOWN_JOIN_TIMEOUT_SEC = 5.0


class InProcessRestServer:
    """Владелец WSGI-сервера, поднятого в daemon-треде текущего процесса."""

    def __init__(
        self,
        app: Any,
        settings: Any,
        error_push: Callable[[str, str], None] | None = None,
    ) -> None:
        self._app = app
        self._enabled = bool(getattr(settings, "REST_IN_PROCESS_ENABLED", False))
        self._port = int(getattr(settings, "REST_SERVER_PORT", 5005))
        self._error_push = error_push

        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Поднимает сервер. True если слушает; False при выключенном
        рубильнике или сбое биндинга. НИКОГДА не бросает."""
        if not self._enabled:
            logger.info("InProcessRestServer: выключен рубильником")
            return False

        with self._lock:
            if self._server is not None:
                return True
            try:
                from werkzeug.serving import make_server

                self._server = make_server(
                    "127.0.0.1", self._port, self._app, threaded=True
                )
            except OSError as exc:
                # EADDRINUSE — самый вероятный: легаси rest-агент ещё жив.
                self._error = f"{type(exc).__name__}: {exc}"
                self._server = None
                logger.error(
                    "InProcessRestServer: порт %s занят — работаем БЕЗ "
                    "встроенного REST (%s)", self._port, self._error,
                )
                self._push_error(
                    "rest.port_conflict",
                    f"127.0.0.1:{self._port} занят: {self._error}",
                )
                return False
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                self._server = None
                logger.exception("InProcessRestServer: не удалось создать сервер")
                self._push_error("rest.port_conflict", self._error)
                return False

            self._thread = threading.Thread(
                target=self._serve,
                name="rest-inprocess",
                daemon=True,
            )
            self._thread.start()

        self._error = None
        logger.info("InProcessRestServer: слушает 127.0.0.1:%s", self._port)
        return True

    def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        try:
            server.serve_forever()
        except Exception:
            # Тред-граница: необработанное исключение здесь тихо убило бы
            # REST и оставило status() врать про running=True.
            logger.exception("InProcessRestServer: serve_forever упал")
            with self._lock:
                self._error = "serve_forever crashed"
                self._server = None

    def stop(self, timeout: float = SHUTDOWN_JOIN_TIMEOUT_SEC) -> None:
        """Идемпотентный останов: shutdown() + join треда."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server, self._thread = None, None

        if server is not None:
            try:
                server.shutdown()
            except Exception:
                logger.exception("InProcessRestServer: shutdown() бросил")
            try:
                server.server_close()
            except Exception:
                logger.exception("InProcessRestServer: server_close() бросил")

        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(
                    "InProcessRestServer: тред не вышел за %.1fс", timeout
                )

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._server is not None and (
                self._thread is not None and self._thread.is_alive()
            )
            return {
                "enabled": self._enabled,
                "running": bool(running),
                "port": self._port,
                "error": self._error,
            }

    # ------------------------------------------------------------------

    def _push_error(self, code: str, detail: str) -> None:
        if self._error_push is None:
            return
        try:
            self._error_push(code, detail)
        except Exception:
            logger.exception("InProcessRestServer: error_push бросил")
```

- [ ] **Step 4: Прогнать — ожидается PASS**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_inprocess_server_M2.py -v
```

Ожидание: 4 passed. Если `test_port_conflict_fails_open_and_pushes_error` не падает при занятом порте — проверить, что тест-сокет действительно слушает (`listen(1)` обязателен: `make_server` с `SO_REUSEADDR` может привязаться к небиндженному порту).

- [ ] **Step 5: ubuntu-parity + audit**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_rest_inprocess_server_M2.py
make audit-all
```

`audit_dead_extracted_modules` обязан пройти: новый модуль пока не имеет прод-импортёров, и гейт это заметит. Если гейт краснеет — **не добавлять исключение**, а сделать задачу 6 в том же PR (импортёр появится там). Порядок задач это учитывает.

- [ ] **Step 6: Коммит**

```bash
git add KrabEar/backend/rest_inprocess.py KrabEar/tests/test_rest_inprocess_server_M2.py
git commit -m "feat(rest): M2 Task 4 — InProcessRestServer (make_server в daemon-треде, fail-open)"
```

---

### Task 5: Мост не включается при in-process

**Files:**
- Modify: `KrabEar/backend/event_bridge.py` (`__init__`, строка с `self._enabled`)
- Test: `KrabEar/tests/test_event_bridge_offline_when_inprocess_M2.py`

**Interfaces:**
- Consumes: `settings.REST_IN_PROCESS_ENABLED` (Task 1).
- Produces: инвариант «в слитом процессе мост выключен». Задача 7 показывает это через `get_diagnostics.event_bridge.state == "disabled"`.

**Почему именно так.** В слитом процессе шина одна: событие, отправленное мостом на `/internal/event`, вернулось бы в ту же шину — классическая двойная доставка одного побочного эффекта из двух точек. Правка делается **внутри** `EventBridge`, а не условием вокруг его создания в `service.py`, по двум причинам: (1) объект должен продолжать существовать — на него ссылается диагностика (`service.py:1243`); (2) существующий флаг `_enabled=False` уже даёт ровно нужное поведение — `on_event` возвращается сразу, `state` становится `"disabled"`, очередь не растёт. Никакой новой логики.

- [ ] **Step 1: Написать падающий тест**

```python
"""M2: EventBridge не работает, когда REST поднят внутри процесса."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.event_bridge import EventBridge  # noqa: E402


class EventBridgeOfflineWhenInProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _bridge(self, *, bridge_on: bool, in_process: bool) -> EventBridge:
        cfg = SimpleNamespace(
            EVENT_BRIDGE_ENABLED=bridge_on,
            REST_IN_PROCESS_ENABLED=in_process,
            REST_SERVER_PORT=5005,
        )
        return EventBridge(settings=cfg, data_dir=self.data_dir)

    def test_disabled_when_rest_is_in_process(self):
        b = self._bridge(bridge_on=True, in_process=True)
        self.assertEqual(b.status()["state"], "disabled")

    def test_in_process_bridge_ignores_events(self):
        b = self._bridge(bridge_on=True, in_process=True)
        b.on_event("krab_error", {"code": "test.code"})
        self.assertEqual(b.status()["queue_depth"], 0)

    def test_still_enabled_in_two_process_mode(self):
        b = self._bridge(bridge_on=True, in_process=False)
        self.assertEqual(b.status()["state"], "unknown")   # «ещё не пробовал»
        b.on_event("krab_error", {"code": "test.code"})
        self.assertEqual(b.status()["queue_depth"], 1)


if __name__ == "__main__":
    unittest.main()
```

⚠️ Имя метода статуса уточнить по коду: в `event_bridge.py` секция диагностики строится около строки 320 (`"state": self._state`). Если публичный метод называется не `status()`, а иначе — использовать фактическое имя во всех трёх тестах.

- [ ] **Step 2: Прогнать — ожидается FAIL**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge_offline_when_inprocess_M2.py -v
```

Ожидание: первые два FAIL (state `"unknown"` вместо `"disabled"`, queue_depth 1 вместо 0), третий PASS.

- [ ] **Step 3: Правка одной строки**

В `KrabEar/backend/event_bridge.py`, в `__init__`, заменить:

```python
        self._enabled = bool(getattr(settings, "EVENT_BRIDGE_ENABLED", True))
```

на:

```python
        # M2: в слитом процессе (REST внутри backend) шина ОДНА — мост создал бы
        # echo: событие ушло бы на /internal/event и вернулось в ту же шину.
        # Поэтому in-process режим выключает мост так же жёстко, как killswitch.
        self._enabled = bool(getattr(settings, "EVENT_BRIDGE_ENABLED", True)) and not bool(
            getattr(settings, "REST_IN_PROCESS_ENABLED", False)
        )
```

- [ ] **Step 4: Прогнать — ожидается PASS**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bridge_offline_when_inprocess_M2.py -v
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "event_bridge" -q
```

Ожидание: 3 passed + весь существующий пласт моста зелёный (двухпроцессное поведение не изменилось: без нового поля `getattr` вернёт False).

- [ ] **Step 5: Коммит**

```bash
git add KrabEar/backend/event_bridge.py KrabEar/tests/test_event_bridge_offline_when_inprocess_M2.py
git commit -m "feat(bridge): M2 Task 5 — мост выключен при rest_in_process_enabled (анти-echo)"
```

---

### Task 6: Проводка в BackendService

**Files:**
- Modify: `KrabEar/backend/service.py` (рядом с блоком EventBridge ~строка 791; `close()` ~строка 1747)
- Test: `KrabEar/tests/test_service_rest_inprocess_wiring_M2.py`

**Interfaces:**
- Consumes: `InProcessRestServer` (Task 4), `rest_server.create_app`/`StaticDeps` (M1), `_ensure_standalone_singletons` не используется — deps берутся у backend.
- Produces: `BackendService._rest_inprocess: InProcessRestServer | None`. Читает задача 7.

- [ ] **Step 1: Написать падающий тест**

```python
"""M2: BackendService поднимает in-process REST при включённом рубильнике."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class ServiceRestInProcessWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.service = None

    def tearDown(self):
        # ОБЯЗАТЕЛЬНО: daemon-треды BackendService иначе валят весь чанк
        # (feedback_backendservice_teardown_ci.md).
        if self.service is not None:
            self.service.close()
        self._tmp.cleanup()

    def _make_service(self, in_process: bool):
        from backend.service import BackendService
        with patch("core.config.settings.REST_IN_PROCESS_ENABLED", in_process, create=True):
            self.service = BackendService(data_dir=str(self.data_dir))
        return self.service

    def test_off_by_default_leaves_server_absent_or_stopped(self):
        svc = self._make_service(False)
        srv = getattr(svc, "_rest_inprocess", None)
        if srv is not None:
            self.assertFalse(srv.status()["running"])

    def test_close_stops_the_server(self):
        svc = self._make_service(False)
        svc.close()
        srv = getattr(svc, "_rest_inprocess", None)
        if srv is not None:
            self.assertFalse(srv.status()["running"])
        self.service = None   # уже закрыт


if __name__ == "__main__":
    unittest.main()
```

⚠️ Сигнатуру `BackendService(...)` взять из соседних тестов (`grep -n "BackendService(" KrabEar/tests/test_backend_service.py | head -3`) — она может требовать больше аргументов. Тест с `in_process=True` в юнит-слое НЕ пишем: он поднял бы настоящий порт; живое поведение проверяет задача 8.

- [ ] **Step 2: Прогнать — ожидается FAIL/пропуск**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_service_rest_inprocess_wiring_M2.py -v
```

Ожидание: тесты проходят вхолостую (атрибута нет). Это допустимая отправная точка — ценность теста в шаге 4, после появления атрибута. Если падает с ошибкой конструктора — починить сигнатуру и повторить.

- [ ] **Step 3: Добавить проводку**

В `KrabEar/backend/service.py`, сразу ПОСЛЕ блока `self._event_bridge.start()` (~строка 796):

```python
        # M2: REST внутри этого же процесса (спека §4.2). Рубильник по умолчанию
        # выключен — прод продолжает работать на двух процессах, пока владелец
        # не решит иначе. При включении EventBridge выше уже сам выключился
        # (event_bridge.py читает тот же рубильник) — echo невозможен.
        self._rest_inprocess = None
        if bool(getattr(settings, "REST_IN_PROCESS_ENABLED", False)):
            try:
                from backend.rest_inprocess import InProcessRestServer
                from backend.rest_server import StaticDeps, create_app
                from backend.event_bus import bus as _event_bus_singleton
                from backend.event_bus import sse_stream as _sse_stream
                from backend.metrics_collector import metrics as _metrics_singleton_ref

                deps = StaticDeps(
                    engine=self.transcriber.engine,
                    store=self.store,
                    transcriber=self.transcriber,
                    translator=self.translator,
                    tts_service=self._tts_service,
                    metrics=_metrics_singleton_ref,
                    event_bus=_event_bus_singleton,
                    sse_stream=_sse_stream,
                )
                self._rest_inprocess = InProcessRestServer(
                    app=create_app(deps),
                    settings=settings,
                    error_push=lambda code, detail: self._error_bus.push(
                        code=code, detail=detail
                    ),
                )
                self._rest_inprocess.start()
            except Exception:
                # Fail-open: встроенный REST не должен мешать диктовке.
                logger.exception("in-process REST: не удалось поднять")
                self._rest_inprocess = None
```

⚠️ Три имени обязательно сверить с фактическим кодом перед запуском: `self.transcriber.engine`, `self.translator`, `self._tts_service`, `self._error_bus.push(...)`. Если атрибут называется иначе — использовать фактический. Проверка: `grep -n "self._tts_service\|self.translator\|self._error_bus" KrabEar/backend/service.py | head`. Сигнатуру `ErrorBus.push` уточнить в `backend/error_bus.py` — если она принимает `KrabError`, а не kwargs, собрать объект.

В `close()` (~строка 1755, сразу после блока остановки EventBridge):

```python
        # Stop in-process REST (M2) — тот же daemon-thread teardown rule.
        rest_inprocess = getattr(self, "_rest_inprocess", None)
        if rest_inprocess is not None:
            try:
                rest_inprocess.stop()
            except Exception:
                logger.exception("InProcessRestServer.stop() raised during close()")
```

- [ ] **Step 4: Прогнать тесты**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_service_rest_inprocess_wiring_M2.py -v
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_backend_service.py -q
```

Ожидание: оба зелёные. `test_backend_service.py` известен тем, что виснет локально при живом прод-backend (конкуренция за flock `StateStore`) — при зависании прогнать через ubuntu-parity: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_backend_service.py`.

- [ ] **Step 4b: Проверить путь останова через GracefulShutdownHandler (спека §4.2)**

Спека требует останова «в `GracefulShutdownHandler`». План вешает его на `BackendService.close()` — это корректно ТОЛЬКО если handler зовёт `close()`. Проверить:

```bash
grep -n "close()\|_close_socket\|def shutdown" KrabEar/backend/shutdown_handler.py | head -20
```

Если `shutdown()` не приводит к `service.close()` — добавить остановку `_rest_inprocess` прямо в `shutdown_handler.py` рядом с `_broadcast_event_bus_sentinel` (строка 242). **Сентинел уже реализован и вызывается** (`_broadcast_event_bus_sentinel`, строка 388) — заново его НЕ писать, спека описывает его как работу M2 ошибочно.

- [ ] **Step 4c: Зафиксировать желаемое поведенческое изменение (спека §4.3)**

В слитом процессе события REST-происхождения (`live_subs.result` и т.п.) впервые становятся видны IPC-слушателям: webhooks, `event_replay`, error-подписчикам. Спека называет это желаемым и требует зафиксировать тестом. Добавить в `test_service_rest_inprocess_wiring_M2.py`:

```python
    def test_rest_origin_event_reaches_ipc_listeners(self):
        """В слитом процессе шина одна — IPC-слушатель видит REST-событие.

        До M2 это было невозможно: два процесса = две шины. Тест фиксирует
        изменение как намеренное, чтобы будущая правка не откатила его молча.
        """
        from backend.event_bus import bus
        seen = []
        bus.add_listener(lambda t, p: seen.append(t))
        bus.emit("live_subs.result", {"text": "проверка"})
        self.assertIn("live_subs.result", seen)
```

Тест не требует поднятого сервера: он проверяет, что шина — общий module-синглтон, то есть саму предпосылку слияния.

- [ ] **Step 5: Аудит-гейты**

```bash
make audit-all
```

Ожидание: зелено. Теперь `rest_inprocess.py` имеет прод-импортёра, и `audit_dead_extracted_modules` доволен. `audit_decorative_wiring` тоже: поле `_rest_inprocess` читается в `close()` и в задаче 7.

- [ ] **Step 6: Коммит**

```bash
git add KrabEar/backend/service.py KrabEar/tests/test_service_rest_inprocess_wiring_M2.py
git commit -m "feat(service): M2 Task 6 — сборка StaticDeps и старт in-process REST"
```

---

### Task 7: Диагностика `rest_in_process`

**Files:**
- Modify: `KrabEar/backend/health_check_service.py` (словарь диагностики ~строка 203, рядом с `"event_bridge"`), при необходимости — прокидывание в конструктор из `service.py:1243`
- Test: `KrabEar/tests/test_diagnostics_rest_inprocess_M2.py`

**Interfaces:**
- Consumes: `BackendService._rest_inprocess` (Task 6), `.status()` (Task 4).
- Produces: `get_diagnostics()["rest_in_process"]` → `{"enabled", "running", "port", "error"}`.

- [ ] **Step 1: Написать падающий тест**

```python
"""M2: get_diagnostics отдаёт секцию rest_in_process."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.health_check_service import HealthCheckService  # noqa: E402


class _FakeRestInProcess:
    def status(self):
        return {"enabled": True, "running": True, "port": 5005, "error": None}


class DiagnosticsRestInProcessTest(unittest.TestCase):
    def test_section_present_when_server_wired(self):
        svc = HealthCheckService(rest_inprocess=_FakeRestInProcess())
        section = svc._build_rest_inprocess_summary()
        self.assertEqual(section["port"], 5005)
        self.assertIs(section["running"], True)

    def test_section_degrades_gracefully_when_absent(self):
        svc = HealthCheckService(rest_inprocess=None)
        section = svc._build_rest_inprocess_summary()
        self.assertIs(section["enabled"], False)
        self.assertIs(section["running"], False)


if __name__ == "__main__":
    unittest.main()
```

⚠️ `HealthCheckService(...)` принимает много коллабораторов (см. `service.py:1240-1249`). Сконструировать его в тесте с нужными kwargs, а недостающие обязательные — передать `None`/фейками; если конструктор строгий, тестировать через `BackendService` с `close()` в `tearDown`.

- [ ] **Step 2: Прогнать — ожидается FAIL**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_diagnostics_rest_inprocess_M2.py -v
```

Ожидание: `TypeError: unexpected keyword argument 'rest_inprocess'` либо `AttributeError: _build_rest_inprocess_summary`.

- [ ] **Step 3: Реализовать**

В `KrabEar/backend/health_check_service.py` — принять коллаборатора в `__init__` (по образцу `event_bridge`), сохранить в `self._rest_inprocess`, добавить метод:

```python
    def _build_rest_inprocess_summary(self) -> dict:
        """Секция rest_in_process (M2). Никогда не бросает.

        Отсутствующий сервер — это НЕ ошибка: дефолт волны — выключено,
        и тогда честный ответ «выключен», а не пустой словарь.
        """
        srv = getattr(self, "_rest_inprocess", None)
        if srv is None:
            return {"enabled": False, "running": False, "port": None, "error": None}
        try:
            return dict(srv.status())
        except Exception:
            logger.exception("rest_in_process: status() бросил")
            return {"enabled": None, "running": False, "port": None,
                    "error": "status_failed"}
```

И в словарь диагностики, рядом с `"event_bridge"`:

```python
            # In-process REST (spec 2026-07-16 §4.2): enabled/running/port/error.
            "rest_in_process": self._build_rest_inprocess_summary(),
```

В `KrabEar/backend/service.py:1243`, рядом с `event_bridge=self._event_bridge,`:

```python
            rest_inprocess=self._rest_inprocess,
```

- [ ] **Step 4: Прогнать**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_diagnostics_rest_inprocess_M2.py -v
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "diagnostic or health_check" -q
```

- [ ] **Step 5: Коммит**

```bash
git add KrabEar/backend/health_check_service.py KrabEar/backend/service.py KrabEar/tests/test_diagnostics_rest_inprocess_M2.py
git commit -m "feat(diagnostics): M2 Task 7 — секция rest_in_process в get_diagnostics"
```

---

### Task 8: Нагрузочный смок против живого in-process REST

**Files:**
- Create: `scripts/rest_inprocess_load_smoke.py`
- Modify: `docs/ROADMAP-2026H2.md` (журнал волны), `CLAUDE.md` (карта модулей: `rest_inprocess.py`)

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: исполняемый смок; exit 0 только если все фазы прошли.

**Почему смок, а не юнит-тесты.** Проверяемое — поведение при конкуренции: три SSE-потока, WS и параллельные POST против одного процесса. Мокнутые юниты этого не ловят; в проекте это уже дало результат дважды (смок владения R2, e2e моста).

- [ ] **Step 0: Замерить RAM до включения (спека §4.5)**

```bash
PYTHONPATH=$(pwd)/KrabEar python scripts/memory_baseline.py > /tmp/m2_ram_two_process.csv
cat /tmp/m2_ram_two_process.csv
```

Записать суммарный RSS двух процессов. После шага 2 повторить замер при `REST_IN_PROCESS_ENABLED=true` и сравнить: ожидание спеки — минус сотни МБ за счёт исчезнувшего дубля `AudioEngine`/`StateStore`. Если экономии НЕТ — это сигнал, что guard задачи 3 не сработал и standalone-комплект всё-таки строится; доложить, не замалчивать.

- [ ] **Step 1: Написать смок**

Создать `scripts/rest_inprocess_load_smoke.py`. Образец структуры (teardown через `try/finally`, throwaway data-dir, облегчение окружения) — `scripts/e2e_owner_gate_smoke.py`, написанный в волне R2; переиспользовать его хелперы подключения к сокету вместо написания своих.

Каркас, задающий обязательные фазы:

```python
#!/usr/bin/env python3
"""M2: нагрузочный смок in-process REST (спека §4.5).

Поднимает THROWAWAY backend с REST внутри процесса и проверяет поведение
под конкуренцией — то, чего не видят юнит-тесты с моками.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PHASE_TIMEOUT_SEC = 120.0        # урок R2: тесные таймауты режут валидную работу
LIGHT_SETTINGS = {               # без этого смок висит 60-85с на GigaAM-воркере
    "stt_gigaam_enabled": False,
    "diarization_enabled": False,
    "llm_rewriter_enabled": False,
    "realtime_partial_enabled": False,
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def phase_health(base_url: str) -> None:
    """Фаза 1: контракт Voice Gateway — /health отдаёт status и profile."""
    with urllib.request.urlopen(f"{base_url}/health", timeout=5) as r:
        assert r.status == 200, r.status
        body = json.loads(r.read())
    assert "status" in body and "profile" in body, body
    print("OK: /health содержит status+profile")


def phase_concurrency(base_url: str) -> float:
    """Фаза 2: 3 SSE-подписчика + 20 конкурентных POST. Возвращает p95."""
    stop = threading.Event()
    sse_threads = [
        threading.Thread(target=_drain_sse, args=(base_url, stop), daemon=True)
        for _ in range(3)
    ]
    for t in sse_threads:
        t.start()
    latencies = []
    lock = threading.Lock()

    def _hit():
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=30):
                pass
        finally:
            with lock:
                latencies.append(time.monotonic() - t0)

    workers = [threading.Thread(target=_hit) for _ in range(20)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=PHASE_TIMEOUT_SEC)
    stop.set()
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1]
    print(f"OK: 20 конкурентных POST при 3 живых SSE, p95={p95:.3f}s")
    return p95


def phase_no_echo(sock_path: Path, base_url: str) -> None:
    """Фаза 3: событие из IPC-шины доходит до SSE РОВНО ОДИН раз.

    Мост при in-process выключен (Task 5); если он всё же жив, подписчик
    увидит дубль — это класс "двойная доставка из двух точек".
    """
    ...  # подписаться на /v1/events, эмитнуть krab_error через IPC,
         # собрать события 3с, assert счётчик == 1


def phase_shutdown(service_handle, port: int) -> None:
    """Фаза 4: close() освобождает порт и гасит треды."""
    service_handle.close()
    time.sleep(1.0)
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))   # успешный bind = порт реально свободен
    finally:
        s.close()
    print("OK: порт освобождён после close()")
```

Фазу `phase_no_echo` дописать полностью (в каркасе оставлено `...` — это единственное место, требующее ручной работы; подписка на SSE берётся из `run_e2e_bridge_smoke.command`, где она уже реализована).

Полный список обязательных фаз:

1. Поднять throwaway `BackendService` на temp data-dir со случайным свободным портом и `KRAB_EAR_REST_IN_PROCESS_ENABLED=true`.
2. **Облегчить окружение** — обязательно, урок R2: `stt_gigaam_enabled=False`, `diarization_enabled=False`, `llm_rewriter_enabled=False`, `realtime_partial_enabled=False`. Без этого смок висит по 60–85 с на попытках поднять GigaAM-worker.
3. `GET /health` → 200, тело содержит `status` и `profile` (контракт Voice Gateway).
4. Три параллельных SSE-подписчика на `/v1/events` + 20 конкурентных `POST` на лёгкий эндпойнт; замерить p95, сравнить с прогоном при `REST_IN_PROCESS_ENABLED=false`.
5. Событие, эмитнутое в IPC-шину, доходит до SSE-подписчика **ровно один раз** (анти-echo: мост выключен).
6. `service.close()` → сервер перестаёт слушать, треды вышли; проверить, что порт освободился.
7. `finally`-teardown с `trap`-семантикой: снести temp data-dir при любом исходе.

Таймаут каждой фазы — не менее 120 с (урок R2: тесные таймауты режут валидную работу).

- [ ] **Step 2: Прогнать смок**

```bash
PYTHONPATH=$(pwd)/KrabEar python -u scripts/rest_inprocess_load_smoke.py
```

`python -u` обязателен: иначе буферизация скроет прогресс при зависании.
Ожидание: `ALL GREEN`, exit 0.

- [ ] **Step 3: Обновить документацию**

- `CLAUDE.md`: добавить `backend/rest_inprocess.py` в список backend-модулей одной строкой в стиле соседей.
- `docs/ROADMAP-2026H2.md` §3.1: журнал волны M2 — что сделано, статус рубильника (выключен), что осталось до S3.

- [ ] **Step 4: Финальный гейт волны**

```bash
make pre-merge-check
make audit-all
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "rest or bridge or diagnostic" -q
```

- [ ] **Step 5: Коммит**

```bash
git add scripts/rest_inprocess_load_smoke.py CLAUDE.md docs/ROADMAP-2026H2.md
git commit -m "test(rest): M2 Task 8 — нагрузочный смок in-process REST + доки"
```

---

## Definition of Done волны

- [ ] Рубильник выключен по умолчанию; поведение прода **не изменилось** ни в одном наблюдаемом аспекте.
- [ ] `import backend.rest_server` не создаёт `AudioEngine` (задача 3), при этом 27 файлов / ≈752 теста категорий A/B/C зелёные **без правок** (кроме возможного счётчика кодов ошибок в задаче 2).
- [ ] При `rest_in_process_enabled=true`: REST отвечает изнутри backend-процесса, мост выключен, `get_diagnostics.rest_in_process.running=true`.
- [ ] Занятый порт не роняет backend и даёт `rest.port_conflict` в ErrorBus.
- [ ] `service.close()` останавливает сервер и его тред.
- [ ] Нагрузочный смок ALL GREEN; p95 POST не хуже двухпроцессного более чем на 15%.
- [ ] `make pre-merge-check`, `make audit-all`, flake8 CI-флагами — зелёные.
- [ ] PR открыт (не только push!) — иначе `ci.yml` не запустится на feature-ветке и половина гейта пропустится. Все раны проверены по **полному** SHA.

## Журнал отклонений от плана (заполняется по ходу исполнения)

Все пункты найдены исполнителями или гейтом координатора и проверены координатором против живого кода. Каждый — дефект **плана**, а не работы воркеров: план писался по аналогии с соседним кодом, и аналогия в этих местах не держала.

| Место в плане | Что было написано | Что оказалось в коде | Последствие, если бы не нашли |
|---|---|---|---|
| Task 1 Step 4 | выбор между dict и классом с аннотациями | `DEFAULT_SETTINGS: dict[str, Any]` — dict-литерал, строка 771 | воркер выбирал бы форму сам |
| Task 2 Step 3 | поля записи `severity`/`category` | `category` не существует; есть `action_id`, `action_label`, `dedupe_seconds` | несовместимая запись реестра |
| Task 2 (не покрыто) | — | префикса `rest` нет в `Component` Literal (`error_bus.py`) | **Task 4 падал бы на Pydantic-валидации в проде** |
| Task 2 (не покрыто) | один тест-счётчик кодов | второй счётчик в `test_error_codes.py:249` + строгий набор ключей | красный CI |
| Task 3 (вся задача) | ленивая инициализация | несовместима с патчингом категорий A и B | 20 регрессий, откат задачи |
| Task 3 Step 4 (не покрыто) | — | `_rest_engine_cleanup` (atexit) читает `engine` голым именем | `NameError` в процессе, не строившем комплект |
| Task 4 (код модуля) | `except OSError` / `except Exception` | `make_server` бросает **`SystemExit`** (`BaseException`!) — werkzeug сам ловит `OSError` и зовёт `sys.exit(1)` | **fail-open не работал: backend завершался при занятом порте** |
| Task 4 (код модуля) | `self._error = None` вне блока лока | единственная мутация состояния не под локом | конкурентный `status()` видел `running: True` со стухшей ошибкой |
| Task 3B тест | `assertNotIn(old_store, module_values)` | `in` идёт через `==`; `LocalProxy.__eq__` вне контекста запроса бросает `RuntimeError` | сломанный тест |
| Task 5 тесты | `bridge.status()` | метод называется `get_diagnostics()` | `AttributeError` в тесте |
| Task 6 (код) | `error_bus.push(code=…, detail=…)` | `push(err: KrabError)` — принимает объект с 9 полями | `TypeError` при первом же конфликте порта |

## Что НЕ делает эта волна

- Не включает рубильник в проде — это отдельное решение владельца после канарейки.
- Не трогает `mlx_inter_process_lock`, auth, CORS, rate-limit, схемы ответов.
- Не выводит легаси rest-агент и не правит `install_rest_launchagent.command` — это S3.
- Не мигрирует ~16 хрупких тест-файлов на фабрику — не-цель серии M.
- Не правит репозиторий Voice Gateway — только бриф в их сессию (S3).
