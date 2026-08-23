# Изоляция privacy_audit.log от тестов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Тестовый прогон и dev-backend больше не пишут в боевой compliance-журнал `~/Library/Application Support/KrabEar/privacy_audit.log`.

**Architecture:** Путь журнала резолвится ленивой функцией `_default_log_path()`, читающей env-переменную `KRAB_EAR_PRIVACY_AUDIT_DIR`; боевой дефолт (home-rooted) не меняется. `conftest.py` принудительно выставляет throwaway-каталог до импорта приложения, семь e2e-смоков экспортируют переменную рядом со своим `--data-dir`. Отдельно дашборд приватности переводится с двух полных чтений журнала на один потоковый проход.

**Tech Stack:** Python 3.14 (dev venv `.venv_krab_ear`) / Python 3.12 (ubuntu CI — настоящий гейт), pytest, unittest.TestCase, bash 3.2 для `.command`-скриптов.

**Спека:** [docs/superpowers/specs/2026-08-23-privacy-audit-path-isolation-design.md](../specs/2026-08-23-privacy-audit-path-isolation-design.md)

## Global Constraints

- **Ветка:** `claude/agitated-ritchie-5b33f1`. 🔴 В `codex/krab-ear-v2` НЕ пушить — только PR.
- **🔴 ЧУЖАЯ ЗОНА — не трогать:** метод `PrivacyAuditLogger.log_event`, `_read_chain_tip`, права `600` при создании файла. Их правит параллельная сессия `xenodochial-cannon-3fc148` в этом же файле (её фикс уже закоммичен: `7923f15b`). Наша зона в `privacy_audit.py` — только резолв пути (строка 28 и первая строка `__init__`) плюс новый метод `summarize()` в конце класса.
- **🔴 Номера строк — подсказка, а не адрес.** Соседний коммит уже сдвинул `privacy_audit.py` на ~+7 строк (константы `_LOG_FILE_MODE` / `_MAX_TIP_SCAN_BYTES` вставлены НАД строкой 28). Все правки ниже заданы точным ТЕКСТОМ и переживут мерж — искать по тексту, сверять номер вторым.
- **Рабочий каталог:** worktree этой ветки. Команды используют `cd "$(git rev-parse --show-toplevel)"` намеренно — прежняя версия плана содержала абсолютный путь на worktree, который был удалён вместе с веткой (восстановлена из dangling-объектов).
- **TDD обязателен:** RED пишется первым и обязан падать по причине «фичи нет», а не из-за опечатки.
- **Имя env-переменной ровно:** `KRAB_EAR_PRIVACY_AUDIT_DIR` (каталог, не файл).
- **Боевой дефолт не меняется:** `Path.home() / "Library" / "Application Support" / "KrabEar" / "privacy_audit.log"`. Если после правок `make audit-purge-coverage` обнаружит новый store — это ошибка реализации (путь утёк под `data_dir`), а не повод править allowlist.
- **ubuntu-parity:** каждый изменённый тестовый файл прогнать через `scripts/pre_merge_py312_check.sh`. Локальный зелёный на 3.14 с mlx ничего не доказывает.
- **Кодировка сообщений:** комментарии и докстринги — по-русски, как в окружающем коде.

---

## File Structure

| Файл | Ответственность | Действие |
|---|---|---|
| `KrabEar/backend/privacy_audit.py` | резолв пути журнала + агрегат `summarize()` | modify (2 точки, механику записи не трогаем) |
| `KrabEar/tests/conftest.py` | принудительный throwaway-путь до импорта приложения + сброс синглтона между тестами | modify |
| `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py` | все тесты волны | create |
| `KrabEar/backend/service.py` | дашборд приватности переходит на `summarize()` | modify (`_handle_get_privacy_dashboard`) |
| `scripts/run_e2e_smokes.command` | экспорт переменной рядом с throwaway data-dir | modify |
| `scripts/run_e2e_bridge_smoke.command` | то же | modify |
| `scripts/e2e_owner_gate_smoke.py` | то же, через `env` подпроцесса | modify |
| `scripts/e2e_rescue_smoke.py` | то же (backend поднимается дважды) | modify |
| `scripts/e2e_recommended_setup_smoke.py` | то же | modify |
| `scripts/rest_inprocess_load_smoke.py` | то же | modify |
| `scripts/s3_gpu_contention_smoke.py` | то же | modify |
| `KrabEar/tests/test_privacy_dashboard.py` | перенацелить симуляцию отказа с `total_count` на `summarize` | modify |

---

## Task 1: Резолв пути через env-переменную

**Files:**
- Modify: `KrabEar/backend/privacy_audit.py:28-30` (константа `_DEFAULT_LOG_PATH`) и `:124` (первая строка `__init__`)
- Test: `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py` (создаётся здесь)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: `backend.privacy_audit._default_log_path() -> Path` — ленивый резолв; `_ENV_DIR_VAR: str = "KRAB_EAR_PRIVACY_AUDIT_DIR"`; `_LOG_FILENAME: str = "privacy_audit.log"`. Задачи 2 и 4 полагаются на эти имена.

- [ ] **Step 1: Написать падающий тест**

Создать `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py`:

