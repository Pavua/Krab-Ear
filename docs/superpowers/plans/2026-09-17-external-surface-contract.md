# Контракт внешних интерфейсов Krab Ear — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** имена и ключи ответов Ear, которые зовут Краб и Voice Gateway, записаны в одном документе и защищены тестом от тихого переименования.

**Architecture:** документ `docs/contracts/ear-external-surface.md` — таблица «кто зовёт → что → какие ключи читает». Тест `KrabEar/tests/test_external_surface_contract.py` разбирает исходники через AST (без импорта backend и ML-зависимостей) и проверяет: IPC-методы есть в `_build_dispatch_table`, REST-маршруты зарегистрированы с нужным префиксом blueprint и HTTP-методом, ключи, которые читают потребители, присутствуют в словарях-ответах. Второй тест сверяет, что каждое имя из теста упомянуто в документе. Зачем: 17.09 сверка стыка нашла у Краба вызов несуществующего IPC `get_history` и проверку здоровья на порту, который никто не слушает (`:18000`). Обе поломки жили молча, потому что гарды Ear видят только свой репо.

**Tech Stack:** Python `ast`, `unittest`. Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/external-surface-contract`, ветка `feat/external-surface-contract`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не править репозитории Краба и VG** (только читать); не менять хендлеры Ear — карточка только фиксирует существующий контракт.

---

## Проверенные факты (координатор, 17.09, file:line)

| Потребитель (файл) | Вызов Ear | Что читает из ответа |
|---|---|---|
| Краб `src/integrations/krab_ear_client.py:187-189`, `src/core/krab_ear_health_probe.py:236-238` | IPC `ping` | `ok`, `result.status` ∈ {ok, healthy, up} |
| Краб `src/voice_engine.py:237` → `krab_ear_client.py:279` | IPC `synthesize_speech` | `result.wav_bytes_b64` |
| Краб `src/mcp_tools/voice_assistant_tools.py` | IPC `get_history` — **нет у Ear** (бриф Крабу: перейти на `get_history_page`) | `result.items` |
| Краб `src/handlers/commands/voicenotes_command.py:631,646`, `src/core/video_studio_ear_stt.py:32,176-183` | REST `POST /v1/stt/transcribe` | `text`, `segments` |
| Краб `observability_commands.py:3049` | `GET :18000/health` — **порт неверный**, правильный — `:5005/health` | HTTP-статус |
| VG `app/stt_engines.py:269` | REST `POST /v1/stt/transcribe` (+ `request_profile`, `persist_history`, `quality_profile`) | `text` |
| VG `app/tts_engines.py:186` | REST `POST /v1/tts/synthesize` | `wav_bytes_b64` |

Сторона Ear:
- `KrabEar/backend/service.py` — `_build_dispatch_table`: ключи `"ping"` (строка 2793), `"synthesize_speech"` (3117), `"get_history_page"`; AST находит 360 ключей верхнего уровня.
- `rest_server.py:1056-1059` — `monitoring_blp`, `url_prefix=""`; `:1541-1544` — `v1_blp`, `url_prefix="/v1"`.
- `rest_server.py:1070` — `@monitoring_blp.route("/health", methods=["GET"])`; `:1730` — `@v1_blp.route("/tts/synthesize", methods=["POST"])`; `:1777` — `@v1_blp.route("/stt/transcribe", methods=["POST"])`.
- Ключи ответа: `health_check_service.py` `handle_ping` → `"status"` (225); `tts_service.py` `handle_synthesize_speech` → `"wav_bytes_b64"` (569); `history_service.py` `handle_get_history_page` → `"items"` (342); `rest_server.py` `synthesize_speech` → `"wav_bytes_b64"` (1770); `rest_server.py` `transcribe_audio` → `"text"`, `"segments"` (2114).
- Живое: `curl http://127.0.0.1:5005/health` → 200; `/v1/health` → 404.
- Общие файлы (не API, но стык): `~/.openclaw/lm_studio_brain.lock` (`backend/brain_lease.py`; Ear TTL 30 с, Краб — 120 с), `~/Library/Application Support/KrabEar/mlx_inter_process.lock` (`core/mlx_inter_lock.py:51`).

