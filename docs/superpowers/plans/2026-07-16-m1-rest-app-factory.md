# M1: REST App-Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Превратить декоративный `create_app()` в `KrabEar/backend/rest_server.py` в настоящую app-factory с DI зависимостей, сохранив поведение бит-в-бит и НЕ правя ни один из 752 существующих тестов.

**Architecture:** Спека — `docs/superpowers/specs/2026-07-16-m-series-rest-merge-design.md` §3 (прочитай её §3 перед стартом). Хендлеры перестают читать module-глобалы напрямую и читают их через `_deps()`; для standalone-пути `_deps()` возвращает прокси `_ModuleGlobalsDeps`, читающий живые module-атрибуты (это сохраняет патчабельность 12 тест-файлов категории B: `patch.object(rest_server, "store", …)`). Вся app-инфраструктура (Flask, Api, Sock, Limiter, CORS, хуки, error-handlers, WS-роуты) переезжает внутрь `create_app(deps)`; module-level `app = create_app()` строится в конце модуля — все module-имена (`app`, `store`, `engine`, `sock`, `ws_stream`, …) сохраняются.

**Tech Stack:** Python 3.14 (`.venv_krab_ear`), Flask + flask-smorest + flask-sock + flask-limiter + flask-cors, unittest/pytest.

---

## Контекст и жёсткие правила для воркера

- Репозиторий: `/Users/pablito/Antigravity_AGENTS/Krab Ear`, база — ветка `codex/krab-ear-v2`. Работать в изолированном worktree, первым действием `git checkout -b feature/m1-rest-app-factory`.
- **ЗАПРЕЩЕНО менять любой существующий файл в `KrabEar/tests/`** — DoD волны: `git diff --stat KrabEar/tests/` показывает ТОЛЬКО новые файлы. Если существующий тест падает — чини production-код, не тест; если не выходит — STATUS: BLOCKED с объяснением.
- **ЗАПРЕЩЕНО менять поведение эндпойнтов**: ни статус-кодов, ни схем ответов, ни auth/CORS/rate-limit логики (спека §7).
- `rest_server.py` сегодня 2190 строк; правки точечные, никакого переформатирования нетронутых блоков.
- Тесты гонять так: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/<файл> -v -p no:cacheprovider` из корня worktree.
- Линт перед каждым коммитом: `.venv_krab_ear/bin/python -m flake8 KrabEar/backend/rest_server.py KrabEar/tests/test_rest_app_factory_M1.py KrabEar/tests/test_rest_vg_contract_M1.py --max-line-length=120` (W293 в тестах НЕ прощается).
- Коммиты — с трейлером `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

### Карта существующего кода (rest_server.py, состояние на 2b3c4e35)

| Строки | Что там | Судьба в M1 |
|---|---|---|
| 46-58 | `app = Flask(__name__)` + config + `api = Api(app)` + `sock = Sock(app)` | внутрь фабрики |
| 61 | `app.after_request(api_version_header())` | внутрь фабрики |
| 91-103+ | `_cors_origins`/`_cors_credentials` + `CORS(app, …)` | вычисление origins остаётся module-level; вызов `CORS(app,…)` — внутрь фабрики |
| 228-237 | `limiter = Limiter(key_func…, app=app, …)` + `register_error_handler(429,…)` | `Limiter(…)` БЕЗ `app=`; `limiter.init_app(app)` и оба `register_error_handler` — внутрь фабрики |
| 240-259 | `_request_entity_too_large_handler` + `_rest_mod_max_content_mb` (читает `app.config`) | функции остаются; `_rest_mod_max_content_mb` переводится на `current_app` с fallback на module-`app` |
| 530-534 | standalone-синглтоны `engine/store/transcriber/translator/tts_service` | НЕ трогать (eager на импорте — контракт категории A) |
| 537-599 | `_propagate_hf_token_to_env()` + `_rest_engine_cleanup` (atexit) | НЕ трогать (standalone-путь) |
| 617-633 | `@app.before_request` ×2 + `@app.after_request` | декораторы снять, регистрация внутри фабрики |
| 669-1147 | `monitoring_blp` + его роуты | НЕ трогать структуру; только свип globals→`_deps()` |
| 1149-1668 | `v1_blp` + его роуты | то же |
| 1670-1744 | `/internal/event` (на monitoring_blp) | то же |
| 1746-1774 | `@app.route` /v2/ catch-all | декораторы снять, регистрация внутри фабрики |
| 1875-1917 | `@sock.route("/ws/events")` | декоратор снять, регистрация внутри фабрики |
| 2122-2124 | декоративный `create_app` (`return app`) | заменяется настоящей фабрикой |
| 2128-2129 | `ws_stream = _ws_stream_handler` + `sock.route("/v1/stream")(…)` | алиас остаётся; регистрация внутрь фабрики |
| где-то ~660 | `api.register_blueprint(monitoring_blp)` / `(v1_blp)` — найди grep'ом `register_blueprint` | внутрь фабрики |