```python
"""Изоляция пути privacy_audit.log (инцидент 2026-08-23).

Боевой compliance-журнал ~/Library/Application Support/KrabEar/privacy_audit.log
набрал 44 907 записей privacy/purge_all_data из 50 041 — тестовый мусор. Корень:
путь был захардкожен модульной константой мимо env и мимо data_dir, а
PrivacyAuditLogger — синглтон, поэтому 17 из 20 purge-тестов писали в боевой файл.
Следствие: реальный purge владельца стал неотличим от тестового.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.privacy_audit import (  # noqa: E402
    PrivacyAuditLogger,
    _default_log_path,
)

# Боевой путь, записанный ЯВНО: тест обязан ловить регрессию, даже если
# продовый дефолт в модуле кто-то переопределит.
_PROD_LOG_PATH = (
    Path.home() / "Library" / "Application Support" / "KrabEar" / "privacy_audit.log"
)


def test_env_var_redirects_log_path(tmp_path, monkeypatch):
    """Выставленная переменная уводит журнал в свой каталог."""
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", str(tmp_path))

    logger = PrivacyAuditLogger()

    assert logger._log_path == tmp_path / "privacy_audit.log"


def test_key_file_follows_log_dir(tmp_path, monkeypatch):
    """HMAC-ключ создаётся рядом с журналом, а не в боевом каталоге.

    _load_or_create_key берёт каталог как self._log_path.parent, поэтому ключ
    обязан переехать вместе с журналом без отдельной переменной.
    """
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", str(tmp_path))

    logger = PrivacyAuditLogger()
    logger.log_event("test", "isolation_probe")

    assert (tmp_path / "privacy_audit.key").exists()
    assert (tmp_path / "privacy_audit.log").exists()


def test_no_env_falls_back_to_home_default(monkeypatch):
    """Без переменной — прежний боевой путь (обратная совместимость).

    Зовём ТОЛЬКО чистую функцию: конструктор создал бы каталог и записал ключ
    в боевую директорию, чего тест делать не имеет права.
    """
    monkeypatch.delenv("KRAB_EAR_PRIVACY_AUDIT_DIR", raising=False)

    assert _default_log_path() == _PROD_LOG_PATH


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_env_falls_back_to_default(monkeypatch, blank):
    """Пустое значение трактуется как «не задано» — fail-safe от опечатки.

    Иначе Path("") / "privacy_audit.log" увёл бы compliance-журнал в CWD.
    """
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", blank)

    assert _default_log_path() == _PROD_LOG_PATH
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py -v
```

Expected: **сбор падает** с `ImportError: cannot import name '_default_log_path' from 'backend.privacy_audit'`. Это корректный RED — символа нет, потому что фичи нет.

- [ ] **Step 3: Минимальная реализация**

В `KrabEar/backend/privacy_audit.py` заменить блок на строках 28-30:

```python
_DEFAULT_LOG_PATH = (
    Path.home() / "Library" / "Application Support" / "KrabEar" / "privacy_audit.log"
)
```

на:

```python
_LOG_FILENAME = "privacy_audit.log"

# Каталог журнала переопределяется этой переменной. Нужна для изоляции тестов и
# dev-инстансов от боевого compliance-журнала (инцидент 2026-08-23: 44 907 из
# 50 041 записи оказались тестовым мусором). Читаем os.environ напрямую, а не
# settings из core/config.py: privacy_audit — листовой модуль без проектных
# импортов, config втянул бы цикл через backend.service.
_ENV_DIR_VAR = "KRAB_EAR_PRIVACY_AUDIT_DIR"


def _default_log_path() -> Path:
    """Путь журнала: env-переменная, иначе боевой home-rooted дефолт.

    Резолвится ЛЕНИВО, при создании инстанса, а не на импорте модуля: константа
    привязала бы изоляцию к порядку импортов — тот самый класс мин, что уже
    кусал репозиторий (sys.modules-стабы, chunk-pollution rest_server).

    Пустое/пробельное значение считается незаданным — fail-safe в сторону
    боевого дефолта, иначе опечатка увела бы журнал в текущий каталог.
    """
    raw = os.environ.get(_ENV_DIR_VAR, "")
    if raw.strip():
        return Path(raw).expanduser() / _LOG_FILENAME
    return Path.home() / "Library" / "Application Support" / "KrabEar" / _LOG_FILENAME
```

Затем в `__init__` (строка 124) заменить:

```python
        self._log_path: Path = log_path if log_path is not None else _DEFAULT_LOG_PATH
```

на:

```python
        self._log_path: Path = log_path if log_path is not None else _default_log_path()
```

`os` и `Path` в модуле уже импортированы (строки 19 и 23) — новых импортов не нужно.

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py -v
```

Expected: **6 passed** (4 теста, из них `test_blank_env_falls_back_to_default` параметризован тремя значениями).

- [ ] **Step 5: Убедиться, что не сломаны существующие тесты логгера**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit.py KrabEar/tests/test_privacy_audit_hash_chain.py KrabEar/tests/test_privacy_audit_clear.py -v
```

Expected: PASS. Эти тесты конструируют логгер с ЯВНЫМ `log_path`, поэтому резолв дефолта их не касается; падение здесь означает, что правка задела не ту строку.

- [ ] **Step 6: Коммит**

```bash
git add KrabEar/backend/privacy_audit.py KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py
git commit -m "$(cat <<'EOF'
feat(privacy-audit): путь журнала через KRAB_EAR_PRIVACY_AUDIT_DIR

Модульная константа _DEFAULT_LOG_PATH заменена ленивой _default_log_path().
Боевой дефолт не изменился; появилась возможность увести журнал в throwaway
для тестов и dev-инстансов. Пустое значение переменной трактуется как
незаданное — иначе опечатка увела бы compliance-журнал в CWD.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Принудительная изоляция в conftest

**Files:**
- Modify: `KrabEar/tests/conftest.py` (блок импортов после строки 39; новая autouse-фикстура)
- Test: `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py` (дописывается)

**Interfaces:**
- Consumes: `_ENV_DIR_VAR` / `_default_log_path()` из Task 1.
- Produces: `KrabEar/tests/conftest.py::_PRIVACY_AUDIT_TMPDIR: str` — каталог-однодневка на процесс; autouse-фикстура `_isolate_privacy_audit_singleton`.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py`:

