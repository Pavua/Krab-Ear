# F2: месячный spend-cap для облачного фоллбэка summary (D3-узко) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** у существующего облачного фоллбэка summary появляется месячный лимит трат: исчерпан → тихо extractive, как будто облака нет. Больше ничего не меняется.

**Architecture:** 🔴 облачная ветка summary УЖЕ в проде (`LLMRewriter._maybe_apply_cloud_summarize`, покрыта `SummarizeCloudOnlyWhenStudioDownTest`) — **вторую облачную ветку НЕ строить** (это был бы двойной вызов = двойное списание + двойная эксфильтрация). Единственный шов: внутрь `_maybe_apply_cloud_summarize`, между существующими проверками (`_cloud_llm_fallback_allowed` + `is_studio_unavailable`) и вызовом `cloud_summarize` — вставить cap-check; после успеха — `add_spend`. Счётчик: `data_dir/cloud_spend.json` (`{"YYYY-MM": usd}`), атомарная запись tmp+replace (прецедент `state_store.py:2280-2288`); цена — ВСЕГДА оценка по тарифной таблице (провайдер usd не возвращает — проверено), custom/self-hosted тариф 0.0. Fail-closed везде: `_spend_dir is None` → облако заблокировано; лимит `0` = запрещено всё; любое исключение → результат без облака.