Свип globals→`_deps()` (реальные обращения в хендлерах, найдены аудитом): `store` ≈16, `engine` ≈6, `transcriber` 3, `translator` 3, `tts_service` 1, `metrics` ≈5, `event_bus` 3, `sse_stream` 1. Точные места ищи grep'ом в зоне 617-2120; НЕ трогай обращения в 537-599 (startup-хелперы standalone).

---

### Task 1: RestDeps-инфраструктура + контракт-тест прокси

**Files:**
- Modify: `KrabEar/backend/rest_server.py` (новый блок после строки 44, до `app = Flask`)
- Test (create): `KrabEar/tests/test_rest_app_factory_M1.py`

- [ ] **Step 1: Написать падающий тест**

Создай `KrabEar/tests/test_rest_app_factory_M1.py`:

```python
"""M1 (спека 2026-07-16-m-series-rest-merge-design §3): фабрика + deps-прокси.

Импорт rest_server — по канону категории A: патчим тяжёлые конструкторы ВОКРУГ
импорта (см. test_rest_smoke.py). Обратимых sys.modules-стабов не используем.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_REST_AVAILABLE = False
try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.load_settings.return_value = {}
    _mock_transcriber = MagicMock()

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
            patch("backend.state_store.StateStore", return_value=_mock_store), \
            patch("backend.transcriber.Transcriber", return_value=_mock_transcriber):
        import backend.rest_server as rs
    _REST_AVAILABLE = True
except Exception:  # pragma: no cover - защитный skip как в test_rest_smoke.py
    rs = None


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class ModuleGlobalsDepsTest(unittest.TestCase):
    """Страж категории B: патч module-атрибута виден через deps-прокси."""

    def test_proxy_reads_live_module_attribute(self):
        fake_store = MagicMock(name="patched_store")
        with patch.object(rs, "store", fake_store):
            self.assertIs(rs._MODULE_DEPS.store, fake_store)
        self.assertIsNot(rs._MODULE_DEPS.store, fake_store)

    def test_deps_helper_falls_back_without_app_context(self):
        # Прямые вызовы хендлеров без request-контекста (канон reload-тестов,
        # см. CLAUDE.md про self.rs.ws_stream(...)) должны получать module-глобалы.
        self.assertIs(rs._deps(), rs._MODULE_DEPS)

    def test_static_deps_is_plain_container(self):
        deps = rs.StaticDeps(
            engine="e", store="s", transcriber="tr", translator="tl",
            tts_service="tts", metrics="m", event_bus="b", sse_stream="ss",
        )
        self.assertEqual(deps.store, "s")
        self.assertEqual(deps.sse_stream, "ss")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тест падает по правильной причине**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_app_factory_M1.py -v -p no:cacheprovider`
Expected: FAIL/ERROR с `AttributeError: module 'backend.rest_server' has no attribute '_MODULE_DEPS'` (не ImportError самого модуля!).

- [ ] **Step 3: Реализация в rest_server.py**

Вставь после строки `logger = logging.getLogger("KrabEar.REST")` (строка 44):

