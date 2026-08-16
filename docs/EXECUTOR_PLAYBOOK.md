# Playbook исполнителя — Krab Ear

Для агента, который получил **одну карточку** и не должен изобретать продукт. Оперативный фронт: [`NOW.md`](NOW.md). Журнал волн: [`ROADMAP-2026H2.md`](ROADMAP-2026H2.md). Карта кода: `AGENTS.md`, детали — `CLAUDE.md` (только когда карточка ссылается на конкретный раздел).

## 0. Перед первой правкой

1. Прочитай [`NOW.md`](NOW.md) целиком.
2. Прочитай **только** указанную карточку в `docs/superpowers/plans/`.
3. Если карточки нет — остановись. Не «улучшай» по журналу ROADMAP.
4. Проверь, что работаешь в **изолированном worktree** от `origin/codex/krab-ear-v2`, не в общем чекауте с чужим WIP.

```bash
git fetch origin
git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2
cd .worktrees/<slug>
```

## 1. Баны (копируй в начало каждой карточки, если их там нет)

- База только `origin/codex/krab-ear-v2`. Не `audit/*`, не detached stale worktree.
- `git add` явными путями. Никогда `-A`.
- Не запускать собранный `KrabEarAgent` / `open "Krab Ear.app"` — `SingleInstanceGuard` убьёт прод. Сборка: `swift build -c release` и `codesign --verify`.
- Прод-backend: только `scripts/safe_backend_restart.command`. Не голый `launchctl kickstart -k`.
- Не трогать Main Krab (`start_krab.command` / `Stop Krab.command` / `~/.openclaw`) и VG `.env` без явного scope в карточке.
- Не мержить PR #1875. Не дообучать `krab_ru` синтетическим TTS.
- Не строить второй EventBridge. Не возвращать wake word на SSE.
- Не включать `REST_IN_PROCESS_ENABLED` в проде.
- Визуал Swift (цвета/шрифты/layout): только `agy` + Gemini 3.1 Pro. IPC-ключи в UI — буква в букву с бэкенд-хендлером, не выдумывать.
- После правки SOURCE гонять зависящие тесты **на обоих языках** (`grep` Swift-сигнатуры в `KrabEar/tests/`).
- `BackendService(...)` в тесте → `service.close()` в `tearDown`.
- Не наследовать `threading.Thread` в тест-стабах, если `start()` не зовёт `super().start()`.
- Секреты (`lens_keys.env`, `hf_token`, `gh auth token`) использовать, **не печатать**.

## 2. Режим Cursor и модель

Координатор выбирает режим **до** старта. Исполнитель не переключает Cloud «на всякий случай».

| Задача | Режим | Модель |
|---|---|---|
| Есть карточка, известный паттерн, Python+тесты | Agent, локальный | Composer 2.5 или Grok 4.6 **high** (не xhigh) |
| Доки, grep, закрытие issue по списку, lint | Agent | Composer 2.5 Fast / Grok medium |
| Баг воспроизводится, нужны логи/Sentry/runtime | **Debug** | Grok 4.6 high или Opus high |
| Нет карточки / развилка / «построй X» | **Plan** — стоп, верни координатору | Grok 4.6 xhigh или Opus `effort=high` |
| Каскадный Swift-compiler hell | Agent | Opus high или Grok xhigh |
| Визуал | не этот чат | agy Gemini 3.1 Pro High |
| Скаут-аудит (не гейт) | Ask или отдельный worktree | `scripts/draft_audit.py`; security не на cerebras/groq/hf |
| Параллель | Multitask только disjoint files + свои worktree | см. запрет файлов ниже |
| Cloud / `/in-cloud` | **нет** для Krab Ear | macOS-native, TCC, живой backend, нет `.cursor/environment.json` |

Запрещено двоим одновременно: `KrabEar/backend/service.py`, `KrabEar/core/engine.py`, `KrabEar/backend/state_store.py`, `HistoryPanelController.swift`, `main.swift`, launchd plist. Двоим нельзя рестартовать backend.

Не жечь xhigh/Opus на lint, переименования, «добавь тест на уже понятый баг», обновление `NOW.md`.

## 3. Цикл одной карточки

1. RED: напиши/запусти падающий тест из карточки. Если он уже зелёный — **стоп**, возможно уже сделано (анти-rebuild). Доложи координатору.
2. Минимальный фикс.
3. GREEN: те же команды, что в карточке. Ожидаемый output — как в карточке.
4. Если трогал `core/pipeline/` или сервисы: `make audit-all`.
5. Изменённые test-файлы: `scripts/pre_merge_py312_check.sh <files>`.
6. Swift: `swift build -c release`. Нет нового `runModal()`. Нет новых Unicode-глифов вне уже используемых в `native/`.
7. Координатор гейтит **дифф**, не самоотчёт. Не пиши «готово», пока гейт не прошёл.
8. Живой e2e — только если карточка явно требует и не убивает прод-агент.

Скиллы по типу: `executing-plans` или `subagent-driven-development`; перед сдачей — `verification-before-completion`; баг с runtime — `systematic-debugging`. Веточная дисциплина — `krab-branch-handoff-governor`.

## 4. Шаблон карточки

Каждая новая волна обязана лежать в `docs/superpowers/plans/YYYY-MM-DD-<slug>.md` в этом виде. Шаги без кода/команды — брак. Нет «TBD», «добавь обработку ошибок», «аналогично задаче N».

````markdown
# <Имя> Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** одно предложение.

**Architecture:** 2–3 предложения.

**Tech Stack:** что уже в репо (не добавляй зависимости без строки в карточке).

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/<slug>`.

**Баны:** вставь список из §1.

---

### Task N: <компонент>

**Files:**
- Create: `точный/путь`
- Modify: `точный/путь.py` (ориентир по символу, не по протухшему номеру строки)
- Test: `KrabEar/tests/test_....py`

- [ ] **Step 1: Write the failing test**

```python
# полный тест, не псевдокод
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_X.py::Class::test_name -v`
Expected: FAIL с конкретной ошибкой

- [ ] **Step 3: Write minimal implementation**

```python
# полный код
```

- [ ] **Step 4: Run test to verify it passes**

Run: та же команда
Expected: PASS

- [ ] **Step 5: Commit** (только если карточка явно разрешает и нет чужого WIP)

```bash
git add <явные пути>
git commit -m "fix: …"
```
````

Спека волны (если есть) — `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md`. При расхождении спека важнее карточки; карточка важнее самоотчёта воркера.

## 5. DoD (без этого нельзя писать «готово»)

- Названные тесты RED→GREEN (или честный «уже зелёные, не трогал — анти-rebuild»).
- `make audit-all` если карточка требует.
- ubuntu-parity на изменённых test-файлах.
- Swift: нет `runModal()`, нет новых глифов, `swift build -c release` зелёный.
- Parity-бинари `Krab Ear.app` + `native/runtime` **не кладёт исполнитель** — это координатор после гейта.
- `NOW.md` обновлён, если волна закрыта.
- Секреты не в диффе.

## 6. Соседи

- Главный Краб: `/Users/pablito/Antigravity_AGENTS/Краб`. Ear → `:8080` через `telegram_bridge` + `X-Krab-Web-Key`. Не стартовать/останавливать Краба.
- Voice Gateway: звонки. Ear отдаёт `/v1/stt`, `/v1/tts`, `/v1/stream`. Их P1 — входящие, не наш UI встреч.
- Cloud Agents, новые MCP, `.cursor/environment.json` — не подключать без координатора.