```python
def test_running_test_session_is_not_on_prod_path():
    """🔴 Главный гард инцидента: в ЛЮБОМ тестовом прогоне журнал не боевой.

    Проверяем путь, а не «боевой файл не изменился» по mtime: на ubuntu-CI
    боевого файла не существует, и такой тест был бы вечно-зелёным именно там,
    где прогоняется настоящий гейт.
    """
    from backend.privacy_audit import get_privacy_audit_logger

    logger = get_privacy_audit_logger()

    assert logger._log_path != _PROD_LOG_PATH
    assert _PROD_LOG_PATH.parent not in logger._log_path.parents
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py::test_running_test_session_is_not_on_prod_path -v
```

Expected: **FAIL** — `assert PosixPath('/Users/…/KrabEar/privacy_audit.log') != PosixPath('/Users/…/KrabEar/privacy_audit.log')`. Именно это и есть инцидент: тест пишет в боевой путь.

🔴 На ubuntu-CI этот тест до правки тоже падает (там `Path.home()` другой, но переменная не выставлена и путь совпадает с `_PROD_LOG_PATH`, вычисленным от того же `Path.home()`).

🔴 **Побочный эффект самого RED-прогона.** До правки `conftest` вызов
`get_privacy_audit_logger()` внутри теста идёт в конструктор, который делает `mkdir`
боевого каталога и читает/создаёт `privacy_audit.key`. На машине владельца это
безвредно (каталог и ключ уже есть), но на чистой машине RED-шаг создаст их. Это
одноразово и исчезает сразу после Step 3 — но знать об этом надо, иначе выглядит как
загрязнение от нашей же волны.

- [ ] **Step 3: Добавить блок изоляции в conftest**

В `KrabEar/tests/conftest.py` сразу после строки 39 (`from typing import Any  # noqa: E402`) вставить:

```python
# ---------------------------------------------------------------------------
# Изоляция privacy_audit.log (инцидент 2026-08-23). Боевой compliance-журнал
# ~/Library/Application Support/KrabEar/privacy_audit.log набрал 44 907 из
# 50 041 записи тестовым мусором: PrivacyAuditLogger — синглтон с захардкоженным
# home-rooted путём, и 17 из 20 файлов, зовущих handle_purge_all_data, его не
# патчили. Реальный purge владельца стал неотличим от тестового.
#
# Правило CLAUDE.md для КАЖДОГО persistence-пути: env-переменная для базового
# пути + throwaway, принудительно выставленный в conftest ДО импорта приложения.
# Именно ПРИСВАИВАНИЕ, не setdefault: унаследованное из оболочки значение не
# должно побеждать изоляцию.
#
# Под pytest -n auto каждый xdist-воркер — отдельный процесс, импортирует
# conftest сам и получает СВОЙ mkdtemp; гонки за один файл не возникает.
# ---------------------------------------------------------------------------
import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

_PRIVACY_AUDIT_TMPDIR = tempfile.mkdtemp(prefix="krab_ear_privacy_audit_")
os.environ["KRAB_EAR_PRIVACY_AUDIT_DIR"] = _PRIVACY_AUDIT_TMPDIR
atexit.register(shutil.rmtree, _PRIVACY_AUDIT_TMPDIR, True)
```

- [ ] **Step 4: Добавить autouse-фикстуру сброса**

В `KrabEar/tests/conftest.py`, рядом с остальными autouse-фикстурами (после `_block_real_network`), добавить:

```python
@pytest.fixture(autouse=True)
def _isolate_privacy_audit_singleton() -> Iterator[None]:
    """Сбрасывает синглтон логгера и чистит throwaway-журнал после теста.

    Два эффекта связаны. Сброс не даёт тесту пронести путь и _last_hash в
    соседний. Удаление файла держит журнал пустым: иначе _read_chain_tip() в
    конструкторе стал бы O(N) по растущему за прогон файлу, и к концу сьюта
    каждое создание инстанса платило бы за десятки тысяч строк.

    Удалять файл БЕЗ сброса синглтона нельзя — _last_hash остался бы указывать
    на стёртую запись. Ключ privacy_audit.key не трогаем: он переиспользуется,
    генерировать 32 байта энтропии на каждый тест незачем.

    Модуль берётся из sys.modules, а не импортируется: если тест его не
    импортировал, создавать инстанс незачем; если тест подменил backend.* стабом,
    getattr-проверки не дадут упасть на чужом объекте.
    """
    yield
    mod = sys.modules.get("backend.privacy_audit")
    if mod is not None:
        cls = getattr(mod, "PrivacyAuditLogger", None)
        reset = getattr(cls, "reset_instance", None)
        if callable(reset):
            reset()
    Path(_PRIVACY_AUDIT_TMPDIR, "privacy_audit.log").unlink(missing_ok=True)
```

`sys`, `Path`, `pytest`, `Iterator` в conftest уже импортированы.