---

### Task 1: Документ контракта

**Files:**
- Create: `docs/contracts/ear-external-surface.md`

- [ ] **Step 1: Создать файл с содержимым**

````markdown
# Внешние интерфейсы Krab Ear (контракт для соседних проектов)

Обновлено: 2026-09-17. Гард: `KrabEar/tests/test_external_surface_contract.py`.
Правило: переименование или удаление любого пункта ниже — это **ломающее изменение**.
Сначала предупредить потребителя (сессии Krab Main / Voice Gateway), потом менять.

## IPC (unix-socket `~/Library/Application Support/KrabEar/krabear.sock`, JSON-RPC построчно)

| Метод | Потребитель | Ключи ответа, которые читает потребитель |
|---|---|---|
| `ping` | Краб: health probe, KrabEarClient | `ok`, `result.status` (`"ok"`) |
| `synthesize_speech` | Краб: локальный TTS в `voice_engine` | `result.wav_bytes_b64` |
| `get_history_page` | Краб: MCP voice-инструменты (сейчас ошибочно зовут `get_history`) | `result.items` |

## REST (`http://127.0.0.1:5005`)

| Маршрут | Потребитель | Ключи ответа |
|---|---|---|
| `GET /health` | Краб: панель наблюдения (сейчас ошибочно `:18000`), smoke-раннер Ear | HTTP 200 |
| `POST /v1/stt/transcribe` | Краб: голосовые заметки, video studio; VG: STT звонков (`request_profile=voice_gateway_call`) | `text`, `segments` |
| `POST /v1/tts/synthesize` | VG: локальный TTS | `wav_bytes_b64` |

Аутентификация REST: опциональный Bearer (`REST_API_AUTH_ENABLED`); VG передаёт `KRAB_EAR_REST_API_KEY`.

## Общие файлы-замки (не API, но стык)

| Файл | Кто пишет | Смысл |
|---|---|---|
| `~/.openclaw/lm_studio_brain.lock` | Ear (`owner="krab_ear"`, TTL 30 с), Краб (`owner="krab"`, TTL 120 с) | Сериализация большой модели в LM Studio |
| `~/Library/Application Support/KrabEar/mlx_inter_process.lock` | Ear (все MLX-пути) | Межпроцессная сериализация GPU. Сторонний процесс, запускающий MLX на этой машине, обязан брать этот flock |

## Известные расхождения у потребителей (17.09, брифы отправлены)

- Краб зовёт IPC `get_history` — метода нет; нужен `get_history_page`.
- Краб проверяет здоровье Ear на `:18000/health` — правильно `:5005/health`.
- Краб `mcp_client.py` зовёт `:5005/screenshot` — такого маршрута у Ear нет.
- Краб `perceptor.py` запускает mlx_whisper без `mlx_inter_process.lock`.
````

### Task 2: Гард (сначала самотест детекторов)

**Files:**
- Create: `KrabEar/tests/test_external_surface_contract.py`

- [ ] **Step 1: Написать тест**

