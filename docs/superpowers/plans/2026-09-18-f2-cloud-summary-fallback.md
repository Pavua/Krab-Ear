# F2: облачный фоллбэк summary (узко, по D3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** когда локальный LLM недоступен (LM Studio DOWN), `summarize_item` и итоги встреч пробуют облако — иначе как сейчас extractive/пусто. Только summary, только Studio-DOWN, privacy всегда сильнее, месячный лимит трат.

**Architecture:** встраиваемся в существующую точку отказа: `_generate_summary` возвращает None → сейчас сразу extractive. Новая ветка между ними: `privacy? → extractive` → `enabled + studio-down + cap-ok? → cloud rewrite(summary-промпт) → ok? cloud : extractive`. Переиспользуем готовое: провайдеры `backend/cloud_rewriter.py` (`OpenAIRewriterProvider`, custom/anthropic — SSRF-guarded opener), предикат `is_studio_unavailable` (`llm_rewriter.py`, НЕ путать с пустым каталогом — C1: пустой каталог = extractive без облака), гейт `_cloud_rewrite_allowed()` (`engine.py:712`, privacy+enabled — сверить сигнатуру и переиспользовать логику, не дублировать). Новое только одно: месячный счётчик трат (файл `data_dir/cloud_spend.json` `{YYYY-MM: usd}`, fail-closed к extractive при превышении; цену берём из ответа провайдера если есть, иначе консервативная оценка по тарифу модели из конфига).

