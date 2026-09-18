# R1: табло надёжности (ежедневный сканер + панель) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ежедневный локальный скан сводит сигналы надёжности Ear за 24 ч/7 дней в JSON-снимки, а пульт `:8777` показывает их живой карточкой с честными `unknown`.

**Architecture:** новый stdlib-only скрипт `scripts/reliability_scan.py` (его же запускает launchd раз в сутки) считает 7 сигналов из существующих источников (логи, `audit_*.ndjson`, `forensics/`, `rescue/`), пишет снимок `~/.local/share/krab-ear/reliability/YYYY-MM-DD.json` и склеенный `latest.json` (24 ч) + `summary_7d.json` — потому что ретеншен источников короче 7 дней (логи 5 МБ ×3, forensics — 5 каталогов, audit — ровно 7 дней); копилка снимков и даёт недельное окно. `scripts/status_dashboard.py` получает `collect_reliability()`, читающий `latest.json`; снимок старше 26 ч → `status: "unknown"` (никогда не «зелёный по умолчанию»). Launchd-обвязка — по прецеденту e2e-smoke (`template + installer + contract-тест`).

**Tech Stack:** Python 3 (stdlib only: `re`, `json`, `pathlib`, `datetime`), launchd plist template, тесты `unittest` + `importlib` (прецеденты: `test_record_golden_script.py`, `test_ear_smoke_launchd_2026_09_14.py`). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/r1-reliability`, ветка `feat/r1-reliability`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не трогать прод-бэкенд/REST/агент и их launchd-юниты**; новый job — только `ai.krab.ear.reliability` (scan-only, никаких команд управления); `status_dashboard.py` запускается системным python — никаких импортов из `KrabEar/`; PII в снимки не тащить (только счётчики/латентности; никаких текстов, путей с именами, содержимого forensics-трейлов).

---

## Проверенные факты (координатор + разведка, 18.09, file:line)

**Пульта (`scripts/status_dashboard.py`, :8777 через `ai.krab.ear.dashboard.plist`):**
- Доктрина: «никогда не выдавать отсутствие данных за успех»; у разделов `status: ok/warn/fail/unknown`.
- Точки расширения: `collect_*()` возвращают dict (:47–:199); рендер `render_html` и секции-карточки :382–394; CLI `--json`/`--serve` :431–436 (флага `--once` нет — так работает и так ок).
- 🔴 Ловушка: словарь собирается в ДВУХ местах — `Handler.do_GET` :407–413 и `main()` :441–450; новый ключ вставлять в оба (иначе `--json` и `--serve` разойдутся).
- Плагинов нет; тестов у пульта нет — контракт нового коллектора закрываем тестом.

**Сигналы (источник → точный паттерн):**
| Сигнал | Источник | Паттерн/метод | Замечания |
|---|---|---|---|
| STT-провал диктовки | `~/Library/Application Support/KrabEar/backend.log*` (ротация 5 МБ ×3) | строка `Критическая ошибка распознавания` (`core/engine.py:1927–1937`, логгер `KrabEar.Engine`) | **запись = 1 на диктовку**, НЕ «Все доступные STT-движки…» (это внутри traceback). Дубли на запись: `phase_c: STT crashed` (`recording_core_service.py:3144`), `stt.failed` SSE — считать только главный паттерн |
| GigaAM chunk loss | `backend.log*` | `gigaam-mlx потерял` (сообщение `GigaAMMLXChunkLoss`, `pipeline/stt_gigaam_mlx.py:200–205`) | record-level = 1 попытка движка; построчный `вернулся пустым` (`:194–198`) НЕ считать (это чанки) |
| Зависшие IPC | `backend.log*` | `handle_request завис дольше` (`backend/ipc_server.py:374–379`) | audit-NDJSON их не видит — только этот лог |
| 401 моста | `logs/krab-ear-rest.err.log` | `неверный bridge-токен` (`backend/rest_server.py:576`) | локальное время в `asctime`; считать по дате |
| Восстановленные записи | `<data_dir>/rescue/*.meta.json` | mtime в окне | collection «Восстановленные записи» в `collections.json` — только успешные; meta-файлы — честнее |
| Нечистые смерти | `<data_dir>/forensics/*/` | число каталогов по mtime | 🔴 ретеншен `_MAX_RETAINED_DIRS=5` (`shutdown_forensics.py:43`) + rate-limit `_COLLECT_MIN_GAP_SEC=900` (:52) — 7-дневный счёт обрезается, писать caveat в снимок; JSON-файлы PII не содержат, `own_logs_tail.txt` — НЕ читать |
| ping p50/p99 | `audit_<UTC-дата>.ndjson` (7 дней, `audit_logger.py:95`) | `method=="ping"` → `duration_ms` | `ts` — **ISO-строка** (`audit_logger.py:126`), не epoch: парсить `datetime.fromisoformat`. Это время handle_request (не RTT) и только успешные вызовы — писать limitation в снимок |

**Что НЕ делать (уже есть):** `scripts/backend_log_digest.py` (расширять его паттерны — отдельная волна, сюда не тащить), `metrics_collector.py` (in-memory STT-метрики), `health_checker.py`, `collect_ux_telemetry.py`, `history_health_report.py`, внешний `system-health-snapshot` (.openclaw — машинный уровень). **«Длительность диктовки к длине речи» из истории не построить** (в `history.ndjson` нет `speech_duration`; `audio_path` в последних 200 записях пуст) — в R1 это честно `unknown`, будущая инструментация.

**Launchd-прецедент:** `KrabEar/launchagents/ai.krab.ear.e2e-smoke.plist.template` + `scripts/install_ear_e2e_smoke.command` (render `__HOME__`/`__PROJECT_ROOT__` → `plutil -lint` → bootout → bootstrap → verify) + контракт-тест `KrabEar/tests/test_ear_smoke_launchd_2026_09_14.py`. Расписание — `StartCalendarInterval` (как system-health-snapshot: 06:00).

**Хранилище снимков:** `~/.local/share/krab-ear/reliability/` (ops-состояние, не data_dir с пользовательскими данными): `YYYY-MM-DD.json`, `latest.json`, `summary_7d.json`.

---

### Task 1: Сканер сигналов (TDD)

**Files:**
- Create: `scripts/reliability_scan.py`
- Create: `KrabEar/tests/test_reliability_scan.py`

- [ ] **Step 1: Написать тесты (RED)** — синтетические фикстуры в `tempfile` для каждого сигнала; ядро тестов:

```python
"""R1: сканер надёжности — синтетические фикстуры на каждый сигнал.

Форматы источников (сверено):
- backend.log: `%(asctime)s [%(name)s] %(levelname)s: %(message)s`,
  asctime с МИЛЛИСЕКУНДАМИ через запятую: `2026-09-18 02:05:43,820`.
- rest.err.log: `%(asctime)s %(levelname)s %(name)s: %(message)s` (тоже `,мс`).
- audit_*.ndjson: `{"ts": "<ISO 8601 с offset>", "method", "success", "duration_ms"}`.

В фикстурах штампы ГЕНЕРИРУЮТСЯ от текущего времени (никаких зашитых дат:
тест обязан быть зелёным в любой день). `until` берётся ПОСЛЕ создания файлов.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "reliability_scan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reliability_scan_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _log_stamp(until: datetime, minutes_ago: int = 2) -> str:
    """Локальный штамп как в backend.log/rest err: 'YYYY-MM-DD HH:MM:SS,mmm'."""
    return (until - timedelta(minutes=minutes_ago)).astimezone().strftime("%Y-%m-%d %H:%M:%S,123")


class ScanSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        (self.root / "logs").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- STT critical -------------------------------------------------
    def test_stt_critical_counts_records_not_lines(self) -> None:
        until = datetime.now(timezone.utc)
        stamp = _log_stamp(until)
        log = self.data_dir / "backend.log"
        log.write_text(
            f"{stamp} [KrabEar.Engine] ERROR: Критическая ошибка распознавания\n"
            "Traceback (most recent call last):\n"
            "RuntimeError: Все доступные STT-движки вышли из строя\n"
            f"{stamp} [KrabEar.Engine] ERROR: Критическая ошибка распознавания\n",
            encoding="utf-8",
        )
        result = self.mod.scan_stt_critical([log], until - timedelta(hours=24), until)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["status"], "warn")  # ≥1 критический провал = warn

    def test_stt_critical_missing_log_is_unknown(self) -> None:
        until = datetime.now(timezone.utc)
        result = self.mod.scan_stt_critical([self.data_dir / "absent.log"], until - timedelta(hours=24), until)
        self.assertEqual(result["status"], "unknown")

    # --- GigaAM chunk loss -------------------------------------------
    def test_gigaam_chunk_loss_record_level(self) -> None:
        until = datetime.now(timezone.utc)
        stamp = _log_stamp(until)
        log = self.data_dir / "backend.log"
        log.write_text(
            f"{stamp} [KrabEar.GigaAMMLX] WARNING: кусок 1.0–2.0с звучит, но вернулся пустым — часть речи потеряна\n"
            f"{stamp} [KrabEar.Engine] WARNING: Модель gigaam не сработала: gigaam-mlx потерял 2 кусок(ов) из 5\n",
            encoding="utf-8",
        )
        result = self.mod.scan_gigaam_chunk_loss([log], until - timedelta(hours=24), until)
        self.assertEqual(result["records"], 1)  # один record-level, «вернулся пустым» не считается

    # --- handle_request hangs ----------------------------------------
    def test_handle_request_hangs_counts(self) -> None:
        until = datetime.now(timezone.utc)
        stamp = _log_stamp(until)
        log = self.data_dir / "backend.log"
        log.write_text(
            f"{stamp} [KrabEar.Backend.Service] ERROR: handle_request завис дольше 180с "
            "(method=stop_recording) — backstop-таймаут, слот освобождён, рабочий поток абандонен\n",
            encoding="utf-8",
        )
        result = self.mod.scan_handle_request_hangs([log], until - timedelta(hours=24), until)
        self.assertEqual(result["records"], 1)

    # --- bridge 401 ---------------------------------------------------
    def test_bridge_401_counts_only_lines_in_window(self) -> None:
        until = datetime.now(timezone.utc)
        stamp = _log_stamp(until, minutes_ago=5)
        old = _log_stamp(until, minutes_ago=3 * 24 * 60)
        log = self.root / "logs" / "krab-ear-rest.err.log"
        log.write_text(
            f"{stamp} WARNING KrabEar.REST: event_bridge: неверный bridge-токен\n"
            f"{old} WARNING KrabEar.REST: event_bridge: неверный bridge-токен\n",
            encoding="utf-8",
        )
        result = self.mod.scan_bridge_401([log], until - timedelta(hours=24), until)
        self.assertEqual(result["records"], 1)

    # --- rescue / forensics ------------------------------------------
    def test_rescue_and_forensics_count_dirs_by_mtime(self) -> None:
        rescue = self.data_dir / "rescue"
        rescue.mkdir()
        (rescue / "a.meta.json").write_text("{}", encoding="utf-8")
        forensics = self.data_dir / "forensics"
        (forensics / "20260918_010101_000001").mkdir(parents=True)
        until = datetime.now(timezone.utc)  # ПОСЛЕ создания файлов — окно их включает
        result_r = self.mod.scan_rescue_files(rescue, until - timedelta(hours=24), until)
        result_f = self.mod.scan_unclean_deaths(forensics, until - timedelta(hours=24), until)
        self.assertEqual(result_r["records"], 1)
        self.assertEqual(result_f["records"], 1)
        self.assertIn("retention", result_f["caveat"].lower())

    # --- ping latency --------------------------------------------------
    def test_ping_latency_p50_p99_from_audit(self) -> None:
        until = datetime.now(timezone.utc)
        audit = self.data_dir / f"audit_{until:%Y-%m-%d}.ndjson"
        stamp = (until - timedelta(minutes=1)).isoformat()
        lines = [
            json.dumps({"ts": stamp, "method": "ping", "success": True, "duration_ms": v})
            for v in (10, 20, 30, 40, 100)
        ]
        audit.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.mod.scan_ping_latency([audit], until - timedelta(hours=24), until)
        self.assertEqual(result["samples"], 5)
        self.assertEqual(result["p50"], 30)
        self.assertEqual(result["p99"], 100)


if __name__ == "__main__":
    unittest.main()
```

Сигнатуры функций — контракт карточки: `scan_stt_critical(logs, since, until)`, `scan_gigaam_chunk_loss(logs, ...)`, `scan_handle_request_hangs(logs, ...)`, `scan_bridge_401(logs, ...)`, `scan_rescue_files(rescue_dir, ...)`, `scan_unclean_deaths(forensics_dir, ...)`, `scan_ping_latency(audit_files, ...)`. Каждая возвращает `{"records"|"samples": int, "status": "ok|warn|fail|unknown", ...}`; `scan_unclean_deaths` дополнительно **обязан** вернуть `"caveat"` (ретеншен/rate-limit).

- [ ] **Step 2: RED**:

```bash
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_reliability_scan.py -v -p no:cacheprovider
```
Ожидаемо: FAIL по отсутствию модуля/функций (не по синтаксису теста).

- [ ] **Step 3: Реализация** — `scripts/reliability_scan.py` (stdlib only):
  - `since/until` — `datetime` aware; логи парсить по локальному времени С МИЛЛИСЕКУНДАМИ: `datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")` → `.astimezone()` (asctime в обоих логах — `…,%f`); audit — `datetime.fromisoformat(entry["ts"])` (ISO с offset, НЕ epoch); mtime сравнивать в UTC. Каждую границу — комментарием (TZ-ловушка).
  - `generated_ts` — ISO с offset без суффикса `Z`: `datetime.now(timezone.utc).astimezone().isoformat()` (🔴 пульт `:8777` работает на системном python 3.9 — `fromisoformat` не понимает `Z`; в 3.9 `+02:00` ок).
  - Ротация: принимать список файлов `backend.log`, `backend.log.1..3`; строку ротации учесть («в логе старше» — ок, окно всё равно 24 ч и ротация быстрая).
  - Статусы: `stt_critical`/`gigaam_chunk_loss`/`unclean_deaths` — `warn` при ≥1, `fail` при ≥3 (константы `THRESHOLD_*` наверху); `handle_request_hangs`/`bridge_401` — `warn` ≥1, `fail` ≥5; `ping_latency` — `warn` если p99 > 800 мс, `fail` > 2000 мс. Отсутствующий источник → `unknown` (НИКОГДА не ok).
  - `build_snapshot(...)` собирает словарь: `{"generated_ts", "window_hours": 24, "signals": {...}, "unknown_sources": [...], "status"}` — общий статус = худший из сигналов, но при ≥1 unknown и остальных ok → `warn` (не ok).
  - `--once` (дефолт): снять снимок, записать `YYYY-MM-DD.json` + `latest.json`, обновить `summary_7d.json` (агрегат по всем снимкам за 7 суток: суммы records, медианы p50/p99, список дат с данными); `--json` печатает снимок; `--out-dir` для тестов.
  - Пути по умолчанию: data_dir = `~/Library/Application Support/KrabEar`, rest-лог = `<repo>/logs/krab-ear-rest.err.log` (репо-корень от `__file__`), store = `~/.local/share/krab-ear/reliability`.
  - `summary_7d.json` — пересчитывать из файлов снимков (не хранить состояние в памяти).

- [ ] **Step 4: GREEN** — та же команда. Ожидаемо: все тесты зелёные.
- [ ] **Step 5: Живой прогон (read-only)** — из worktree: `python3 scripts/reliability_scan.py --json | head -40`; сверить: сигналы не «ok по умолчанию» там, где источников нет (должны быть честные unknown/числа); вывод без PII. Числа записать в отчёт.
- [ ] **Step 6: Гейт** — `flake8 --max-line-length=120` оба файла; `scripts/pre_merge_py312_check.sh KrabEar/tests/test_reliability_scan.py`.

### Task 2: Панель в пульте `:8777`

**Files:**
- Modify: `scripts/status_dashboard.py` (`collect_reliability()` + секция рендера + ДВА места сборки словаря)
- Test: `KrabEar/tests/test_reliability_scan.py` (дописать контракт-тест)

- [ ] **Step 1: Тест контракта коллектора (RED)** — читает фикстуру `latest.json` (свежую/просроченную):

```python
    def test_dashboard_collector_fresh_and_stale(self) -> None:
        # коллектор читает файл из директории, переданной параметром (тестируемость),
        # свежий (<26ч) → status из снимка; просроченный → unknown.
        ...
```
(Импорт `status_dashboard` тоже через `importlib`; передавай путь снимка параметром — если сейчас сигнатура иная, добавь параметр `snapshot_path: Path | None = None` — это и есть контракт.)

- [ ] **Step 2: Реализация** — `collect_reliability(snapshot_path=None)`: читает `latest.json`; нет файла/битый → `{"status": "unknown", "reason": "no-snapshot"}`; `generated_ts` старше 26 ч → `{"status": "unknown", "reason": "stale"}`; иначе — сигналы как есть. Секция в `render_html` рядом с `collect_prod` (пилюли статусов + 7-дн. суммы). 🔴 Вставить ключ `"reliability": collect_reliability()` в ОБА места: `do_GET` :407–413 и `main()` :441–450.
- [ ] **Step 3: GREEN** + быстрый живой просмотр: `python3 scripts/status_dashboard.py --json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['reliability']['status'])"` (системным python; зависимостей нет).

### Task 3: Launchd-обвязка (по прецеденту e2e-smoke)

**Files:**
- Create: `KrabEar/launchagents/ai.krab.ear.reliability.plist.template`
- Create: `scripts/install_reliability_scanner.command`
- Test: `KrabEar/tests/test_reliability_launchd.py` (по образцу `test_ear_smoke_launchd_2026_09_14.py`)

- [ ] **Step 1:** Шаблон: `__HOME__`/`__PROJECT_ROOT__` (как e2e-smoke), `ProgramArguments` = `.venv_krab_ear/bin/python3 -u scripts/reliability_scan.py --once`, `StartCalendarInterval` 06:00, `RunAtLoad=false`, `ProcessType=Background`, `LowPriorityIO`, `PYTHONPATH=KrabEar`, stdout/err в `<repo>/logs/reliability.out/err.log`.
- [ ] **Step 2:** Инсталлер — логика `install_ear_e2e_smoke.command`, НО verify другой: render → `plutil -lint` → `bootout` (если загружен) → `bootstrap` → верификация `launchctl print gui/$(id -u)/ai.krab.ear.reliability` содержит `Hour 6`/`Minute 0` (StartCalendarInterval; 🔴 grep «run interval» из e2e-smoke НЕ подходит — там StartInterval) → один ручной `--once` прогон (пишет снимок). Никаких правок чужих юнитов.
- [ ] **Step 3:** Контракт-тест: `plistlib.load` шаблона — `StartCalendarInterval`=06:00, `RunAtLoad` false, путь скрипта существует в репо; инсталлер упоминает `plutil -lint` и `ai.krab.ear.reliability`.
- [ ] **Step 4: Установка (живой шаг)** — выполнить `scripts/install_reliability_scanner.command`; проверить `launchctl print gui/$(id -u)/ai.krab.ear.reliability` и появившийся `latest.json`; записать в отчёт вывод.

### Task 4: Коммит и PR

- [ ] Явные `git add`: 6 файлов из Task 1–3.
- [ ] Коммит: `feat(reliability): R1 — ежедневный сканер + панель табло`
- [ ] PR в `codex/krab-ear-v2` с цифрами живого прогона (Task 1 Step 5, Task 3 Step 4). НЕ мержить.

## Definition of Done

- Синтетические тесты на КАЖДЫЙ сигнал (искусственное срабатывание — правило «всегда красный = слепота» и «всегда зелёный = обман»): missing/битый источник → `unknown`, не `ok`.
- Живой снимок создан; `--json` пульта показывает `reliability`; просроченный снимок → `unknown` (тест).
- launchd-джоб установлен и разово прогнан; контракт-тест зелёный.
- PII-дисциплина: в отчёт/снимки — только счётчики и латентности; содержимое forensics-трейлов и текстов логов не выводится.
- Прод-юниты не тронуты; ничего не перезапускалось.

## Вне scope (записать в отчёт, не чинить)

- Паттерны `backend_log_digest.py` не расширять (отдельная волна).
- «Длительность речи vs диктовка» — нет источника, честный `unknown` (будущая карточка инструментации).
- mlx-nightly (GitHub Actions) — сигнал R6, отдельно.
- Ретеншен forensics (5 каталогов) — только caveat в снимке, не расширять.