```python
# ---------------------------------------------------------------------------
# M1 (спека 2026-07-16-m-series-rest-merge-design §3): DI-зависимости фабрики.
#
# Хендлеры читают коллабораторов ТОЛЬКО через _deps():
#   - внутри app-контекста — из app.config["REST_DEPS"] (у каждого app свои);
#   - вне контекста (прямой вызов хендлера из тестов) — module-глобалы,
#     как и до фабрики.
# _ModuleGlobalsDeps читает module-атрибуты ЖИВЬЁМ при каждом обращении —
# это контракт для patch.object(rest_server, "store", ...) в 12 тест-файлах.
# ---------------------------------------------------------------------------
from dataclasses import dataclass as _dataclass
from typing import Any as _Any, Protocol as _Protocol

from flask import current_app, has_app_context


class RestDeps(_Protocol):
    engine: _Any
    store: _Any
    transcriber: _Any
    translator: _Any
    tts_service: _Any
    metrics: _Any
    event_bus: _Any
    sse_stream: _Any


@_dataclass
class StaticDeps:
    """Путь M2: BackendService собирает deps из своих коллабораторов."""
    engine: _Any
    store: _Any
    transcriber: _Any
    translator: _Any
    tts_service: _Any
    metrics: _Any
    event_bus: _Any
    sse_stream: _Any


class _ModuleGlobalsDeps:
    """Standalone-путь: живое чтение module-атрибутов (имена совпадают 1:1)."""

    def __getattr__(self, name):
        return getattr(sys.modules[__name__], name)


_MODULE_DEPS = _ModuleGlobalsDeps()


def _deps() -> "RestDeps":
    if has_app_context():
        d = current_app.config.get("REST_DEPS")
        if d is not None:
            return d
    return _MODULE_DEPS
```

Проверь, что `import sys` уже есть в шапке модуля (есть, строка 8 зоны импортов — если нет, добавь). `current_app`/`has_app_context` добавь в существующую строку `from flask import ...` вместо локального импорта, если предпочитаешь — но тогда убери их из блока выше.

- [ ] **Step 4: Прогнать тест**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_app_factory_M1.py -v -p no:cacheprovider`
Expected: 3 passed.

- [ ] **Step 5: Смок нерегрессии + коммит**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_smoke.py -v -p no:cacheprovider`
Expected: как на базовой ветке (запусти на базе до правок, зафиксируй число passed/skipped).

```bash
git add KrabEar/backend/rest_server.py KrabEar/tests/test_rest_app_factory_M1.py
git commit -m "feat(rest): M1 Task 1 — RestDeps/StaticDeps/_ModuleGlobalsDeps + _deps()

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Настоящая create_app(deps)

**Files:**
- Modify: `KrabEar/backend/rest_server.py` (блоки из карты: 46-61, 103, 228-237, 617-633, 1746-1774, 1875, 2122-2129, register_blueprint)
- Test: `KrabEar/tests/test_rest_app_factory_M1.py` (дополнить)

- [ ] **Step 1: Дописать падающие тесты фабрики**

Добавь в `test_rest_app_factory_M1.py`:

```python
def _fresh_static_deps():
    eng = MagicMock()
    eng.quality_profile = "balanced"
    st = MagicMock()
    st.load_vocabulary.return_value = []
    st.load_settings.return_value = {}
    m = MagicMock()
    m.get_summary.return_value = {"total_requests": 0, "error_rate": 0, "status": "waiting_data"}
    return rs.StaticDeps(
        engine=eng, store=st, transcriber=MagicMock(), translator=MagicMock(),
        tts_service=MagicMock(), metrics=m, event_bus=MagicMock(), sse_stream=MagicMock(),
    )


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class CreateAppFactoryTest(unittest.TestCase):
    def test_two_apps_are_independent(self):
        d1, d2 = _fresh_static_deps(), _fresh_static_deps()
        app1, app2 = rs.create_app(d1), rs.create_app(d2)
        self.assertIsNot(app1, app2)
        self.assertIs(app1.config["REST_DEPS"], d1)
        self.assertIs(app2.config["REST_DEPS"], d2)

    def test_factory_app_health_uses_injected_engine(self):
        deps = _fresh_static_deps()
        deps.engine.quality_profile = "max"
        client = rs.create_app(deps).test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["profile"], "max")

    def test_module_level_app_still_exists_and_serves(self):
        resp = rs.app.test_client().get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_module_level_aliases_preserved(self):
        for name in ("app", "sock", "api", "limiter", "ws_stream",
                     "store", "engine", "transcriber", "translator", "tts_service"):
            self.assertTrue(hasattr(rs, name), f"module-алиас {name} пропал")