- [ ] **Step 5: Прогнать тест и убедиться, что он проходит**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py -v
```

Expected: **7 passed**.

- [ ] **Step 6: Доказать изоляцию на реальном purge-тесте**

Это проверка сути инцидента, а не формы. Запомнить состояние боевого файла, прогнать
purge-тесты, сверить.

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && \
P="$HOME/Library/Application Support/KrabEar/privacy_audit.log" && \
BEFORE="$(/usr/bin/stat -f '%z %m' "$P" 2>/dev/null || echo 'ОТСУТСТВУЕТ')" && \
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_purge_all_data_w1730.py KrabEar/tests/test_purge_privacy_gaps_w1767.py -q > /dev/null 2>&1; \
AFTER="$(/usr/bin/stat -f '%z %m' "$P" 2>/dev/null || echo 'ОТСУТСТВУЕТ')" && \
echo "ДО:    $BEFORE" && echo "ПОСЛЕ: $AFTER" && \
[ "$BEFORE" = "$AFTER" ] && echo "✅ боевой журнал не тронут" || echo "🔴 ПРОВАЛ: purge-тесты всё ещё пишут в бой"
```

Expected: обе строки ОДИНАКОВЫ и `✅ боевой журнал НЕ тронут`. Конкретное значение
неважно — журнал мог уже быть создан течью до деплоя волны; доказывает РАВЕНСТВО до и после.

🔴 **`/usr/bin/stat` указан абсолютным путём намеренно.** В PATH владельца `stat` — это
GNU coreutils из Homebrew, где `-f` означает «сведения о файловой системе», а не формат
вывода. С голым `stat` команда падала бы, ветка `|| echo 'ОТСУТСТВУЕТ'` срабатывала бы и
ДО, и ПОСЛЕ, строки сравнивались бы как равные — и шаг печатал бы «✅ не тронут»
НЕЗАВИСИМО от того, работает изоляция или нет. Найдено исполнителем Task 2 на живом
прогоне; ровно класс «BSD vs GNU» из CLAUDE.md, только утилита затенена прямо в PATH.
Всегда-зелёный гард — это слепота, а не защита.

🔴 Если вывод `🔴 ПРОВАЛ` — остановиться и разбираться: значит есть путь создания логгера в обход conftest, и это находка, а не повод ослабить тест.

- [ ] **Step 7: Коммит**

```bash
git add KrabEar/tests/conftest.py KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py
git commit -m "$(cat <<'EOF'
test(privacy-audit): принудительный throwaway-путь журнала в conftest

Тестовый прогон больше не пишет в боевой compliance-журнал: conftest
выставляет KRAB_EAR_PRIVACY_AUDIT_DIR присваиванием до импорта приложения,
autouse-фикстура сбрасывает синглтон и чистит throwaway-файл между тестами.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Смоки перестают писать в боевой журнал

**Files:**
- Modify: `scripts/run_e2e_smokes.command:29`, `scripts/run_e2e_bridge_smoke.command:23`, `scripts/e2e_owner_gate_smoke.py:102-103`
- Test: `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py` (дописывается)

**Interfaces:**
- Consumes: имя переменной `KRAB_EAR_PRIVACY_AUDIT_DIR` из Task 1.
- Produces: ничего для последующих задач.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py`:

```python
# Смоки поднимают throwaway-backend и обещают в своих шапках «never touches your
# prod data» — но privacy-события писали в боевой журнал. Source-контракт: без
# него правка скрипта тихо вернёт загрязнение.
#
# 🔴 Список получен grep'ом по '"--data-dir"' и '--data-dir' в scripts/, а не
# на глаз: первая версия волны знала только о трёх скриптах и пропустила
# четыре. НЕ включены сознательно: e2e_meeting_smoke.py (спавн только в
# докстринге, скрипт сам backend не поднимает), build_bundled_runtime.command
# (строка 155 — это log с ПОДСКАЗКОЙ команды, не запуск), memory_baseline.py и
# history_health_report.py (подключаются к существующему инстансу, не спавнят),
# validate_c1_mps_fix.command и observe_production.command (целятся в ПРОД
# намеренно — им боевой журнал и нужен).
_SMOKE_SCRIPTS = (
    "scripts/run_e2e_smokes.command",
    "scripts/run_e2e_bridge_smoke.command",
    "scripts/e2e_owner_gate_smoke.py",
    "scripts/e2e_rescue_smoke.py",
    "scripts/e2e_recommended_setup_smoke.py",
    "scripts/rest_inprocess_load_smoke.py",
    "scripts/s3_gpu_contention_smoke.py",
)

# Проверяем ПРИСВАИВАНИЕ, а не вхождение имени переменной:
# (1) голое "KRAB_EAR_PRIVACY_AUDIT_DIR" вечно-зеленится комментарием,
# (2) присваивание ПУСТОГО значения прошло бы проверку, а fail-safe из C1 молча
#     увёл бы журнал обратно в бой — слабый ассерт маскировал бы ровно тот отказ,
#     ради которого тест написан.
#
# Python-скрипты разбираем AST (правило CLAUDE.md для source-inspection тестов:
# матчить конструкцию, а не подстроку). Для .command AST неприменим — bash;
# там требуем точную строку с непустым значением "$DATADIR".
_ENV_VAR = "KRAB_EAR_PRIVACY_AUDIT_DIR"
_BASH_ASSIGNMENT = f'export {_ENV_VAR}="$DATADIR"'


def _python_assigns_env_var(source: str) -> bool:
    """True, если в AST есть `<что-то>["KRAB_EAR_PRIVACY_AUDIT_DIR"] = <непустое>`."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            key = target.slice
            if not (isinstance(key, ast.Constant) and key.value == _ENV_VAR):
                continue
            value = node.value
            # Литеральная пустая/пробельная строка — не изоляция, а её видимость.
            if isinstance(value, ast.Constant) and (
                not isinstance(value.value, str) or not value.value.strip()
            ):
                return False
            return True
    return False


@pytest.mark.parametrize("rel_path", _SMOKE_SCRIPTS)
def test_e2e_smoke_scripts_export_privacy_audit_dir(rel_path):
    """Каждый смок с throwaway data-dir обязан увести и privacy-журнал."""
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")

    if rel_path.endswith(".py"):
        assigned = _python_assigns_env_var(source)
        expected = f'env["{_ENV_VAR}"] = str(data_dir)'
    else:
        assigned = _BASH_ASSIGNMENT in source
        expected = _BASH_ASSIGNMENT

    assert assigned, (
        f"{rel_path} поднимает backend на throwaway data-dir, но privacy-события "
        "уйдут в боевой ~/Library/Application Support/KrabEar/privacy_audit.log. "
        f"Ожидается присваивание вида: {expected}"
    )
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py -k smoke_scripts -v
```

