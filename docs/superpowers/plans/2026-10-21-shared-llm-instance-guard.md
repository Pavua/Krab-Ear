# Shared LLM instance guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Не выгружать общий brain через роль rewriter и не перезагружать один экземпляр при старте записи.

**Architecture:** Один вызов пассивного native-v1 metadata probe разрешает оба configured ID по точным `key`/`loaded_instances[].id`. Пересечение непустых instance sets блокирует действие; неизвестность также блокирует. Guard охватывает conductor и legacy recording hook (проверка внутри фонового unload worker). Каталог — снимок, а не атомарное право владения: guard не разрешает включать enforce и не заменяет межпроцессный протокол. Socket timeout 1.5с и лимит тела 1MiB не являются общим deadline; opener допускает redirects.

**Tech Stack:** Python stdlib, существующие urllib safe opener и dependency injection; unittest без ML.

**База:** `origin/codex/krab-ear-v2` = `052b6c904244aeedf571b78f9f9128677f0b5f85`. Worktree: `/Users/pablito/.codex/worktrees/ear-shared-llm-guard/Krab Ear`.

## Global Constraints

- Scope владельца: исходники/тесты/CI/ревью и согласование Main/Gateway. Деплой, рестарты, live ES и load/unload реальных моделей запрещены.
- Не менять production settings, `~/.openclaw`, соседние репозитории, shared WIP, бинарные parity-артефакты.
- Не включать `memory_conductor_enforce*`, `REST_IN_PROCESS_ENABLED`, brain autoload; не мержить #1875.
- Не запускать агент/приложение, не строить второй EventBridge, не менять UI/wake word/SSE.
- Сеть lifecycle в тестах подменена, shadow marker только во временном каталоге. Новых зависимостей нет.
- Не менять существующий bool/None-контракт `model_loaded` и существующую лестницу для доказанно разных экземпляров.

## Review Focus

- Разные configured alias ведут к одному instance ID: обе автоматические ветки ничего не выгружают/грузят.
- Отсутствующий или незагруженный ID, malformed metadata, неоднозначный alias и ошибка GET: неизвестность, без lifecycle действий.
- Разные instance IDs одного catalog key: сравнивать именно выбранные экземпляры, а catalog key представляет все свои экземпляры.
- Подлинно разные загруженные экземпляры сохраняют прежнее действие и verify/cooldown.
- Shadow/active recording не делают новый GET; секрет передаётся только Bearer и не логируется.

### Task 1: Metadata guard и обе точки применения

**Files:**
- Modify: `KrabEar/backend/lm_studio_lifecycle.py`
- Modify: `KrabEar/backend/memory_conductor.py`
- Modify: `KrabEar/backend/recording_core_service.py` (передача protected role в существующий async worker)
- Modify: `KrabEar/tests/test_memory_conductor_2026_08_19.py` (DI и временный marker)
- Create: `KrabEar/tests/test_shared_llm_instance_guard_2026_10_21.py`

**Interfaces:** `models_share_loaded_instance(base_url: str, model_id: str, protected_model_id: str, timeout: float = 1.5, api_key: str | None = None) -> bool | None`; `MemoryConductor(..., models_share_loaded_instance_fn=None)`; `unload_model_async(..., *, protected_model_id: str | None = None)` (None сохраняет explicit/manual API, пустая строка блокирует как unknown).

- [x] **Step 1:** Написать тест с native catalog `key=shared-qwen`, `loaded_instances=[{'id':'shared-runtime'}]`, rewriter=`shared-runtime`, brain=`shared-qwen`. Для `_rewriter_step(False)` и `_recording_sequence_worker()` ожидается `events == []`.
- [x] **Step 2:** RED: `PYTHONPATH=KrabEar python3.12 -m unittest discover -s KrabEar/tests -p test_shared_llm_instance_guard_2026_10_21.py -v`. Ожидается FAIL: неожиданный `unload` (и `load` в sequence).
- [x] **Step 3:** Реализовать helper одним native-v1 probe; неоднозначность/неполные данные/пустые instance sets возвращают None. Инжектировать helper в conductor. Пропускать обе точки при любом результате кроме буквального False, инкрементировать skipped_gate без cooldown. В legacy hook передать protected role, проверяя metadata внутри worker.
- [x] **Step 4:** GREEN: та же команда. Добавить таблицы malformed/alias/disjoint/auth/shadow/recording, прогнать существующие conductor/lifecycle тесты по одному файлу. Ожидается PASS без реального HTTP/ML/записи production marker.
- [ ] **Step 5:** Ubuntu parity на изменённых тестах существующим harness, если он уже доступен; иначе точный Python3.12 stdlib прогон плюс hosted CI и честная запись ограничения. Проверить audit/lint и независимый whole-diff review сильной моделью перед объявлением готовности.
- [ ] **Step 6:** Разрешены commit только явных перечисленных путей и публикация reviewable PR; merge/deploy/restart не входят в эту карточку.

## Execution ledger

Pre-flight: no shared interfaces between tasks (одна задача). Design boundary согласована Main; unknown identity fail-closed, новый глобальный coordinator не вводится. Native execution следует прямому запросу владельца продолжать исходники и тесты.

Ruling: independent review исходного conductor diff обнаружил существующий legacy bypass при enforce OFF. Scope расширен одной передачей protected role и worker guard, иначе широкая цель не доказана. Legacy hook RED: protected role отсутствует; worker RED: keyword не поддержан; GREEN: 16 тестов. Hook проверяется исполнением точного AST try-block без импорта audio/ML сервиса; это не live microphone E2E.

Ruling: строгий общий HTTP deadline не вводится в этот identity diff. Socket timeout и body cap — текущая защита; slow-drip/redirect total deadline остаётся отдельным ограничением. Atomic ownership, activity recheck между verify/load и admission всей экосистемы также не заявлены.