```

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_app_factory_M1.py -v -p no:cacheprovider`
Expected: `test_two_apps_are_independent` и `test_factory_app_health_uses_injected_engine` FAIL (create_app пока `return app` и игнорирует deps); тесты Task 1 — passed. `test_factory_app_health_uses_injected_engine` упадёт позже ещё раз — до Task 3 хендлер `/health` читает module-глобал; это ожидаемо, пометь его `@unittest.expectedFailure` СЕЙЧАС и сними маркер в Task 3 Step 1.

- [ ] **Step 3: Реализация фабрики**

Механика (каждый пункт — точечный перенос, поведение не меняется):

1. Строки 46-58: замени на объявление БЕЗ app:
   ```python
   def _base_config() -> dict:
       return {
           "MAX_CONTENT_LENGTH": 500 * 1024 * 1024,
           "API_TITLE": "Krab Ear REST API",
           "API_VERSION": "v1",
           "OPENAPI_VERSION": "3.0.3",
           "OPENAPI_URL_PREFIX": "/api",
           "OPENAPI_SWAGGER_UI_PATH": "/docs",
           "OPENAPI_SWAGGER_UI_URL": "https://cdn.jsdelivr.net/npm/swagger-ui-dist/",
       }
   ```
   (`app`/`api`/`sock` в этом месте больше не создаются — они появятся в конце модуля, см. п.8.)