```python
"""Контракт внешних потребителей Krab Ear (Краб, Voice Gateway).

Соседние проекты зовут Ear по именам, которых не видит их CI. 17.09.2026
сверка стыка нашла у Краба вызов несуществующего IPC `get_history` и проверку
здоровья на порту, который никто не слушает. Гард держит сторону Ear: всё из
docs/contracts/ear-external-surface.md существует в исходниках. Разбор AST,
без импорта backend — не нужны ML-зависимости.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

KRABEAR = Path(__file__).resolve().parents[1]
CONTRACT_DOC = KRABEAR.parent / "docs" / "contracts" / "ear-external-surface.md"

IPC_METHODS = ("ping", "synthesize_speech", "get_history_page")

# полный путь -> (переменная blueprint, путь внутри blueprint, HTTP-метод)
REST_ROUTES = {
    "/health": ("monitoring_blp", "/health", "GET"),
    "/v1/stt/transcribe": ("v1_blp", "/stt/transcribe", "POST"),
    "/v1/tts/synthesize": ("v1_blp", "/tts/synthesize", "POST"),
}

# (файл относительно KrabEar/, функция, ключ, который читает потребитель)
RESPONSE_KEYS = (
    ("backend/health_check_service.py", "handle_ping", "status"),
    ("backend/tts_service.py", "handle_synthesize_speech", "wav_bytes_b64"),
    ("backend/history_service.py", "handle_get_history_page", "items"),
    ("backend/rest_server.py", "synthesize_speech", "wav_bytes_b64"),
    ("backend/rest_server.py", "transcribe_audio", "text"),
    ("backend/rest_server.py", "transcribe_audio", "segments"),
)


def functions_named(tree: ast.AST, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def dict_string_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for key in sub.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def blueprint_prefixes(tree: ast.AST) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "Blueprint"):
            continue
        for kw in node.value.keywords:
            if kw.arg == "url_prefix" and isinstance(kw.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        prefixes[target.id] = kw.value.value
    return prefixes


def registered_routes(tree: ast.AST) -> set[tuple[str, str, str]]:
    """(blueprint, путь, HTTP-метод) из декораторов `@<bp>.route("/p", methods=[...])`."""
    routes: set[tuple[str, str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route"
                and isinstance(dec.func.value, ast.Name)
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                continue
            methods = ["GET"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
            for method in methods:
                routes.add((dec.func.value.id, dec.args[0].value, method))
    return routes


def parse(rel: str) -> ast.AST:
    return ast.parse((KRABEAR / rel).read_text(encoding="utf-8"))


class ExternalSurfaceContractTests(unittest.TestCase):
    def test_ipc_methods_are_dispatched(self) -> None:
        builders = functions_named(parse("backend/service.py"), "_build_dispatch_table")
        self.assertEqual(len(builders), 1, "ожидался ровно один _build_dispatch_table")
        keys = dict_string_keys(builders[0])
        missing = [m for m in IPC_METHODS if m not in keys]
        self.assertEqual(missing, [], "внешние потребители зовут эти IPC-методы — см. контракт")

    def test_rest_routes_are_registered(self) -> None:
        tree = parse("backend/rest_server.py")
        prefixes = blueprint_prefixes(tree)
        routes = registered_routes(tree)
        problems: list[str] = []
        for full_path, (bp, path, method) in REST_ROUTES.items():
            if prefixes.get(bp, None) is None:
                problems.append(f"{full_path}: blueprint {bp} не найден")
                continue
            if prefixes[bp] + path != full_path:
                problems.append(f"{full_path}: префикс {bp}={prefixes[bp]!r} даёт {prefixes[bp] + path}")
            if (bp, path, method) not in routes:
                problems.append(f"{full_path}: нет @{bp}.route({path!r}, methods=[{method!r}])")
        self.assertEqual(problems, [])

    def test_response_keys_read_by_consumers_exist(self) -> None:
        problems: list[str] = []
        for rel, func, key in RESPONSE_KEYS:
            defs = functions_named(parse(rel), func)
            if len(defs) != 1:
                problems.append(f"{rel}:{func}: найдено определений {len(defs)}, ожидалось 1")
                continue
            if key not in dict_string_keys(defs[0]):
                problems.append(f"{rel}:{func}: нет ключа {key!r} в словарях ответа")
        self.assertEqual(problems, [])

    def test_every_guarded_name_is_documented(self) -> None:
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        names = list(IPC_METHODS) + list(REST_ROUTES) + sorted({k for _, _, k in RESPONSE_KEYS})
        undocumented = [n for n in names if f"`{n}" not in text and f"{n}`" not in text]
        self.assertEqual(undocumented, [], "гард и документ контракта разошлись")


class DetectorSelfTest(unittest.TestCase):
    """Гард, тихо переставший находить нарушение, отчитывался бы зелёным вечно."""

    def test_route_detector_sees_prefix_and_method(self) -> None:
        tree = ast.parse(
            'v1_blp = Blueprint("v1", __name__, url_prefix="/v1")\n'
            '@v1_blp.route("/stt/transcribe", methods=["GET"])\n'
            "def transcribe_audio():\n"
            '    return {"text": ""}\n'
        )
        self.assertEqual(blueprint_prefixes(tree), {"v1_blp": "/v1"})
        self.assertNotIn(("v1_blp", "/stt/transcribe", "POST"), registered_routes(tree))
        self.assertIn(("v1_blp", "/stt/transcribe", "GET"), registered_routes(tree))

    def test_key_detector_misses_renamed_key(self) -> None:
        tree = ast.parse('def handle_get_history_page(p):\n    return {"itemz": []}\n')
        (func,) = functions_named(tree, "handle_get_history_page")
        self.assertNotIn("items", dict_string_keys(func))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Прогнать**

```bash
PYTHONPATH=$(pwd)/KrabEar "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m pytest KrabEar/tests/test_external_surface_contract.py -v -p no:cacheprovider
```

Ожидаемо: `6 passed`. Гард фиксирует **существующий** контракт, поэтому зелёный сразу — это правильно (правило «RED уже зелёный → стоп» здесь не действует). Доказательство, что он ловит поломки, — Step 3.

- [ ] **Step 3: Мутационная проверка (обязательно, потом откатить)**

```bash
sed -n '318p;342p' KrabEar/backend/history_service.py   # обе строки — ответы handle_get_history_page (privacy-ветка и обычная)
/usr/bin/sed -i '' '318s/"items": \[\]/"itemz": []/;342s/"items": items/"itemz": items/' KrabEar/backend/history_service.py
PYTHONPATH=$(pwd)/KrabEar "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m pytest KrabEar/tests/test_external_surface_contract.py -q -p no:cacheprovider
git checkout -- KrabEar/backend/history_service.py
git diff --stat KrabEar/backend/history_service.py
```

Ожидаемо: `test_response_keys_read_by_consumers_exist` FAILED с `handle_get_history_page: нет ключа 'items'`; после `checkout` — `git diff --stat` пуст. Результат обоих прогонов вставить в отчёт.

🔴 Менять **обе** строки: гард ищет ключ в любом словаре функции, а `"items"` есть и в privacy-ответе (318). Мутация одной строки 342 оставит тест зелёным — это не баг гарда, ключ реально есть. Если номера строк уехали — сначала найти оба `return` внутри `handle_get_history_page`. `/usr/bin/sed` — чтобы не попасть на GNU sed из Homebrew.

- [ ] **Step 4: Гейт**

```bash
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m flake8 --max-line-length=120 KrabEar/tests/test_external_surface_contract.py
scripts/pre_merge_py312_check.sh KrabEar/tests/test_external_surface_contract.py
```

### Task 3: Ссылка из CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — раздел `## Important Patterns`, добавить пункт в конец списка

