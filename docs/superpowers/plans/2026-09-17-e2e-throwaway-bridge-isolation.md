# Изоляция одноразовых e2e-backend'ов от прод-REST — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** одноразовый backend из e2e-скриптов не шлёт события в боевой REST `:5005`.

**Architecture:** e2e-скрипты поднимают `KrabEar/main.py --data-dir <tmp>`. Его `EventBridge` включён по умолчанию и целит в `127.0.0.1:{REST_SERVER_PORT}` (5005 — боевой REST), подписывая события токеном из `<tmp>`. Боевой REST отвечает 401 и пишет `event_bridge: неверный bridge-токен` пачками (16.09: 26 строк в 00:29, 24 строки в 09:50 — ровно во время e2e-прогонов). Фикс — тот же приём, которым уже изолирован privacy-журнал (`KRAB_EAR_PRIVACY_AUDIT_DIR`): явный env-выключатель `KRAB_EAR_EVENT_BRIDGE_ENABLED=0` в обоих запускателях + контракт-тест, который не даст выключателю потеряться.

**Tech Stack:** bash, Python `ast`/`unittest` (уже в репо). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/e2e-bridge-isolation`, ветка `fix/e2e-bridge-isolation`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не менять `backend/event_bridge.py` и `core/config.py`** — см. «Почему не продуктовый гард».

---

## Проверенные факты (координатор, 17.09)

- `scripts/run_e2e_smokes.command:33` — `export KRAB_EAR_PRIVACY_AUDIT_DIR="$DATADIR"`; `:46` — запуск backend'а. Выключателя моста нет.
- `scripts/e2e_recommended_setup_smoke.py:89-93` — `env = dict(os.environ)` + `env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = ...`; `:100-103` — `subprocess.Popen([... "KrabEar/main.py", "--data-dir", ...], env=env)`. Выключателя нет.
- `scripts/run_e2e_bridge_smoke.command` **не трогать**: он тестирует сам мост на случайном порту (`:30`), выключатель там сломал бы смысл теста.
- `backend/event_bridge.py:228` читает `settings.EVENT_BRIDGE_ENABLED` один раз при старте; `start()` при `False` пишет `EventBridge отключён (EVENT_BRIDGE_ENABLED=False)` (`:298`).
- `core/config.py:778-784`: env-переменная `KRAB_EAR_<KEY>` **сильнее** `settings.json`; в прод-`settings.json` ключа `event_bridge_enabled` нет. Значит, выключатель сработает.
- Документационные примеры ручного запуска: `scripts/e2e_meeting_smoke.py:5`, `scripts/e2e_ipc_smoke.py:8`.

### Почему не продуктовый гард

Вариант «мост сам выключается, если `--data-dir` не дефолтный» отвергнут: dev-режим (`~/.krab_ear_data` + dev-REST) — законная пара с недефолтным каталогом. Гард в продукте сломал бы доставку событий в dev. Причина шума — запускатель, который не изолирует окружение. Там и чиним.

---

### Task 1: Контракт-тест (RED)

**Files:**
- Create: `KrabEar/tests/test_e2e_throwaway_isolation_contract.py`

- [ ] **Step 1: Написать тест**

```python
"""Одноразовые e2e-backend'ы обязаны быть изолированы от боевых ресурсов.

Инцидент 2026-09-16: `run_e2e_smokes.command` поднимал backend без
выключателя EventBridge, мост целил в боевой REST :5005 с токеном из
временного каталога, и прод-лог REST ловил пачки
`event_bridge: неверный bridge-токен` на каждом e2e-прогоне. Тот же класс,
что уже закрытая утечка privacy-журнала (KRAB_EAR_PRIVACY_AUDIT_DIR):
запускатель обязан выставить КАЖДУЮ изолирующую переменную ДО старта процесса.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHELL_LAUNCHER = REPO / "scripts" / "run_e2e_smokes.command"
PY_LAUNCHER = REPO / "scripts" / "e2e_recommended_setup_smoke.py"

# переменная -> допустимые значения (None = любое непустое)
ISOLATION_ENV: dict[str, set[str] | None] = {
    "KRAB_EAR_PRIVACY_AUDIT_DIR": None,
    "KRAB_EAR_EVENT_BRIDGE_ENABLED": {"0", "false", "False"},
}


def shell_exports_before_spawn(text: str) -> dict[str, str]:
    """Значения `export VAR=...`, объявленных строкой РАНЬШЕ запуска main.py."""
    spawn = re.search(r"^.*KrabEar/main\.py --data-dir.*$", text, re.MULTILINE)
    if spawn is None:
        raise AssertionError("в shell-запускателе не найден запуск KrabEar/main.py")
    head = text[: spawn.start()]
    found: dict[str, str] = {}
    for m in re.finditer(r'^\s*export\s+([A-Z_]+)="?([^"\n]*)"?\s*$', head, re.MULTILINE):
        found[m.group(1)] = m.group(2)
    return found


def python_env_before_popen(source: str) -> dict[str, str]:
    """`env["VAR"] = <значение>` в функции, строками РАНЬШЕ subprocess.Popen."""
    tree = ast.parse(source)
    popen_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    if not popen_lines:
        raise AssertionError("в Python-запускателе не найден subprocess.Popen")
    first_popen = min(popen_lines)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or node.lineno >= first_popen:
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "env"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                value = node.value.value if isinstance(node.value, ast.Constant) else "<expr>"
                found[target.slice.value] = str(value)
    return found


def missing_isolation(found: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for name, allowed in ISOLATION_ENV.items():
        if name not in found:
            problems.append(f"{name}: не выставлена до старта backend")
        elif allowed is not None and found[name] not in allowed:
            problems.append(f"{name}={found[name]!r}: ожидалось одно из {sorted(allowed)}")
    return problems


class ThrowawayLauncherIsolationTests(unittest.TestCase):
    def test_shell_launcher_isolates_backend(self) -> None:
        found = shell_exports_before_spawn(SHELL_LAUNCHER.read_text(encoding="utf-8"))
        self.assertEqual(missing_isolation(found), [])

    def test_python_launcher_isolates_backend(self) -> None:
        found = python_env_before_popen(PY_LAUNCHER.read_text(encoding="utf-8"))
        self.assertEqual(missing_isolation(found), [])


class DetectorSelfTest(unittest.TestCase):
    """Гард, тихо переставший находить нарушение, отчитывался бы зелёным вечно."""

    def test_shell_detector_flags_export_after_spawn(self) -> None:
        text = (
            'export KRAB_EAR_PRIVACY_AUDIT_DIR="$D"\n'
            'PYTHONPATH=x "$PY" KrabEar/main.py --data-dir "$D" &\n'
            "export KRAB_EAR_EVENT_BRIDGE_ENABLED=0\n"
        )
        self.assertEqual(
            missing_isolation(shell_exports_before_spawn(text)),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED: не выставлена до старта backend"],
        )

    def test_python_detector_flags_wrong_value(self) -> None:
        source = (
            "import subprocess\n"
            "def main():\n"
            "    env = {}\n"
            '    env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = "/tmp/x"\n'
            '    env["KRAB_EAR_EVENT_BRIDGE_ENABLED"] = "1"\n'
            '    subprocess.Popen(["python"], env=env)\n'
        )
        self.assertEqual(
            missing_isolation(python_env_before_popen(source)),
            ["KRAB_EAR_EVENT_BRIDGE_ENABLED='1': ожидалось одно из ['0', 'False', 'false']"],
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Прогнать — ждём RED ровно по мосту**

```bash
PYTHONPATH=$(pwd)/KrabEar "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m pytest KrabEar/tests/test_e2e_throwaway_isolation_contract.py -v -p no:cacheprovider
```

Ожидаемо: `DetectorSelfTest` — 2 passed; `test_shell_launcher_isolates_backend` и `test_python_launcher_isolates_backend` — FAILED с `KRAB_EAR_EVENT_BRIDGE_ENABLED: не выставлена до старта backend`. Если упали по другой причине (парсер не нашёл запуск) — **стоп**, доложить координатору.

### Task 2: Фикс запускателей (GREEN)

**Files:**
- Modify: `scripts/run_e2e_smokes.command` (после строки 33)
- Modify: `scripts/e2e_recommended_setup_smoke.py` (после строки 93)
- Modify: `scripts/e2e_meeting_smoke.py:5`, `scripts/e2e_ipc_smoke.py:8` (только docstring)

- [ ] **Step 1: shell-запускатель** — сразу после `export KRAB_EAR_PRIVACY_AUDIT_DIR="$DATADIR"`:

```bash
# EventBridge одноразового backend'а целит в REST :5005 — это БОЕВОЙ REST,
# а токен лежит во временном каталоге: прод-лог ловит пачки 401
# «неверный bridge-токен» (инцидент 16.09). Мост тестирует отдельный
# run_e2e_bridge_smoke.command на своём порту — здесь он не нужен.
export KRAB_EAR_EVENT_BRIDGE_ENABLED=0
```

- [ ] **Step 2: Python-запускатель** — сразу после `env["KRAB_EAR_PRIVACY_AUDIT_DIR"] = str(data_dir)`:

```python
    # EventBridge одноразового backend'а иначе стучится в БОЕВОЙ REST :5005
    # с токеном из временного каталога (пачки 401 в прод-логе, инцидент 16.09).
    env["KRAB_EAR_EVENT_BRIDGE_ENABLED"] = "0"
```

- [ ] **Step 3: docstring-примеры ручного запуска**

`scripts/e2e_meeting_smoke.py:5` →
```
  KRAB_EAR_EVENT_BRIDGE_ENABLED=0 KRAB_EAR_PRIVACY_AUDIT_DIR=/tmp/krab_ear_meeting_e2e python KrabEar/main.py --data-dir /tmp/krab_ear_meeting_e2e &   # throwaway
```
`scripts/e2e_ipc_smoke.py:8` → заменить `` `python KrabEar/main.py --data-dir <dir>` `` на
`` `KRAB_EAR_EVENT_BRIDGE_ENABLED=0 KRAB_EAR_PRIVACY_AUDIT_DIR=<dir> python KrabEar/main.py --data-dir <dir>` ``.

- [ ] **Step 4: GREEN** — та же команда, что в Task 1 Step 2. Ожидаемо: `4 passed`.

- [ ] **Step 5: Гейт**

```bash
bash -n scripts/run_e2e_smokes.command
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m flake8 --max-line-length=120 KrabEar/tests/test_e2e_throwaway_isolation_contract.py scripts/e2e_recommended_setup_smoke.py
scripts/pre_merge_py312_check.sh KrabEar/tests/test_e2e_throwaway_isolation_contract.py
```

Ожидаемо: без вывода ошибок; ubuntu-parity — PASS.

### Task 3: Живое доказательство (обязательно)

e2e-скрипт берёт venv из корня репо, которого в worktree нет. Временная символическая ссылка (не коммитить: `.venv_krab_ear` в worktree показывается как untracked):

- [ ] **Step 1: До/после по боевому логу**

```bash
ln -s "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear" .venv_krab_ear
LOG="/Users/pablito/Antigravity_AGENTS/Krab Ear/logs/krab-ear-rest.err.log"
BEFORE=$(grep -a -c 'неверный bridge-токен' "$LOG")
scripts/run_e2e_smokes.command
AFTER=$(grep -a -c 'неверный bridge-токен' "$LOG")
echo "401 до=$BEFORE после=$AFTER"
rm .venv_krab_ear
```

Ожидаемо: `после == до`, e2e — PASS. Скрипт не трогает прод-backend и агента: он поднимает свой backend во временном каталоге.

- [ ] **Step 2 (контроль, что выключатель реально сработал):** во время прогона лог одноразового backend'а (`$DATADIR/backend.log`, путь печатается в начале прогона) содержит `EventBridge отключён (EVENT_BRIDGE_ENABLED=False)`. Если строки нет — фикс не сработал, даже если счётчик совпал (прогон мог не дойти до эмита событий).

### Task 4: Коммит и PR

- [ ] `git branch --show-current` → `fix/e2e-bridge-isolation`
- [ ] `git add` **явными путями**: 5 файлов из Task 1–2.
- [ ] Коммит: `fix(e2e): одноразовый backend не шлёт события в боевой REST`
- [ ] PR в `codex/krab-ear-v2`; в описании — цифры «401 до/после» из Task 3.

## Definition of Done

- 4 теста зелёные; `DetectorSelfTest` доказывает, что гард ловит нарушения.
- Живой прогон: число строк 401 в боевом логе REST не растёт; в логе одноразового backend'а есть строка про отключённый мост.
- Прод-backend, REST и агент не перезапускались.

## Вне scope (записать в отчёт, не чинить)

Одноразовый backend всё ещё **читает** владельческий `~/Library/Application Support/KrabEar/settings.json` (`core/config.py:33`) и делит с продом межпроцессный замок MLX (`core/mlx_inter_lock.py:51` — это правильно: сериализация GPU) и файл brain lease `~/.openclaw/lm_studio_brain.lock`. Если e2e стартует запись, lease на 30 с может помешать Крабу. Отметить наблюдения в отчёте; решение — отдельной карточкой координатора.