2. Строка 61 (`app.after_request(api_version_header())`) — удалить, переносится в фабрику.
3. `CORS(app, …)` (~строка 103): оберни в функцию `def _init_cors(app):` с ТЕМ ЖЕ телом вызова (использует уже вычисленные module-level `_cors_origins`/`_cors_credentials`).
4. Limiter (228-237): убери `app=app` из конструктора (остальные аргументы не трогай); `app.register_error_handler(429, …)` и соседний `register_error_handler(413, …)` (найди grep'ом) — удалить отсюда, они уйдут в фабрику. Module-level `limiter` остаётся — на нём висят `@limiter.limit` декораторы.
5. Хуки 617-633: сними декораторы `@app.before_request`/`@app.after_request`, функции оставь как есть.
6. `/v2/` catch-all (1746-1747): сними оба `@app.route(...)` декоратора (и `@limiter.limit` на строке 1749 НЕ трогай — он остаётся на функции), запомни точные маршруты для регистрации в фабрике.
7. `@sock.route("/ws/events")` (1875): сними декоратор.
8. В конце модуля замени декоративный `create_app` (2122-2124) и блок 2128-2129 на:
   ```python
   def create_app(deps: "RestDeps | None" = None, config_mapping=None) -> Flask:
       """Настоящая фабрика (M1). deps=None → standalone module-глобалы."""
       flask_app = Flask(__name__)
       flask_app.config.update(_base_config())
       if config_mapping:
           flask_app.config.update(config_mapping)
       flask_app.config["REST_DEPS"] = deps if deps is not None else _MODULE_DEPS

       api_local = Api(flask_app)
       flask_app.after_request(api_version_header())
       _init_cors(flask_app)
       limiter.init_app(flask_app)
       flask_app.register_error_handler(429, _rate_limit_exceeded_handler)
       flask_app.register_error_handler(413, _request_entity_too_large_handler)
       flask_app.before_request(_check_vocabulary_post_size)
       flask_app.before_request(start_timer)
       flask_app.after_request(log_request)

       api_local.register_blueprint(monitoring_blp)
       api_local.register_blueprint(v1_blp)

       flask_app.add_url_rule(
           "/v2/", "v2_catchall_root", v2_catchall,
           defaults={"p": ""},
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
       flask_app.add_url_rule(
           "/v2/<path:p>", "v2_catchall", v2_catchall,
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])

       sock_local = Sock(flask_app)
       sock_local.route("/ws/events")(ws_events)
       sock_local.route("/v1/stream")(_block_cross_origin_reads(_ws_stream_handler))
       flask_app.extensions["krab_sock"] = sock_local
       return flask_app


   # --- standalone module-level путь (контракт категорий A/B/C: 752 теста) ---
   app = create_app()
   sock = app.extensions["krab_sock"]
   ws_stream = _ws_stream_handler
   # алиас api — по результату grep-проверки, см. примечание ниже
   ```
   Примечание про `api`: выясни фактическое имя module-глобала-потребителя — если `api` нигде вне фабрики не используется (grep `\bapi\.` по модулю после правок) и ни один тест его не импортирует (`grep -rn "rest_server.api\b\|import api" KrabEar/tests/`), алиас `api` можно объявить как `api = None  # M1: Api создаётся per-app внутри create_app` — но СНАЧАЛА подтверди grep'ами оба нуля. Если потребители есть — сохрани настоящий инстанс: верни его из фабрики через `flask_app.extensions["krab_api"] = api_local` и `api = app.extensions["krab_api"]`.
   Старый существующий `api.register_blueprint(...)` module-level вызов (найди grep'ом `register_blueprint`) — удали (переехал в фабрику).
9. Убедись, что определение `create_app` стоит ПОСЛЕ всех функций, которые оно регистрирует (хуки, v2_catchall, ws_events, `_ws_stream_handler`) — т.е. на своём текущем месте в конце файла, а `app = create_app()` — последним.
10. `_rest_mod_max_content_mb` (254-259): замени чтение `app.config` на:
    ```python
    cfg = current_app.config if has_app_context() else app.config
    limit = cfg.get("MAX_CONTENT_LENGTH", 500 * 1024 * 1024)
    ```
11. `__main__`-блок НЕ трогай (он использует module-level `app`, который теперь продукт фабрики).

- [ ] **Step 4: Прогнать тесты фабрики + смоки нерегрессии**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_app_factory_M1.py KrabEar/tests/test_rest_smoke.py KrabEar/tests/test_rest_server.py KrabEar/tests/test_rest_e2e.py -v -p no:cacheprovider`
Expected: фабрика-тесты passed (кроме expectedFailure), старые файлы — те же числа passed/skipped, что на базовой ветке.

- [ ] **Step 5: Коммит**

```bash
git add KrabEar/backend/rest_server.py KrabEar/tests/test_rest_app_factory_M1.py
git commit -m "feat(rest): M1 Task 2 — настоящая create_app(deps), app-инфра в фабрике

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Свип хендлеров на _deps()

**Files:**
- Modify: `KrabEar/backend/rest_server.py` (зона 617-2120)
- Test: `KrabEar/tests/test_rest_app_factory_M1.py` (снять expectedFailure)

- [ ] **Step 1: Снять маркер**

Убери `@unittest.expectedFailure` с `test_factory_app_health_uses_injected_engine`. Прогони файл — тест должен упасть (хендлер ещё читает module-глобал).

- [ ] **Step 2: Свип**

В КАЖДОМ request-time хендлере (зона 617-2120) замени чтение module-глобалов на локальную привязку в начале хендлера, например для `/health` (строки 686-688):

```python
def health():
    deps = _deps()
    return {"status": "ok", "service": "krab-ear", "profile": deps.engine.quality_profile}
```

Полный список целей (проверь each grep'ом, счётчики — ориентир): `store` (vocabulary GET/POST, transcribe, dashboard/HealthChecker, `_load_settings_field`, ws-стрим: ≈16 мест), `engine` (health, dashboard, transcribe normalize_audio, gigaam-adapter в 587-599 НЕ трогать — это atexit-хелпер standalone: ≈6), `transcriber` (HealthChecker, `_pool.submit`, LiveSubsService kwargs: 3), `translator` (LiveSubsService kwargs, 2 перевода в ws: 3), `tts_service` (synthesize: 1), `metrics` (get_metrics, prometheus, start_timer/log_request если читают: ≈5), `event_bus` + `sse_stream` (events_stream, internal_event, ws_events: 4).

Правила свипа:
- `deps = _deps()` — первой строкой хендлера, дальше `deps.store` и т.д. Вложенные функции хендлера используют ту же локальную `deps`.
- `_handle_ws_connection(ws, bus, …)` уже принимает bus параметром — поменяй только call-site (`_handle_ws_connection(ws, _deps().event_bus, type_filter)`).
- Функции зоны 537-599 (`_propagate_hf_token_to_env`, `_rest_engine_cleanup`) — НЕ трогать.
- `_load_settings_field` — переведи на `_deps().store`; `_privacy_gate` зависит от него — проверь, что после свипа existing privacy-тесты зелёные.

- [ ] **Step 3: Полный прогон rest-пласта**

Run (все 27 файлов одним процессом — ловим chunk-эффекты):
```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_rest_*.py KrabEar/tests/test_ws_streaming.py \
  KrabEar/tests/test_prometheus_metrics.py KrabEar/tests/test_api_v2_501_W1357.py \
  KrabEar/tests/test_cloud_stt.py KrabEar/tests/test_export_validation.py \
  KrabEar/tests/test_health_dashboard.py KrabEar/tests/test_rest_app_factory_M1.py \
  -v -p no:cacheprovider
```
Expected: числа passed/skipped == базовой ветке + новые тесты. Любое расхождение = регрессия патчабельности — чини `_deps()`/свип, НЕ тесты.

- [ ] **Step 4: Коммит**

```bash
git add KrabEar/backend/rest_server.py KrabEar/tests/test_rest_app_factory_M1.py
git commit -m "feat(rest): M1 Task 3 — хендлеры читают зависимости через _deps()

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Контракт-тесты VG-поверхности

**Files:**
- Test (create): `KrabEar/tests/test_rest_vg_contract_M1.py`

- [ ] **Step 1: Написать тесты (падать не должны — это стражи контракта, спека §3.3)**

```python
"""Контракт Voice Gateway ↔ Krab Ear REST (спека M-серии §1a, §4.5).

VG зовёт ровно: GET /health, POST /v1/stt/transcribe, POST /v1/tts/synthesize.
Эти тесты — стражи схемы/семантики, обязаны пережить M2/S3 без правок.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_REST_AVAILABLE = False
try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.load_settings.return_value = {}

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
            patch("backend.state_store.StateStore", return_value=_mock_store), \
            patch("backend.transcriber.Transcriber", return_value=MagicMock()):
        import backend.rest_server as rs
    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    rs = None


def _deps_with(privacy: bool = False, auth_enabled: bool = False):
    st = MagicMock()
    st.load_vocabulary.return_value = []
    st.load_settings.return_value = {
        "privacy_mode_enabled": privacy,
        "REST_API_AUTH_ENABLED": auth_enabled,
    }
    eng = MagicMock()
    eng.quality_profile = "balanced"
    tts = MagicMock()
    tts.handle_synthesize_speech.return_value = {
        "ok": True, "wav_bytes_b64": "UklGRg==", "language": "ru",
        "engine": "say", "byte_count": 8,
    }
    m = MagicMock()
    m.get_summary.return_value = {"total_requests": 0, "error_rate": 0, "status": "waiting_data"}
    return rs.StaticDeps(
        engine=eng, store=st, transcriber=MagicMock(), translator=MagicMock(),
        tts_service=tts, metrics=m, event_bus=MagicMock(), sse_stream=MagicMock(),
    )


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class VGHealthContractTest(unittest.TestCase):
    """preflight_call.py у VG парсит поля status и profile; httpx timeout=1s."""

    def test_health_schema(self):
        client = rs.create_app(_deps_with()).test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("status", body)
        self.assertIn("profile", body)
        self.assertEqual(body["status"], "ok")


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class VGPrivacyContractTest(unittest.TestCase):
    """403 + skipped:privacy_mode — VG останавливается БЕЗ fallback (их семантика)."""

    def test_tts_privacy_403(self):
        client = rs.create_app(_deps_with(privacy=True)).test_client()
        resp = client.post("/v1/tts/synthesize", json={"text": "привет"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json().get("skipped"), "privacy_mode")

    def test_stt_privacy_403(self):
        client = rs.create_app(_deps_with(privacy=True)).test_client()
        resp = client.post("/v1/stt/transcribe", data={})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json().get("skipped"), "privacy_mode")

    def test_tts_ok_without_privacy(self):
        client = rs.create_app(_deps_with()).test_client()
        resp = client.post("/v1/tts/synthesize", json={"text": "привет"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wav_bytes_b64", resp.get_json())


if __name__ == "__main__":
    unittest.main()
```

Если какой-то assert не совпал с фактическим поведением (например точная форма privacy-ответа) — СНАЧАЛА прочитай хендлер и поправь ОЖИДАНИЕ под фактическое прод-поведение (это стражи текущего контракта, не новая функциональность). Auth-пару 401 покрой, если auth включается через deps без env-хаков; если для 401 нужен реальный token-store — оставь privacy+health+ok-путь и напиши в отчёте NOTE.

- [ ] **Step 2: Прогнать**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_vg_contract_M1.py -v -p no:cacheprovider`
Expected: все passed.

- [ ] **Step 3: Коммит**

```bash
git add KrabEar/tests/test_rest_vg_contract_M1.py
git commit -m "test(rest): M1 Task 4 — контракт-тесты VG-поверхности (/health, stt, tts)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Финальные гейты волны

**Files:** без новых правок кода (только фиксы найденных регрессий)

- [ ] **Step 1: DoD «0 правок старых тестов»**

Run: `git diff --stat origin/codex/krab-ear-v2 -- KrabEar/tests/`
Expected: ТОЛЬКО `test_rest_app_factory_M1.py` и `test_rest_vg_contract_M1.py` (новые).

- [ ] **Step 2: Полный rest-пласт одним процессом**

Команда из Task 3 Step 3 + сравнение с базой. Expected: идентичные числа.

- [ ] **Step 3: ubuntu-parity**

Run: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_rest_app_factory_M1.py KrabEar/tests/test_rest_vg_contract_M1.py`
Expected: PASS (py3.12, mlx покinut).

- [ ] **Step 4: Аудиты + линт**

Run: `make audit-all` и flake8-команду из «жёстких правил».
Expected: всё зелёное (фабрика внутри rest_server.py — dead-module гард не триггерится).

- [ ] **Step 5: Живой e2e-смок бит-в-бит**

На базовой ветке И на feature-ветке подними standalone REST на временном data-dir и сверь ответы:
```bash
KRAB_EAR_DATA_DIR=$(mktemp -d) KRAB_EAR_REST_SERVER_PORT=5098 \
  .venv_krab_ear/bin/python KrabEar/backend/rest_server.py &
sleep 8
curl -s http://127.0.0.1:5098/health | python3 -m json.tool
curl -s http://127.0.0.1:5098/info | python3 -m json.tool
curl -s http://127.0.0.1:5098/v1/readiness | python3 -m json.tool
curl -s http://127.0.0.1:5098/v1/models | python3 -m json.tool | head -30
curl -s -N --max-time 3 "http://127.0.0.1:5098/v1/events?filter=krab_error" | head -5
kill %1
```
Expected: диффа в ответах между ветками нет (кроме заведомо динамических полей: uptime, timestamps).
Примечание: если env-переопределение порта называется иначе — проверь `core/config.py` (`REST_SERVER_PORT`, префикс `KRAB_EAR_`).

- [ ] **Step 6: Финальный отчёт**

STATUS: DONE / DONE_WITH_CONCERNS + headSha + числа прогонов (база vs ветка) + список NOTE.

---

## Self-review плана (выполнен автором)

- Покрытие спеки §3: 3.1 (прокси) → Task 1; 3.2 (фабрика) → Task 2+3; 3.3 (тесты) → Task 1/2/4; 3.4 (DoD) → Task 5. §4/§5 — вне плана (M2/S3, отдельные планы).
- Типы/имена согласованы: `RestDeps`/`StaticDeps`/`_MODULE_DEPS`/`_deps()`/`create_app(deps, config_mapping)` едины во всех задачах.
- Известные неопределённости отданы воркеру ЯВНО с методом разрешения (алиас `api`, точное место register_blueprint, форма privacy-ответа) — не placeholder'ы, а инструкции с grep-командами.