- [ ] **Step 1:** добавить строку

```markdown
- **Внешний контракт Ear (2026-09-17)**: IPC-методы, REST-маршруты и ключи ответов, которые зовут Краб и Voice Gateway, перечислены в `docs/contracts/ear-external-surface.md` и защищены `KrabEar/tests/test_external_surface_contract.py`. Переименование пункта — ломающее изменение: сначала предупредить сессию-потребителя, потом менять.
```

### Task 4: Коммит и PR

- [ ] `git branch --show-current` → `feat/external-surface-contract`
- [ ] `git add` явными путями: `docs/contracts/ear-external-surface.md`, `KrabEar/tests/test_external_surface_contract.py`, `CLAUDE.md`
- [ ] Коммит: `test(contract): внешние интерфейсы Ear под гардом`
- [ ] PR в `codex/krab-ear-v2`; в описании — вывод мутационной проверки.

## Definition of Done

- 6 тестов зелёные; мутация ключа `items` роняет ровно один тест; мутация откачена.
- Документ и гард согласованы (4-й тест).
- Хендлеры Ear не менялись; репозитории Краба и VG не трогались.

## Потом (не в этой карточке)

Когда ответит сессия VG, координатор добавит в контракт то, что VG потребляет сверх списка (например, `/v1/stream`). Потребительские тесты на стороне Краба и VG — их брифы, не эта карточка.