Expected: **7 failed** — по одному на каждый скрипт, с текстом ассерта про боевой журнал.

- [ ] **Step 3: Правка `scripts/run_e2e_smokes.command`**

После строки 29 (`LOG="$DATADIR/backend.log"`) добавить:

```bash
# Privacy-журнал тоже уводим в throwaway: логгер home-rooted по умолчанию и
# иначе пишет в боевой compliance-файл вопреки обещанию шапки скрипта.
export KRAB_EAR_PRIVACY_AUDIT_DIR="$DATADIR"
```

- [ ] **Step 4: Правка `scripts/run_e2e_bridge_smoke.command`**

После строки 23 (`SOCK="$DATADIR/krabear.sock"`) добавить:

```bash
# Privacy-журнал тоже уводим в throwaway: логгер home-rooted по умолчанию и
# иначе пишет в боевой compliance-файл вопреки обещанию шапки скрипта.
export KRAB_EAR_PRIVACY_AUDIT_DIR="$DATADIR"
```

- [ ] **Step 5: Правка `scripts/e2e_owner_gate_smoke.py`**

В функции `_spawn_backend` после строки `env["PYTHONPATH"] = str(KRAB_EAR)` добавить:

```python
    # Privacy-журнал тоже уводим в throwaway: логгер home-rooted по умолчанию и
    # иначе пишет в боевой compliance-файл вопреки обещанию шапки скрипта.
    env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = str(data_dir)
```

- [ ] **Step 6: Правка четырёх остальных Python-смоков**

🔴 Найдены ревью-гейтом: первая версия волны знала о трёх скриптах, а спавнеров семь.
В каждом — та же одна строка в функцию, поднимающую backend, сразу после
`env["PYTHONPATH"] = ...` (или перед `subprocess.Popen`, если `env` собирается иначе):

```python
    # Privacy-журнал тоже уводим в throwaway: логгер home-rooted по умолчанию и
    # иначе пишет в боевой compliance-файл вопреки обещанию шапки скрипта.
    env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = str(data_dir)
```

| Файл | Функция / место |
|---|---|
| `scripts/e2e_rescue_smoke.py` | `_spawn_backend`, спавн на строке 130 (backend поднимается ДВАЖДЫ — одной правки `env` хватает на обе жизни) |
| `scripts/e2e_recommended_setup_smoke.py` | спавн на строке 98 |
| `scripts/rest_inprocess_load_smoke.py` | спавн на строке 221 |
| `scripts/s3_gpu_contention_smoke.py` | спавн на строке 144 |

🔴 В каждом файле сверить, что переменная с throwaway-каталогом называется именно
`data_dir`; если имя другое — подставить фактическое, но НЕ хардкодить путь.

- [ ] **Step 7: Прогнать тест и убедиться, что он проходит**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py -v
```

Expected: **14 passed** (7 тестов волны + 7 параметров source-контракта).

- [ ] **Step 8: Проверить bash-совместимость правок**

`.command`-скрипты исполняются на Bash 3.2 (macOS), где нет `mapfile`/`declare -A`.
`export VAR="$DATADIR"` — базовый синтаксис, но синтаксис всё равно проверить явно.

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && bash -n scripts/run_e2e_smokes.command && bash -n scripts/run_e2e_bridge_smoke.command && echo "✅ синтаксис обоих скриптов валиден"
```

Expected: `✅ синтаксис обоих скриптов валиден`.

- [ ] **Step 9: Коммит**

```bash
git add scripts/run_e2e_smokes.command scripts/run_e2e_bridge_smoke.command \
        scripts/e2e_owner_gate_smoke.py scripts/e2e_rescue_smoke.py \
        scripts/e2e_recommended_setup_smoke.py scripts/rest_inprocess_load_smoke.py \
        scripts/s3_gpu_contention_smoke.py \
        KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py
git commit -m "$(cat <<'EOF'
fix(smoke): e2e-смоки уводят privacy-журнал в throwaway data-dir

Все семь смоков поднимают backend на mktemp-каталоге и обещают не трогать
боевые данные, но privacy-события шли в home-rooted compliance-журнал.
Source-контракт-тест держит обещание при будущих правках скриптов.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Дашборд — один потоковый проход вместо двух чтений

**Files:**
- Modify: `KrabEar/backend/privacy_audit.py` (новый метод `summarize()` в конце класса, после `clear()`)
- Modify: `KrabEar/backend/service.py:4482-4497` (секция `audit` в `_handle_get_privacy_dashboard`)
- Test: `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py` (дописывается)

**Interfaces:**
- Consumes: `_default_log_path()` из Task 1.
- Produces: `PrivacyAuditLogger.summarize() -> dict[str, Any]` с ключами ровно `{"total": int, "last_ts": str | None, "by_type": dict[str, int]}`.

🔴 **Контракт-парити.** Старый код брал `total_events` из `total_count()`, который считает
все непустые строки, ВКЛЮЧАЯ неразбираемые, а `by_type` — из `read_entries()`, который
неразбираемые пропускает. `summarize()` обязан воспроизвести обе семантики: счётчик
инкрементируется до `json.loads`, агрегаты — после.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py`:

```python
def test_summarize_matches_legacy_two_pass_result(tmp_path, monkeypatch):
    """summarize() воспроизводит ровно то, что считал прежний двойной проход."""
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", str(tmp_path))
    audit_logger = PrivacyAuditLogger()
    audit_logger.log_event("privacy", "purge_all_data")
    audit_logger.log_event("sentry", "blocked")
    audit_logger.log_event("privacy", "purge_all_data")

    legacy_total = audit_logger.total_count()
    legacy_entries = audit_logger.read_entries(limit=max(legacy_total, 1))
    legacy_by_type: dict[str, int] = {}
    legacy_last_ts = None
    for entry in legacy_entries:
        action = str(entry.get("action", "unknown"))
        legacy_by_type[action] = legacy_by_type.get(action, 0) + 1
        ts = entry.get("ts")
        if ts and (legacy_last_ts is None or ts > legacy_last_ts):
            legacy_last_ts = ts

    summary = audit_logger.summarize()

    assert summary["total"] == legacy_total == 3
    assert summary["by_type"] == legacy_by_type == {"purge_all_data": 2, "blocked": 1}
    assert summary["last_ts"] == legacy_last_ts


def test_summarize_counts_unparseable_line_like_total_count(tmp_path, monkeypatch):
    """Битая строка считается в total, но не попадает в by_type.

    Ровно так вела себя пара total_count() + read_entries(): счётчик берёт все
    непустые строки, агрегаты — только разобранные. Расхождение здесь дало бы
    дашборду total_events, не равный сумме by_type.
    """
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", str(tmp_path))
    audit_logger = PrivacyAuditLogger()
    audit_logger.log_event("privacy", "purge_all_data")
    with (tmp_path / "privacy_audit.log").open("a", encoding="utf-8") as fh:
        fh.write("{битая строка\n")

    summary = audit_logger.summarize()

    assert summary["total"] == 2
    assert summary["by_type"] == {"purge_all_data": 1}


def test_summarize_on_missing_log_is_empty(tmp_path, monkeypatch):
    """Отсутствующий журнал — не ошибка: после архивации это норма."""
    monkeypatch.setenv("KRAB_EAR_PRIVACY_AUDIT_DIR", str(tmp_path))
    audit_logger = PrivacyAuditLogger()

    assert audit_logger.summarize() == {"total": 0, "last_ts": None, "by_type": {}}
```

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py -k summarize -v
```

Expected: **3 failed** — `AttributeError: 'PrivacyAuditLogger' object has no attribute 'summarize'`.

- [ ] **Step 3: Реализовать `summarize()`**

В `KrabEar/backend/privacy_audit.py` добавить метод в конец класса `PrivacyAuditLogger`,
сразу после `clear()` и ДО модульной функции `get_privacy_audit_logger`:

```python
    def summarize(self) -> dict[str, Any]:
        """Агрегаты журнала за ОДИН потоковый проход.

        Заменяет в get_privacy_dashboard пару total_count() +
        read_entries(limit=total_events): та читала файл дважды и
        материализовала весь журнал в список ради трёх чисел. На боевом
        50k-журнале это стоило ~0.2 с и росло без потолка.

        Семантика счётчиков сохранена побитово: total инкрементируется до
        разбора строки (как total_count, который считает и битые строки),
        by_type/last_ts наполняются после (как read_entries, который битые
        пропускает). Иначе total_events в дашборде разошёлся бы с суммой by_type.

        Returns:
            {"total": int, "last_ts": str | None, "by_type": dict[str, int]}
        """
        summary: dict[str, Any] = {"total": 0, "last_ts": None, "by_type": {}}
        if not self._log_path.exists():
            return summary

        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    for raw_line in fh:
                        line = raw_line.strip()
                        if not line:
                            continue
                        summary["total"] += 1
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        action = str(entry.get("action", "unknown"))
                        summary["by_type"][action] = summary["by_type"].get(action, 0) + 1
                        ts = entry.get("ts")
                        if ts and (summary["last_ts"] is None or ts > summary["last_ts"]):
                            summary["last_ts"] = ts
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка агрегации журнала")

        return summary
```

- [ ] **Step 4: Перевести дашборд на `summarize()`**

В `KrabEar/backend/service.py` заменить блок в `_handle_get_privacy_dashboard`
(строки 4483-4497, начиная с `audit = get_privacy_audit_logger()`):

```python
            audit = get_privacy_audit_logger()
            total_events = audit.total_count()
            all_entries = audit.read_entries(limit=max(total_events, 1))
            last_event_ts: str | None = None
            by_type: dict[str, int] = {}
            for entry in all_entries:
                action = str(entry.get("action", "unknown"))
                by_type[action] = by_type.get(action, 0) + 1
                ts = entry.get("ts")
                if ts and (last_event_ts is None or ts > last_event_ts):
                    last_event_ts = ts
            result["audit"] = {
                "total_events": total_events,
                "last_event_ts": last_event_ts,
                "by_type": by_type,
            }