**Tech Stack:** Python `unittest` + `unittest.mock` (сеть в тестах ЗАПРЕЩЕНА — `cloud_summarize` только мок). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/f2-cloud-summary`, ветка `feat/f2-cloud-summary`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно — 🔴 PRIVACY-КРИТИЧНО: **диктовку в облако НЕ отправлять** (не трогать `engine.py` и STT-пути вообще); тексты транскриптов в тесты/логи/отчёты НЕ тащить (синтетика вида «тестовый текст N»); секреты только из settings/ENV, никогда в код/логи/отчёты/файл трат (в нём — только цифры); после мержа — независимое security-ревью (координатор).

---

## Проверенные факты (координатор, 18.09, file:line)

- Шов: `llm_rewriter.py:393-441` `_maybe_apply_cloud_summarize(result, text, max_sentences)`: `result.ok → return` → `_cloud_llm_fallback_allowed()` (`:376-388`: privacy wins, нет getter → закрыто) → `is_studio_unavailable(reason, last_error)` (`:273-289`: только `timeout`/`connection_error` (+circuit_open с такой ошибкой); пустой каталог — НЕ down) → `cloud_summarize(cleaned, max_sentences)` (импорт внутри метода, `:410`) → audit-лог БЕЗ текста (`:417-433`: только provider/input_chars/output_chars — прецедент «в spend-файл тоже только цифры») → `LLMRewriteResult(ok=True, text=...)` (`:436-441`).
- `cloud_summarize(text, max_sentences=3) -> Optional[str]` (`cloud_rewriter.py:496-514`): текст или None; usd НЕ возвращает (проверено `rg usd` — пусто). `get_cloud_rewriter(name)` (`:416`). Тарифной таблицы в репо НЕТ — создать.
- `summarize()` (`llm_rewriter.py:1207`): Studio → `_maybe_apply_cloud_summarize`. Оба `_generate_summary` (`text_processing_service.py:147`, `recording_core_service.py:3954`, семантика None идентична) идут через него — cap в шве покрывает оба пути сразу. (`recording_core:3954` — это батч-импорт аудио (`:3861`), не «итоги встреч»; итоги встреч — через `handle_summarize_item` (`service.py:4943`). Поведение не различать — шов общий.)
- Конструктор: `LLMRewriter(base_url, api_key, model, ...)` — прямого `__new__` не надо; `_settings_getter` — plain-атрибут (прецедент `test_studio_unavailable_cloud_fallback_2026_09_05.py:207`); `_spend_dir` — новый plain-атрибут (None = облако заблокировано).
- Прод-владелец один: `LLMRewriter(` в прод-коде только `service.py:1881`; `_settings_getter` ставится в `:552` — `_spend_dir = store.data_dir` ставить рядом (проверить, что `store` в скоупе — строки :474+ его используют).
- Конфиг точные имена (`core/config.py:1277+`): `cloud_rewriter_enabled` (False), `cloud_rewriter_provider` (openai), `cloud_rewriter_openai_model` (gpt-4o-mini), `cloud_rewriter_anthropic_model`, `cloud_rewriter_base_url`, `cloud_rewriter_custom_model`, `cloud_rewriter_api_key` (+ `anthropic_api_key`, см. комментарий :1300). Новое: `cloud_spend_cap_usd_monthly` (дефолт **1.0**; **0 = запрещено всё**, fail-closed).
- D3-рамка (решение владельца): summary only; Studio-DOWN only; privacy wins; лимит. Флаг остаётся False (включает владелец; НЕ в этой карточке).

---

### Task 1: Тесты cap (RED)

**Files:**
- Create: `KrabEar/tests/test_cloud_spend_cap.py`

- [ ] **Step 1: Написать тесты** (фикстура — настоящий `LLMRewriter`, как в существующем cloud-fallback тесте; патчится ТОЛЬКО реально существующий `backend.cloud_rewriter.cloud_summarize`, БЕЗ `create=True`; audit-логгер мокается):

```python
"""F2: месячный spend-cap облачного фоллбэка summary (D3-узко).

Шов уже в проде (_maybe_apply_cloud_summarize); здесь только cap.
Сеть запрещена: cloud_summarize всегда мок.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriteResult, LLMRewriter


def _rewriter(settings: dict, spend_dir: Path | str | None) -> LLMRewriter:
    rw = LLMRewriter(base_url="http://127.0.0.1:1", api_key="", model="test-model")
    rw._settings_getter = lambda k, d=None: settings.get(k, d)
    rw._spend_dir = spend_dir
    return rw


def _failed_result() -> LLMRewriteResult:
    # timeout ∈ _STUDIO_UNAVAILABLE_REASONS → is_studio_unavailable True.
    return LLMRewriteResult(ok=False, text=None, fallback_reason="timeout", latency_ms=None)


def _no_audit():
    return patch("backend.llm_rewriter.get_privacy_audit_logger", create=False)


class CloudSpendCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.spend_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _base_settings(self, **over):
        cfg = {"privacy_mode_enabled": False, "cloud_rewriter_enabled": True,
               "cloud_rewriter_provider": "openai", "cloud_spend_cap_usd_monthly": 100.0}
        cfg.update(over)
        return cfg

    def test_cap_exceeded_blocks_cloud(self) -> None:
        """RED-критерий №1: лимит 0 → cloud_summarize НЕ вызывается."""
        rw = _rewriter(self._base_settings(cloud_spend_cap_usd_monthly=0.0), self.spend_dir)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 1", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_cap_allows_and_records_spend(self) -> None:
        """RED-критерий №2: лимит есть → вызов + запись трат в файл."""
        rw = _rewriter(self._base_settings(), self.spend_dir)
        with patch("backend.cloud_rewriter.cloud_summarize", return_value="облачное саммари") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 2", 3)
        cloud.assert_called_once()
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "облачное саммари")
        from backend import cloud_rewriter as cr
        spent = cr.read_spend_usd(self.spend_dir, cr.current_month_key())
        self.assertGreater(spent, 0.0)

    def test_no_spend_dir_blocks_cloud(self) -> None:
        """_spend_dir None → fail-closed (guard, зелёный и до, и после — фиксирует инвариант)."""
        rw = _rewriter(self._base_settings(), None)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 3", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_privacy_mode_blocks_before_cap(self) -> None:
        """privacy ON → облака нет (guard, был и будет)."""
        rw = _rewriter(self._base_settings(privacy_mode_enabled=True), self.spend_dir)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(_failed_result(), "тестовый текст 4", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_empty_catalog_never_reaches_cap(self) -> None:
        """Пустой каталог ≠ DOWN (guard, был и будет)."""
        rw = _rewriter(self._base_settings(), self.spend_dir)
        studio_empty = LLMRewriteResult(ok=False, text=None, fallback_reason="studio_empty_no_autoload",
                                        latency_ms=None)
        with patch("backend.cloud_rewriter.cloud_summarize") as cloud, _no_audit():
            out = rw._maybe_apply_cloud_summarize(studio_empty, "тестовый текст 5", 3)
        cloud.assert_not_called()
        self.assertFalse(out.ok)

    def test_spend_helpers_roundtrip(self) -> None:
        """read/add атомарно считают месяц (хелперы новые: pre-impl это ERROR — ожидаемо, см. ниже)."""
        from backend import cloud_rewriter as cr
        month = cr.current_month_key()
        self.assertEqual(cr.read_spend_usd(self.spend_dir, month), 0.0)
        cr.add_spend_usd(self.spend_dir, month, 0.001)
        cr.add_spend_usd(self.spend_dir, month, 0.002)
        self.assertAlmostEqual(cr.read_spend_usd(self.spend_dir, month), 0.003)


if __name__ == "__main__":
    unittest.main()
```

🔴 Честная классификация RED (зафиксировать в отчёте, не маскировать): тесты `test_cap_exceeded_blocks_cloud` и `test_cap_allows_and_records_spend` — настоящий RED (FAIL: до имплементации облако вызывается / файла нет); `test_no_spend_dir/privacy/empty_catalog` — guard (зелёные до и после); `test_spend_helpers_roundtrip` — ERROR до имплементации (имён нет), GREEN после. Перед прогоном сверить: `_maybe_apply_cloud_summarize` — метод инстанса (не static), `LLMRewriteResult` импортируется из `backend.llm_rewriter`, `get_privacy_audit_logger` патчится по пути `backend.llm_rewriter.get_privacy_audit_logger` (импорт внутри метода — сверить, что имя резолвится оттуда; если нет — править ТЕСТ под код).

- [ ] **Step 2: RED**:

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_cloud_spend_cap.py -v -p no:cacheprovider
```
Ожидаемо: 2 FAIL (cap-тесты) + 1 ERROR (helpers) + 3 guard-pass. Иная картина (импорты, конструктор) — **стоп** координатору.

### Task 2: Реализация (GREEN)

**Files:**
- Modify: `KrabEar/backend/cloud_rewriter.py` (хелперы + тарифы)
- Modify: `KrabEar/backend/llm_rewriter.py` (cap-check + add_spend в шве)
- Modify: `KrabEar/core/config.py` (ключ `cloud_spend_cap_usd_monthly`, дефолт 1.0)
- Modify: `KrabEar/backend/service.py` (1 строка: `_spend_dir` рядом с `:552`)
- Test: `KrabEar/tests/test_cloud_spend_cap.py`

- [ ] **Step 1: Хелперы в `cloud_rewriter.py`** — `current_month_key() -> "%Y-%m" (локальное время, комментарий)`; `read_spend_usd(data_dir, month) -> float` (нет файла/битый → 0.0, не исключение); `add_spend_usd(data_dir, month, usd)` (tmp+`os.replace`, округление до 6 знаков; в файл — ТОЛЬКО цифры); `estimate_summarize_usd(in_text, out_text, provider, model) -> float` (токены ≈ `len/4`, тарифная таблица `{(provider, model): (in_usd_per_1m, out_usd_per_1m)}`: gpt-4o-mini известный тариф + комментарий «приближённо, сверить по первому счёту владельца»; unknown → консервативный blended $1.00/1M (cap срабатывает раньше — fail-closed); custom/self-hosted → 0.0 + комментарий); `spend_allowed(cap, spent, est) -> bool` (`cap <= 0 → False`; иначе `spent + est <= cap`).
- [ ] **Step 2: Шов в `_maybe_apply_cloud_summarize`** — ПОСЛЕ существующих `allowed` + `is_studio_unavailable` проверок, ДО вызова `cloud_summarize`: прочитать cap (`getter("cloud_spend_cap_usd_monthly", 1.0)`, float(); исключение getter → считать 0 = блок); `spend_dir = self._spend_dir` (None → `return result` — fail-closed); `est = estimate...`; `if not spend_allowed: return result`; ПОСЛЕ успеха (`cloud_text` непустой, перед audit-блоком): `add_spend_usd(spend_dir, current_month_key(), estimate(..., cloud_text))` в try/except-pass (учёт не должен ронять результат). 🔴 Вторая облачная ветка запрещена — только эти вставки, сигнатуры и остальная логика метода не меняются.
- [ ] **Step 3: Проводка** — `service.py` рядом с `:552`: `self._llm_rewriter._spend_dir = store.data_dir` (проверить `store` в скоупе — строки :474+; если имя другое — взять то же выражение, что у соседних сервисов). Больше нигде `LLMRewriter(` в проде нет (проверено `rg`) — других мест проводки не требуется.
- [ ] **Step 4: Существующие тесты** — ЕДИНСТВЕННОЕ разрешённое изменение старых файлов: в `test_studio_unavailable_cloud_fallback_2026_09_05.py` (и siblings, если тоже упадут — перечислить в отчёте) добавить в setUp `_spend_dir = tmp_path` + cap высокий (`cloud_spend_cap_usd_monthly: 100.0` в их settings-фикстуру). Любая другая правка старых тестов — **стоп** координатору.
- [ ] **Step 5: GREEN** — команда Task 1 Step 2. Ожидаемо: 6 passed.
- [ ] **Step 6: Регрессия** — `test_studio_unavailable_cloud_fallback_2026_09_05.py`, `test_llm_rewriter_summarize*.py`, `test_cloud_rewriter*.py` (найти точные имена `rg -ln`). Ожидаемо: зелёные.
- [ ] **Step 7: Гейт** — flake8 (max-120) изменённых + `scripts/pre_merge_py312_check.sh` на новый тест + `make audit-all` (сервисы тронуты).

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `feat/f2-cloud-summary`
- [ ] `git add` **явными путями** (только файлы Task 2 + setup-правки Step 4)
- [ ] Коммит: `feat(summary): месячный spend-cap облачного фоллбэка (D3)`
- [ ] PR в `codex/krab-ear-v2` с пометкой «privacy-путь: нужен gate-security»; НЕ мержить.

## Definition of Done

- 2 RED→GREEN + 3 guard + 1 helper (ERROR→GREEN); регрессия зелёная; audit-all зелёный.
- В диффе: НЕТ второй облачной ветки; НЕТ правок `engine.py`/STT-путей/`_generate_summary`; НЕТ текстов/ключей в spend-файле (проверить `rg -n 'sk-|text'` по диффу spend-кода); флаг `cloud_rewriter_enabled` остаётся False.
- Деплой/включение — НЕ в этой карточке.

## Вне scope (записать в отчёт, не чинить)

- Включение флага / ввод API-ключа / сверка тарифа по счёту — владелец.
- Точный учёт токенов провайдера (нет в ответах API) — оценка len/4.
- Cloud для диктовки — запрещено D3.
