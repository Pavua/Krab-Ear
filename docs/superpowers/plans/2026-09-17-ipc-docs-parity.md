# Паритет IPC-документации с диспетчером — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** каждый ключ `_build_dispatch_table` задокументирован в `docs/IPC_API_REFERENCE.md`, счётчики в `CLAUDE.md` верны, commerce — ноль дрейфа между кодом и доками.

**Architecture:** тест паритета разбирает `service.py` через AST (таблица dispatch — `Return` внутри `_build_dispatch_table`) и имена секций `### \`метод\`` в доке, считает разницу в обе стороны. 🔴 Ловушка (найдена координатором 17.09): `clear_privacy_audit_log` задокументирован, но УДАЛЁН из dispatch намеренно (W957 SECURITY, `service.py:3167`) — его нельзя «чинить» возвратом в dispatch, только пометить в доке. 🔴 Числа в карточке НЕ хардкодить итогом (360/50 протухают) — тест проверяет ОТНОШЕНИЯ (пустые разницы), а не тоталы.

**Tech Stack:** Python `ast`, `re`, `unittest`. Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/ipc-docs-parity`, ветка `docs/ipc-docs-parity`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не менять `service.py` и любой прод-код** (только `docs/IPC_API_REFERENCE.md`, тест, 1 строка `CLAUDE.md`); **не возвращать `clear_privacy_audit_log` в dispatch**.

---

## Проверенные факты (координатор, 17.09)

- Dispatch: один `_build_dispatch_table`, таблица — `Return` верхнего уровня, 360 ключей (17.09; тотал в тест НЕ вшивать).
- Док: 329 строк `^### \`` (17.09), из них 311 чисто-идентификаторных IPC-имён; остальные — секции с дефисами/путями (не IPC, не трогать).
- Разница на 17.09: 50 ключей без документации (список ниже — ориентир, НЕ догма; тест пересчитает сам), 1 документированный без dispatch (`clear_privacy_audit_log` — намеренно удалён, W957).
- Формат записи дока (прецедент `### ping`): строка в категориальной таблице `| \`метод\` | Описание |` + секция `### \`метод\`` со строкой маршрутизации `(service.py → <модуль>.py)`, описанием и `Returns:`.
- `CLAUDE.md`: строка «handler count now **360**» (2026-07-01, с методикой — оставить) и «Счётчик хендлеров: **365**» (2026-07-19 — обновить на 360); других счётчиков нет (проверено `rg`, перепроверить в Task 3).
- Ориентировочный список 50 (проверить тестом, сгруппировать по префиксу dispatch-значения `self._<сервис>.`): `add_abbreviation`, `add_normalization_profile`, `add_phonetic_entry`, `add_text_snippet`, `analyze_speech_pace`, `apply_normalization_profile`, `bulk_reprocess_cancel`, `bulk_reprocess_start`, `bulk_reprocess_status`, `call_intervene`, `call_resume_bot`, `call_start`, `clear_translation_cache`, `clear_unavailable_models`, `delete_speaker_fingerprint`, `estimate_batch_cost`, `export_selected_items`, `get_audit_log`, `get_auto_backup_status`, `get_auto_glossary`, `get_calendar_link`, `get_daily_insight`, `get_disk_status`, `get_encryption_status`, `get_export_schedule_status`, `get_meeting_report`, `get_memory_ledger`, `get_never_played`, `get_speaker_statistics`, `get_storage_breakdown`, `get_voice_gateway_credential`, `link_to_calendar_event`, `list_phonetic_entries`, `list_speaker_fingerprints`, `list_stt_engines`, `list_text_snippets`, `list_voice_commands`, `merge_recordings`, `preview_merge`, `purge_all_data`, `refresh_auto_glossary`, `register_speaker`, `remove_normalization_profile`, `remove_phonetic_entry`, `remove_text_snippet`, `rename_collection`, `rollback_migration`, `search_by_calendar_event`, `semantic_search_reset`, `set_history_encryption`.

---

### Task 1: Тест паритета (RED)

**Files:**
- Create: `KrabEar/tests/test_ipc_docs_parity.py`

- [ ] **Step 1: Написать тест**