```

на:

```python
            audit_summary = get_privacy_audit_logger().summarize()
            result["audit"] = {
                "total_events": audit_summary["total"],
                "last_event_ts": audit_summary["last_ts"],
                "by_type": audit_summary["by_type"],
            }
```

Ключи ответа (`total_events`, `last_event_ts`, `by_type`) не меняются — Swift-сторона
читает именно их.

- [ ] **Step 5: 🔴 Перенацелить тест graceful degradation — иначе он станет декоративным**

Найдено ревью-гейтом. `KrabEar/tests/test_privacy_dashboard.py:465-487`
(`test_audit_failure_returns_defaults`) ломает `total_count`:

```python
        broken_audit.total_count = boom_total  # type: ignore[method-assign]
```

После Task 4 дашборд `total_count()` **не вызывает** — он зовёт `summarize()`. А
`summarize()` на несуществующем `broken_audit.log` штатно вернёт `0 / None / {}` —
ровно те значения, которые тест ждёт от ветки обработки ошибки. Тест останется
**ЗЕЛЁНЫМ и перестанет проверять хоть что-нибудь**: это класс «test-validates-the-hole»,
против которого в репозитории есть именной CI-гард (`audit_dispatch_test_targets.py`).

🔴 Предупреждение «если тест упал — разбираться» тут не спасёт: падения НЕ будет.

Заменить строку симуляции отказа на:

```python
        def boom_summarize(*a, **kw):
            raise OSError("simulated audit failure")

        broken_audit.summarize = boom_summarize  # type: ignore[method-assign]
```

и переименовать `boom_total` → `boom_summarize` в определении выше. Ассерты дефолтов
(`total_events == 0`, `last_event_ts is None`, `by_type == {}`) оставить как есть — они
и есть контракт graceful degradation.

- [ ] **Step 6: Доказать, что перенацеленный тест реально ловит отказ**

Временно убрать `try/except` вокруг audit-секции в `_handle_get_privacy_dashboard` и
убедиться, что тест КРАСНЕЕТ; вернуть `try/except` — зеленеет. Без этой проверки нет
доказательства, что тест снова живой.

Run (после возврата `try/except`):
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_dashboard.py::PrivacyDashboardTestCase::test_audit_failure_returns_defaults -v
```

Expected: PASS.

- [ ] **Step 7: Прогнать тесты и убедиться, что проходят**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py KrabEar/tests/test_privacy_dashboard.py KrabEar/tests/test_privacy_dashboard_ok_contract_2026_08_23.py -v
```

Expected: PASS; в файле волны — **17 passed**.

🔴 Если `test_privacy_dashboard.py` упал на `total_events` — разбираться как с настоящей
находкой (изоляция изменила счётчик, и тест мог зависеть от накопленного боевого
журнала), а НЕ подгонять ассерт под новый результат.

- [ ] **Step 6: Коммит**

```bash
git add KrabEar/backend/privacy_audit.py KrabEar/backend/service.py KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py
git commit -m "$(cat <<'EOF'
perf(privacy-dashboard): агрегаты журнала за один проход

get_privacy_dashboard читал журнал дважды (total_count + read_entries) и
материализовал его целиком в список ради трёх чисел. Новый summarize()
делает это потоково, сохраняя обе прежние семантики счёта: битая строка
считается в total, но не попадает в by_type.

Не является фиксом скорости дашборда: замер на боевом 50k-журнале дал
~0.2 с из 2.0 с. Основной источник задержки — get_storage_info и
get_history_stats, отдельная волна.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Полный гейт и PR

**Files:**
- Modify: `docs/NOW.md` (запись о волне)

**Interfaces:**
- Consumes: всё из Task 1-4.
- Produces: PR в `codex/krab-ear-v2`.

- [ ] **Step 1: Прогнать зависящие тестовые файлы**

Правило репозитория: меняешь SOURCE — гоняй ЗАВИСЯЩИЕ тесты, а не только свои.

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py \
  KrabEar/tests/test_privacy_audit.py \
  KrabEar/tests/test_privacy_audit_hash_chain.py \
  KrabEar/tests/test_privacy_audit_clear.py \
  KrabEar/tests/test_privacy_audit_clear_race_W1768.py \
  KrabEar/tests/test_privacy_audit_log_race_W1029.py \
  KrabEar/tests/test_privacy_dashboard.py \
  KrabEar/tests/test_privacy_dashboard_ok_contract_2026_08_23.py \
  KrabEar/tests/test_purge_all_data_w1730.py \
  KrabEar/tests/test_purge_privacy_gaps_w1767.py \
  -p no:cacheprovider -q
```

Expected: все PASS.

- [ ] **Step 2: ubuntu-parity**

Локальный venv — Python 3.14 С mlx; ubuntu-CI — 3.12 БЕЗ mlx. Зелёный локально ничего не доказывает.

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && scripts/pre_merge_py312_check.sh KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py
```

Expected: PASS.

- [ ] **Step 3: Прогнать чанк с privacy-тестами в ОДНОМ процессе**

ubuntu-parity изолирует каждый файл и потому НЕ воспроизводит загрязнение состояния
между файлами в чанке — хронический класс багов репозитория. Autouse-фикстура из Task 2
трогает глобальный синглтон, поэтому чанк обязателен.

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_privacy_*.py KrabEar/tests/test_purge_*.py -p no:cacheprovider -q
```

Expected: все PASS, процесс завершается кодом 0 (не 1 от фатального stderr-lock при выходе).

- [ ] **Step 4: CI-гарды**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && make audit-all
```