**Tech Stack:** Python `unittest` + `unittest.mock` (сеть в тестах ЗАПРЕЩЕНА — провайдер только мок). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/f2-cloud-summary`, ветка `feat/f2-cloud-summary`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно — 🔴 PRIVACY-КРИТИЧНО: **диктовку в облако НЕ отправлять** (rewrite диктовки остаётся сырым STT); любой путь, отправляющий текст наружу, обязан начинаться с privacy-проверки (fail-closed); тексты транскриптов в тесты/логи/отчёты НЕ тащить (синтетика вида «тестовый текст N»); секреты (`cloud_rewriter_api_key`) только из settings/ENV, никогда в код/логи/отчёты; после мержа — независимое security-ревью (координатор).

---

## Проверенные факты (координатор, 18.09, file:line)

- `text_processing_service.py:147` `_generate_summary(text) -> Optional[str]` (None если LLM недоступен); `:162` `handle_summarize_item` — privacy-гейт на уровне хендлера уже есть (wave-1770 HIGH); дальше читает текст из history, зовёт `_generate_summary`, при None — extractive (дочитать точную ветку в файле, строки ~185+).
- `recording_core_service.py:3954` `_generate_summary` — путь итогов встреч (вторая точка встройки; сверить, куда уходит None).
- `llm_rewriter.py:1207` `summarize(text, max_sentences=3) -> LLMRewriteResult(ok, text, fallback_reason, latency_ms)`; `:1250` — пустой каталог → extractive без POST (C1).
- `engine.py:1800-1803` — прецедент различия DOWN vs пусто + вызов `_cloud_rewrite_allowed()` и `is_studio_unavailable` (прочитать и повторить семантику).
- `engine.py:712` `_cloud_rewrite_allowed()` — privacy + enabled (прочитать точные условия).
- `cloud_rewriter.py:186-218` — `CloudRewriterProvider.rewrite(text, language, system_prompt) -> Dict`; `adopt_settings_reader`/`_load_settings` (`:156-184`) — как провайдер читает конфиг.
- Конфиг (`core/config.py:1277+`): `cloud_rewriter_enabled=False`, `provider=openai`, `openai_model=gpt-4o-mini`, `base_url/custom_model/api_key`. Месячного лимита НЕТ — проектируем в Task 2.
- D3-рамка (решение владельца): summary/встречи only; Studio DOWN only; privacy wins; месячный лимит.

---

### Task 1: Тесты ветки (RED)

**Files:**
- Create: `KrabEar/tests/test_cloud_summary_fallback.py`

- [ ] **Step 1: Написать тесты** (провайдер — строго мок, сети нет):

```python
"""F2: облачный фоллбэк summary — узко по D3 (только summary, только Studio-DOWN)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _svc(monkey_settings: dict):
    from backend import text_processing_service as tps
    svc = tps.TextProcessingService.__new__(tps.TextProcessingService)
    svc._settings_get = lambda k, d=None: monkey_settings.get(k, d)
    return svc


class CloudSummaryFallbackTests(unittest.TestCase):
    def test_privacy_mode_never_calls_cloud(self) -> None:
        """privacy ON → extractive/пусто, провайдер не создан и не вызван."""
        svc = _svc({"privacy_mode_enabled": True, "cloud_rewriter_enabled": True})
        with patch("backend.text_processing_service.CloudRewriterProvider", create=True) as provider:
            result = svc._generate_summary("тестовый текст 1")
        provider.assert_not_called()
        self.assertIsNone(result)  # вызывающий код идёт в extractive

    def test_empty_catalog_never_calls_cloud(self) -> None:
        """Пустой каталог Studio ≠ DOWN (C1): облака нет даже при enabled."""
        svc = _svc({"privacy_mode_enabled": False, "cloud_rewriter_enabled": True})
        with patch("backend.text_processing_service.is_studio_unavailable", return_value=False, create=True), \
             patch("backend.text_processing_service.CloudRewriterProvider", create=True) as provider:
            result = svc._generate_summary("тестовый текст 2")
        provider.assert_not_called()
        self.assertIsNone(result)

    def test_studio_down_calls_cloud_and_returns_text(self) -> None:
        svc = _svc({"privacy_mode_enabled": False, "cloud_rewriter_enabled": True})
        fake = MagicMock()
        fake.rewrite.return_value = {"ok": True, "text": "облачное саммари", "usd": 0.0001}
        with patch("backend.text_processing_service.is_studio_unavailable", return_value=True, create=True), \
             patch("backend.text_processing_service.build_cloud_provider", return_value=fake, create=True):
            result = svc._generate_summary("тестовый текст 3")
        self.assertEqual(result, "облачное саммари")
        fake.rewrite.assert_called_once()

    def test_cloud_failure_falls_back_to_none(self) -> None:
        """Облако упало/лимит исчерпан → None (вызывающий идёт в extractive), без исключений наружу."""
        svc = _svc({"privacy_mode_enabled": False, "cloud_rewriter_enabled": True})
        fake = MagicMock()
        fake.rewrite.return_value = {"ok": False, "error": "timeout"}
        with patch("backend.text_processing_service.is_studio_unavailable", return_value=True, create=True), \
             patch("backend.text_processing_service.build_cloud_provider", return_value=fake, create=True):
            result = svc._generate_summary("тестовый текст 4")
        self.assertIsNone(result)

    def test_spend_cap_blocks_cloud(self) -> None:
        """Исчерпанный месячный лимит → облака нет (fail-closed к extractive)."""
        svc = _svc({"privacy_mode_enabled": False, "cloud_rewriter_enabled": True,
                    "cloud_spend_cap_usd_monthly": 0.0})
        with patch("backend.text_processing_service.is_studio_unavailable", return_value=True, create=True), \
             patch("backend.text_processing_service.CloudRewriterProvider", create=True) as provider:
            result = svc._generate_summary("тестовый текст 5")
        provider.assert_not_called()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
```

🔴 Две проверки перед прогоном (имена/конструкторы могут отличаться — сверить `rg`, править ТЕСТ под код, не код): конструктор `TextProcessingService(...)` (здесь — через `__new__` + `_settings_get`; если класс требует store/rewriter в `__init__` — инжектить моки, а не `__new__`); имена `is_studio_unavailable` / фабрики провайдера (в тесте — `build_cloud_provider`; если фабрики нет — создать тонкую `build_cloud_provider(settings_get)` в `cloud_rewriter.py` рядом с провайдерами и использовать её и в прод-коде, и в тесте).

- [ ] **Step 2: RED**:

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_cloud_summary_fallback.py -v -p no:cacheprovider
```
Ожидаемо: FAIL (ветки нет — `_generate_summary` возвращает None всегда). Если FAIL по именам — это проверки выше, не RED.

### Task 2: Реализация (GREEN)

**Files:**
- Modify: `KrabEar/backend/text_processing_service.py` (`_generate_summary` + опционально фабрика)
- Modify: `KrabEar/backend/recording_core_service.py` (`_generate_summary`, та же ветка)
- Modify: `KrabEar/backend/cloud_rewriter.py` (только если нужна фабрика `build_cloud_provider`)
- Modify: `KrabEar/core/config.py` (ключи `cloud_spend_cap_usd_monthly` (дефолт 0 = лимит не задан → ветка выключена до явного лимита? РЕШЕНИЕ: дефолт `1.0` USD/мес — безопасный потолок из коробки; 0 = запрещено) + `cloud_spend_*` учёт)
- Test: `KrabEar/tests/test_cloud_summary_fallback.py`

- [ ] **Step 1: Счётчик трат** — `data_dir/cloud_spend.json` вида `{"2026-09": 0.0123}`; хелперы `read_spend(month)`, `add_spend(usd)` (атомарная запись через tmp+replace, как принято в репо); цена — из ответа провайдера (`usd`), иначе оценка `len(text)/4/1000 * TARIFF[model]` (тарифы маленькой таблицей в `cloud_rewriter.py`, gpt-4o-mini первым). Проверка лимита ДО вызова; превышен/равен → сразу None. 🔴 Ключ от провайдера/тексты в файл трат НЕ писать (только цифры).
- [ ] **Step 2: Ветка в обоих `_generate_summary`**: `privacy? → None` → `local = llm.summarize(...)` → `if ok: return` → `if not (enabled and studio_down and cap_ok): return None` → `cloud = provider.rewrite(text, lang, SUMMARY_SYSTEM_PROMPT)` (промпт: «сожми в 3 предложения, язык входа») → `ok? (add_spend; return text) : None`. ВСЯ ветка в try/except → None (облако никогда не роняет summary).
- [ ] **Step 3: GREEN** — команда Task 1 Step 2. Ожидаемо: 5 passed.
- [ ] **Step 4: Регрессия** — файлы, покрывающие summarize: найти `rg -ln 'summarize_item|_generate_summary' KrabEar/tests/` и прогнать их все. Ожидаемо: зелёные без правок (правка существующих тестов запрещена — расхождение = стоп координатору).
- [ ] **Step 5: Гейт** — flake8 (max-120) изменённых + `scripts/pre_merge_py312_check.sh` на новый тест. `make audit-all` — да (тронуты сервисы).

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `feat/f2-cloud-summary`
- [ ] `git add` **явными путями** (только файлы Task 2)
- [ ] Коммит: `feat(summary): облачный фоллбэк summary при Studio-DOWN (D3-узко)`
- [ ] PR в `codex/krab-ear-v2` с пометкой «privacy-путь: нужен gate-security»; НЕ мержить.

## Definition of Done

- 5 тестов RED→GREEN; регрессия summarize-файлов зелёная; audit-all зелёный.
- В прод-коде: диктовка облака не касается (проверить `rg -n 'cloud' KrabEar/core/engine.py` — только существующие строки); privacy-проверка ПЕРВОЙ в ветке; секреты не в коде/логах.
- Деплой/включение флага — НЕ в этой карточке (флаг остаётся False; включает владелец отдельной командой после мержа).

## Вне scope (записать в отчёт, не чинить)

- Включение `cloud_rewriter_enabled` / ввод API-ключа — владелец.
- Точный тариф провайдера — таблицей-приближением, сверка по первому счёту.
- Cloud для диктовки — запрещено D3 (не делать).