```python
"""Паритет IPC-документации с диспетчером (волна 0.4).

Каждый ключ _build_dispatch_table обязан быть задокументирован в
docs/IPC_API_REFERENCE.md. Исключение: clear_privacy_audit_log — намеренно
удалён из dispatch (W957 SECURITY, service.py:3167), в доке обязан нести
маркер удаления, а не молчать.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "KrabEar" / "backend" / "service.py"
DOC = REPO / "docs" / "IPC_API_REFERENCE.md"

# Документирован, но НЕ в dispatch — и это правильно (возврат запрещён).
KNOWN_REMOVED = {"clear_privacy_audit_log"}
REMOVED_MARKER = "намеренно удалён"


def dispatch_keys() -> set[str]:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    builders = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_build_dispatch_table"
    ]
    assert len(builders) == 1, "ожидался ровно один _build_dispatch_table"
    returns = [n for n in ast.walk(builders[0]) if isinstance(n, ast.Return)]
    tables = [
        n.value for n in returns
        if isinstance(n.value, ast.Dict)
        and sum(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in n.value.keys) > 50
    ]
    assert len(tables) == 1, "таблица dispatch не найдена"
    return {k.value for k in tables[0].keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def documented_names() -> set[str]:
    return set(re.findall(r"^### `([A-Za-z0-9_]+)`", DOC.read_text(encoding="utf-8"), re.M))


class IpcDocsParityTests(unittest.TestCase):
    def test_every_dispatched_method_is_documented(self) -> None:
        missing = sorted(dispatch_keys() - documented_names())
        self.assertEqual(missing, [])

    def test_every_documented_name_exists_or_is_marked_removed(self) -> None:
        extra = sorted(documented_names() - dispatch_keys())
        self.assertEqual(set(extra) - KNOWN_REMOVED, set())
        text = DOC.read_text(encoding="utf-8")
        for name in sorted(set(extra) & KNOWN_REMOVED):
            section = text.split(f"### `{name}`", 1)[1].split("\n## ", 1)[0]
            self.assertIn(REMOVED_MARKER, section,
                          f"{name}: секция обязана нести маркер удаления")

    def test_counts_are_not_pinned_but_sane(self) -> None:
        # Гарды от протухания методики, НЕ от дрейфа тоталов: тоталы не вшиваем.
        self.assertGreater(len(dispatch_keys()), 300)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Прогнать — ждём RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_ipc_docs_parity.py -v -p no:cacheprovider
```

Ожидаемо: `test_every_dispatched_method_is_documented` — FAILED со списком ~50 имён; `test_every_documented_name_exists_or_is_marked_removed` — FAILED (маркера удаления нет). Если FAILED по другой причине (парсер не нашёл таблицу) — **стоп**, доложить координатору. Сверь список missing с ориентиром выше: расхождение ±5 — норма (код ушёл), расхождение в разы — стоп.

### Task 2: Дописать 50 записей + маркер + счётчик (GREEN)

**Files:**
- Modify: `docs/IPC_API_REFERENCE.md`
- Modify: `CLAUDE.md` (ровно 1 строка: «Счётчик хендлеров: **365**» → «Счётчик хендлеров: **360**»)

- [ ] **Step 1: Для каждого missing-ключа** добавить строку в категориальную таблицу + секцию `### \`метод\``. Маршрутизацию брать из значения dispatch (`"m": self._history.handle_x` → `(service.py → history_service.py)`); модуль сверить: `def handle_x` обязан существовать в файле модуля. Категорию выбирать по соседству: секция, где уже лежат методы того же `self._<сервис>.`; если такой нет — ближайшая по смыслу + пометка в отчёте. Описание — 1 строка по сигнатуре хендлера (params/returns из кода, не выдумывать).
- [ ] **Step 2: Секции `### \`clear_privacy_audit_log\``** добавить первым абзацем:
  `> 🔴 Намеренно удалён из IPC dispatch (W957 SECURITY, service.py:3167): существует только как внутренний хендлер, по сокету недоступен. Не добавлять обратно.`
- [ ] **Step 3: `CLAUDE.md`** — заменить `**365**` на `**360**` в строке B3 brain-lease; затем `rg -n '\*\*(36[0-9]|37[0-9])\*\*|handler count now' CLAUDE.md` — обязаны остаться только согласованные 360.
- [ ] **Step 4: GREEN** — та же команда, что в Task 1 Step 2. Ожидаемо: `3 passed`.
- [ ] **Step 5: Гейт**

```bash
python -m flake8 --max-line-length=120 KrabEar/tests/test_ipc_docs_parity.py
scripts/pre_merge_py312_check.sh KrabEar/tests/test_ipc_docs_parity.py
```

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `docs/ipc-docs-parity`
- [ ] `git add` **явными путями**: `KrabEar/tests/test_ipc_docs_parity.py`, `docs/IPC_API_REFERENCE.md`, `CLAUDE.md`
- [ ] Коммит: `docs(ipc): паритет документации с диспетчером + гард`
- [ ] PR в `codex/krab-ear-v2`; НЕ мержить (мержит координатор после гейта).

## Definition of Done

- 3 теста RED→GREEN; `git diff --stat` показывает только 3 файла (прод-код не тронут — проверить `git diff --name-only -- KrabEar/backend/` пуст).
- Ни одного вшитого тотала (360/50) в тесте — только отношения.

## Вне scope (записать в отчёт, не чинить)

Секции дока с дефисами/путями (не-IPC `###`-записи) — не трогать. Расхождения описаний/примеров с кодом (устаревшие Returns) — только списком в отчёт, отдельной волной.