Expected: все гарды зелёные.

🔴 Отдельно проконтролировать `audit-purge-coverage`: боевой дефолт не менялся, поэтому
сканер НЕ должен обнаружить новый file-backed store. Обнаружил — значит резолв пути утёк
под `data_dir`; чинить реализацию, а не добавлять строку в allowlist.

- [ ] **Step 5: Линтер**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && python -m flake8 KrabEar/backend/privacy_audit.py KrabEar/tests/conftest.py KrabEar/tests/test_privacy_audit_path_isolation_2026_08_23.py scripts/e2e_owner_gate_smoke.py
```

Expected: без замечаний. W293 в тестах НЕ расслаблен — пустые строки не должны содержать пробелов.

- [ ] **Step 6: Запись в `docs/NOW.md`**

Добавить в актуальный раздел:

```markdown
- 🟢 Изоляция privacy_audit.log от тестов: `KRAB_EAR_PRIVACY_AUDIT_DIR` + принудительный
  throwaway в conftest + три e2e-смока. Боевой журнал 22 МБ / 50 041 запись (90% —
  тестовый мусор) заархивирован отдельной сессией; фикс HMAC-цепочки — смежная волна.
  Дашборд переведён на одно-проходный `summarize()`.
```

- [ ] **Step 7: Открыть PR**

```bash
cd "$(git rev-parse --show-toplevel)" && \
git add docs/NOW.md && git commit -m "docs(now): волна изоляции privacy_audit.log

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" && \
git push -u origin claude/agitated-ritchie-5b33f1 && \
gh pr create --base codex/krab-ear-v2 --title "fix(privacy-audit): изоляция журнала от тестов и dev-инстансов" --body "$(cat <<'EOF'
## Что чинит

Боевой compliance-журнал `~/Library/Application Support/KrabEar/privacy_audit.log` на 90% состоял из тестового мусора: **44 907 из 50 041** записи — `privacy/purge_all_data` из тестов. Реальный purge владельца стал неотличим от тестового, то есть compliance-функция журнала не выполнялась.

Данные владельца целы — 12 746 записей истории на месте, реального удаления не было.

## Корень

`privacy_audit.py:28` — путь захардкожен модульной константой мимо env И мимо `data_dir`, а `PrivacyAuditLogger` синглтон. Два потребителя дыры: **17 из 20** файлов, зовущих `handle_purge_all_data`, логгер не патчили; плюс все три e2e-смока поднимают throwaway-backend, но privacy-события шли в боевой файл вопреки обещанию их шапок.

## Что сделано

- `_default_log_path()` вместо константы: `KRAB_EAR_PRIVACY_AUDIT_DIR` → каталог журнала, боевой дефолт не изменился. Ленивый резолв, а не чтение env в константу — иначе изоляция зависела бы от порядка импортов.
- `conftest.py`: throwaway выставляется **присваиванием** (не `setdefault`) до импорта приложения + autouse-сброс синглтона между тестами.
- Три e2e-смока экспортируют переменную рядом со своим `--data-dir`, source-контракт-тест держит это при будущих правках.
- Дашборд: `summarize()` за один проход вместо `total_count()` + `read_entries(limit=total)`.

## Что НЕ входит

- Фикс HMAC-цепочки (она разорвана с 2026-06-03, структурно: журнал пишут несколько процессов) и права `600` — смежная волна.
- Ускорение `get_storage_info`/`get_history_stats` — настоящий источник 1.44 с, отдельная волна.

## Деплой

🔴 `scripts/safe_backend_restart.command --with-rest` — REST второй писатель журнала и после архивации не перезапускался.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**1. Покрытие спеки.**

| Раздел спеки | Задача |
|---|---|
| C1 — `_default_log_path()` | Task 1 |
| C2 — изоляция в conftest | Task 2 |
| C3 — smoke-скрипты | Task 3 |
| C4 — `summarize()` | Task 4 |
| §3 тесты 1-4 | Task 1 Step 1 |
| §3 тест 5 | Task 2 Step 1 |
| §3 тест 6 | Task 3 Step 1 |
| §6 гейты и деплой | Task 5 |
| §4 граница с параллельной сессией | Global Constraints |
| §5 ограничения (unittest, ретроспектива, окно до деплоя) | документированы в спеке, кода не требуют |

Гэпов нет. Сверх спеки добавлены два шага, которых она не называла явно, но которые
вытекают из правил репозитория: Task 2 Step 6 (эмпирическое доказательство изоляции на
реальном purge-тесте — «зелёный тест ≠ работает») и Task 5 Step 3 (чанк в одном процессе —
ubuntu-parity изолирует файлы и не воспроизводит загрязнение состояния).

**2. Заглушки.** Не найдено: каждый шаг с кодом содержит полный текст правки, каждая
команда — точная, с ожидаемым выводом.

**3. Согласованность типов.** `summarize()` объявлен в Task 4 Interfaces как
`dict[str, Any]` с ключами `total` / `last_ts` / `by_type`; ровно эти ключи использованы
в реализации (Task 4 Step 3), в правке `service.py` (Step 4) и в трёх тестах (Step 1).
`_default_log_path()` объявлен в Task 1 Interfaces и используется в Task 1 Step 3 и
косвенно в Task 4. Имена env-переменной (`KRAB_EAR_PRIVACY_AUDIT_DIR`) и файла
(`privacy_audit.log`) совпадают во всех пяти задачах и в спеке.
