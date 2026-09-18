# F2b: харденинг spend-cap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** закрыть 5 находок adversarial-ревью F2 (#2036): гонка учёта (MED-1), `Infinity` в cap (MED-2), fail-open на битый файл (LOW-1), symlink через фиксированный `.tmp` (LOW-2), дефолт cap=1.0 при сбое чтения (LOW-3) + тесты на эти сценарии (LOW-4).

**Architecture:** учёт трат переводится на **резерв до вызова**: новый `reserve_spend_usd(data_dir, month, est, cap) -> bool` — под модульным `threading.Lock` читает (strict-валидированный) файл, проверяет `spent + est <= cap`, атомарно пишет резерв (`core/atomic_io.atomic_write_text` — уникальный mkstemp, без фиксированного `.tmp`) и возвращает вердикт. Шов в `_maybe_apply_cloud_summarize`: `reserve → cloud_summarize → на успех reconcile (actual − est_bound ≤ 0), на провал release (−est_bound)`. Битый/невалидный файл = «spent unknown» → резерв запрещён (fail-closed), предупреждение в лог. Cap клампится валидатором настроек, дефолт при сбое чтения — `0.0`.

**Tech Stack:** Python `unittest` + `threading` (реальные потоки в тесте — воспроизведение гонки), `core/atomic_io.atomic_write_text` (уже в репо). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/f2b-spend-hardening`, ветка `fix/f2b-spend-hardening`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не трогать privacy-гейты и сам факт вызова облака** (только учёт), не менять формулу `estimate_summarize_usd`, не менять схему файла (`{"YYYY-MM": usd}`), новых IPC нет, `cloud_rewriter_enabled` остаётся False; в логах — без текстов и ключей.

---

## Проверенные факты (adversarial-ревью + координатор, 18.09)

- Находки (подтверждены воспроизведением): MED-1 гонка — 8 потоков × 50 add по $0.0001 → учтено 0.0005 вместо 0.04, большинство записей молча теряется (`llm_rewriter.py:473-474 except: pass`); MED-2 `float("1e999")` → `inf` → `spend_allowed(inf,...)` == True; LOW-1 битый файл → `{}` → 0.0 → перезапись без истории; LOW-2 фиксированный `cloud_spend.json.tmp` → symlink write-through (проверено: victim-файл перезаписан); LOW-3 `getter("cloud_spend_cap_usd_monthly", 1.0)` — при сбое чтения стора разрешает до $1.
- Приватность/scope — SAFE (ревью): единственный `cloud_summarize` — шов; privacy первым, fail-closed; диктовка/STT не тронуты.
- Готовое: `core/atomic_io.py:15` `atomic_write_text(path, text, *, encoding="utf-8")` — unique mkstemp + fsync + os.replace (докстрока явно описывает устранение shared-`.tmp` гонки). Валидатор: `settings_validator.py:98` `_RANGE_FIELDS`, sibling `call_budget_usd:127` = `(0.0, 1000.0, 2.0, float)`; non-finite режется `:358+`.
- Текущий код: `cloud_rewriter.py:190-300` (`_read_spend_map/read_spend_usd/add_spend_usd/estimate_summarize_usd/spend_allowed`); шов `llm_rewriter.py:409-475` (импорт хелперов внутри метода, pre-check `read_spend_usd`+`spend_allowed`, post-success `add_spend_usd(actual)`).
- Тесты F2: `test_cloud_spend_cap.py` (6 шт., cap-логика исполняется по-настоящему, мок только `cloud_summarize`).

---

### Task 1: Тесты (RED)

**Files:**
- Create: `KrabEar/tests/test_cloud_spend_cap_hardening.py`

- [ ] **Step 1: Тесты** (потоки — реальные; файл — tmp; `cloud_summarize` — мок):

```python
"""F2b: харденинг spend-cap (гонка, inf-cap, битый файл, reconcile).

Ревью F2 (#2036): MED-1 read-modify-write гонка, MED-2 inf-обход,
LOW-1 fail-open на битый файл, LOW-3 дефолт при сбое чтения.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import cloud_rewriter as cr  # noqa: E402


class ReserveSpendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.month = "2026-09"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_reserve_no_lost_updates(self) -> None:
        """MED-1: 8 потоков × 50 резервов — ничего не теряется."""
        est = 0.0001
        cap = 100.0
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(50):
                ok = cr.reserve_spend_usd(self.dir, self.month, est, cap)
                with lock:
                    results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 400)
        self.assertTrue(all(results), "при cap=100 все резервы должны пройти")
        spent = cr.read_spend_usd(self.dir, self.month)
        self.assertAlmostEqual(spent, 400 * est, places=9)

    def test_concurrent_reserve_never_exceeds_cap(self) -> None:
        """MED-1: суммарный резерв не превышает cap + один шаг."""
        est = 0.01
        cap = 0.05  # ровно 5 успешных резервов
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            ok = cr.reserve_spend_usd(self.dir, self.month, est, cap)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        granted = sum(1 for r in results if r)
        self.assertEqual(granted, 5, "ровно cap/est резервов, без обхода гонкой")
        self.assertAlmostEqual(cr.read_spend_usd(self.dir, self.month), 5 * est, places=6)

    def test_corrupt_file_denies_and_is_not_silently_reset(self) -> None:
        """LOW-1: битый файл → резерв запрещён (fail-closed), файл не перезаписан молча."""
        path = self.dir / "cloud_spend.json"
        path.write_text("{не json", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        self.assertFalse(cr.reserve_spend_usd(self.dir, self.month, 0.001, 100.0))
        self.assertEqual(path.read_text(encoding="utf-8"), before, "битый файл не перезаписан")
        self.assertFalse(cr.reserve_spend_usd(self.dir, self.month, 0.001, 100.0))

    def test_non_finite_cap_is_rejected(self) -> None:
        """MED-2: defense-in-depth в резерве — inf/NaN cap запрещены."""
        for bad_cap in (float("inf"), float("nan"), float("-inf")):
            with self.subTest(cap=bad_cap):
                self.assertFalse(cr.reserve_spend_usd(self.dir, self.month, 0.001, bad_cap))

    def test_validator_range_registered(self) -> None:
        """MED-2: ключ в _RANGE_FIELDS с клампом (non-finite режет валидатор)."""
        from backend.settings_validator import _RANGE_FIELDS
        self.assertIn("cloud_spend_cap_usd_monthly", _RANGE_FIELDS)
        lo, hi, default, _ = _RANGE_FIELDS["cloud_spend_cap_usd_monthly"]
        self.assertEqual((lo, hi), (0.0, 1000.0))
        self.assertEqual(default, 1.0)

    def test_no_fixed_tmp_name_left_after_write(self) -> None:
        """LOW-2: unique-mkstemp (atomic_io) — фиксированного .tmp в каталоге нет."""
        self.assertTrue(cr.reserve_spend_usd(self.dir, self.month, 0.002, 100.0))
        self.assertFalse((self.dir / "cloud_spend.json.tmp").exists())


class SeamReservationTests(unittest.TestCase):
    """Шов: reserve → вызов → reconcile/release (реальный LLMRewriter, мок cloud_summarize)."""

    def _rewriter(self, spend_dir: Path):
        from backend.llm_rewriter import LLMRewriter
        rw = LLMRewriter(base_url="http://127.0.0.1:1", api_key="", model="test-model")
        rw._settings_getter = lambda k, d=None: {
            "privacy_mode_enabled": False,
            "cloud_rewriter_enabled": True,
            "cloud_rewriter_provider": "openai",
            "cloud_spend_cap_usd_monthly": 100.0,
        }.get(k, d)
        rw._spend_dir = spend_dir
        return rw

    def _failed(self):
        from backend.llm_rewriter import LLMRewriteResult
        return LLMRewriteResult(ok=False, text=None, fallback_reason="timeout", latency_ms=None)

    def test_failed_cloud_releases_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rw = self._rewriter(Path(d))
            with patch("backend.cloud_rewriter.cloud_summarize", return_value=None), \
                 patch("backend.privacy_audit.get_privacy_audit_logger"):
                out = rw._maybe_apply_cloud_summarize(self._failed(), "тестовый текст 1", 3)
            self.assertFalse(out.ok)
            self.assertAlmostEqual(cr.read_spend_usd(Path(d), cr.current_month_key()), 0.0, places=6)

    def test_success_reconciles_to_actual_not_bound(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rw = self._rewriter(Path(d))
            with patch("backend.cloud_rewriter.cloud_summarize", return_value="ок"), \
                 patch("backend.privacy_audit.get_privacy_audit_logger"):
                out = rw._maybe_apply_cloud_summarize(self._failed(), "тестовый текст 2 подлиннее", 3)
            self.assertTrue(out.ok)
            month = cr.current_month_key()
            spent = cr.read_spend_usd(Path(d), month)
            bound = cr.estimate_summarize_usd("тестовый текст 2 подлиннее", "тестовый текст 2 подлиннее",
                                              "openai", "gpt-4o-mini")
            actual = cr.estimate_summarize_usd("тестовый текст 2 подлиннее", "ок", "openai", "gpt-4o-mini")
            self.assertAlmostEqual(spent, actual, places=6)
            self.assertLess(spent, bound)


if __name__ == "__main__":
    unittest.main()
```

🔴 Перед прогоном сверить `rg`: `estimate_summarize_usd` — точная сигнатура; `LLMRewriter(base_url=, api_key=, model=)` — конструктор (прецедент `test_cloud_spend_cap.py`); `_maybe_apply_cloud_summarize(result, text, max_sentences)` — метод инстанса. Несовпадение — править ТЕСТ под код.

- [ ] **Step 2: RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_cloud_spend_cap_hardening.py -v -p no:cacheprovider
```
Ожидаемо: FAIL/ERROR по отсутствию `reserve_spend_usd` / ключа в `_RANGE_FIELDS`; `test_failed_cloud_releases_reservation` и `test_success_reconciles_to_actual_not_bound` могут ПАДАТЬ по сути (сегодняшний шов: reserve нет, release нет) либо ERROR — классификацию каждого теста в отчёт. Если падает фикстура/импорт — стоп координатору.

### Task 2: Реализация (GREEN)

**Files:**
- Modify: `KrabEar/backend/cloud_rewriter.py`
- Modify: `KrabEar/backend/llm_rewriter.py`
- Modify: `KrabEar/backend/settings_validator.py`
- Test: `KrabEar/tests/test_cloud_spend_cap_hardening.py`

- [ ] **Step 1: Хелперы** — в `cloud_rewriter.py`:
  - `import math`, `import threading`, `from core.atomic_io import atomic_write_text` (сверить путь/импорт-конвенцию модуля).
  - `_SPEND_LOCK = threading.Lock()` рядом с `_CLOUD_SPEND_FILENAME`.
  - `_read_spend_map_strict(data_dir) -> dict | None`: читает файл; нет файла → `{}`; не-dict / битый JSON / невалидный ключ (не `^\d{4}-\d{2}$`) / значение не finite или < 0 → `None` + `logger.warning` (без содержимого).
  - `reserve_spend_usd(data_dir, month, est, cap) -> bool`: guard `math.isfinite(cap) and cap > 0` (иначе False); под `_SPEND_LOCK`: `raw = _read_spend_map_strict(...)`; `None` → False; `spent = raw.get(month, 0.0)`; `if not spend_allowed(cap, spent, est): return False`; `raw[month] = round(spent + max(0.0, float(est)), 9)`; `atomic_write_text(path, json.dumps(raw))`; True. Исключение записи → лог + False. 🔴 Округление — 9 знаков: суммы summary микродолларовые, 6 знаков теряют reconcile (пример: bound 4.875e-6).
  - `add_spend_usd` — переписать на `_SPEND_LOCK` + `atomic_write_text` (дельта может быть отрицательной; кламп итога `max(0.0, round(..., 9))`; битый файл → лог + no-op, НЕ перезапись).
  - `read_spend_usd` — оставить (используется тестами/статусом); на strict-`None` вернуть 0.0 (чтение не гейт).
- [ ] **Step 2: Шов** (`llm_rewriter.py:409-475`): импорт заменить на `reserve_spend_usd` (+ `add_spend_usd`, `current_month_key`, `estimate_summarize_usd`); pre-check → `if not reserve_spend_usd(spend_dir, current_month_key(), est_bound, cap): return result`; на провале облака/пустом ответе/исключении — release `add_spend_usd(spend_dir, month, -est_bound)` (в try/except-pass); на успехе — reconcile `add_spend_usd(spend_dir, month, actual - est_bound)`. 🔴 `getter(..., 0.0)` вместо 1.0 (LOW-3). Privacy-порядок и вызов `cloud_summarize` НЕ менять.
- [ ] **Step 3: Валидатор** — `settings_validator.py` рядом с `call_budget_usd:127`: `"cloud_spend_cap_usd_monthly": (0.0, 1000.0, 1.0, float),`.
- [ ] **Step 4: GREEN** — команда Task 1 Step 2. Ожидаемо: все зелёные.
- [ ] **Step 5: Регрессия** — `test_cloud_spend_cap.py`, `test_studio_unavailable_cloud_fallback_2026_09_05.py`, `test_cloud_rewriter.py`, `test_llm_rewriter_summarize.py`, `tests/test_atomic_io*.py` (найти `rg -ln 'atomic_io' KrabEar/tests/`). Любая правка старых тестов — стоп координатору.
- [ ] **Step 6: Гейт** — flake8 (max-120) изменённых; `scripts/pre_merge_py312_check.sh` на новый тест; `make audit-all`.

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `fix/f2b-spend-hardening`
- [ ] `git add` **явными путями** (3 файла Task 2 + новый тест)
- [ ] Коммит: `fix(summary): харденинг spend-cap — атомарный резерв, inf-кламп, fail-closed (F2b)`
- [ ] PR в `codex/krab-ear-v2`; НЕ мержить.

## Definition of Done

- Все тесты RED→GREEN; регрессия зелёная; audit-all зелёный.
- Гонка не воспроизводится (тест с потоками), `inf`/`NaN` cap запрещены и в валидаторе, и в резерве; битый файл → deny + не перезаписывается; фиксированного `.tmp` нет; сбой облака → release (в тесте spent==0).
- Privacy/scope инварианты: единственный `cloud_summarize`, privacy первым, диктовка не тронута, флаг False.
- В отчёт: RED-классификация каждого теста; подтверждение «фиксированного .tmp нет» (rg по диффу).

## Вне scope (записать в отчёт, не чинить)

- Межпроцессная сериализация spend-файла (одного процесса достаточно; flock — если появится второй writer).
- Реальная сверка тарифа по счёту — владелец.
- Включение `cloud_rewriter_enabled` — владелец.
