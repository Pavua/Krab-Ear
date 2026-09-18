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
DASHBOARD_PATH = REPO_ROOT / "scripts" / "status_dashboard.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("reliability_scan_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_dashboard():
    spec = importlib.util.spec_from_file_location("status_dashboard_under_test", DASHBOARD_PATH)
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


class DashboardCollectorTests(unittest.TestCase):
    """Контракт `status_dashboard.collect_reliability(snapshot_path=...)`.

    Коллектор читает снимок из переданного пути (тестируемость); свежий
    (<26 ч) → статус из снимка; просроченный/битый/отсутствующий → `unknown`
    (никогда не «зелёный по умолчанию»).
    """

    def setUp(self) -> None:
        self.dash = _load_dashboard()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_latest(self, payload: dict) -> Path:
        path = self.root / "latest.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_dashboard_collector_fresh_and_stale(self) -> None:
        fresh = {
            "generated_ts": datetime.now(timezone.utc).astimezone().isoformat(),
            "status": "warn",
            "signals": {"stt_critical": {"records": 2, "status": "warn"}},
        }
        path = self._write_latest(fresh)
        result = self.dash.collect_reliability(snapshot_path=path)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["signals"]["stt_critical"]["records"], 2)

        stale = dict(fresh)
        stale["generated_ts"] = (
            datetime.now(timezone.utc) - timedelta(hours=30)
        ).astimezone().isoformat()
        self._write_latest(stale)
        result = self.dash.collect_reliability(snapshot_path=path)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "stale")

    def test_dashboard_collector_missing_and_broken(self) -> None:
        missing = self.dash.collect_reliability(snapshot_path=self.root / "absent.json")
        self.assertEqual(missing["status"], "unknown")
        self.assertEqual(missing["reason"], "no-snapshot")

        broken = self._write_latest({"generated_ts": "не-дата", "status": "ok"})
        result = self.dash.collect_reliability(snapshot_path=broken)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "no-snapshot")


if __name__ == "__main__":
    unittest.main()
