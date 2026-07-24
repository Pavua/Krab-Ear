"""R1 Task 7: privacy-purge покрытие rescue/ + forensics/ + dirty-маркера.

audit_purge_coverage.py --fail-on-found обнаружил, что continuous spill
записи (recording_spill.py, Task 1-3) и сбор форензики после некорректного
завершения (shutdown_forensics.py, Task 6) создают под data_dir новые
PII-хранилища, которые handle_purge_all_data не чистил:

  - rescue/                 — сырое аудио spill (.f32.part/.meta.json/.rescued.wav),
                               голос пользователя, недо-восстановленный на старте
  - forensics/<ts>/*.txt|json — хвосты unified log / launchctl print / собственных
                               логов backend, собранные после SIGKILL/OOM
  - runtime_alive.marker    — dirty-маркер текущей жизни процесса (не PII, но
                               чистим заодно — ноль вреда)

Тесты сеют реальные файлы через RecordingSpillWriter (rescue/) и напрямую
через каталог/файлы форензики (по формату shutdown_forensics.py), затем
проверяют, что handle_purge_all_data({"confirm": True}) стирает всё и
возвращает rescue_deleted в ответе. Фикстуры — по образцу
test_purge_cluster_w1770.py (FakeStore + HistoryService(store=store)).
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService          # noqa: E402
from backend.recording_spill import RecordingSpillWriter     # noqa: E402
from backend.shutdown_forensics import _MARKER               # noqa: E402


# ---------------------------------------------------------------------------
# Минимальный StateStore fake (паттерн из test_purge_cluster_w1770.py)
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2024-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": "тестовый текст"}


class FakeStore:
    """Минимальный StateStore fake для purge-тестов."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}
        self._tombstones: list[dict] = []
        self._lock_obj = threading.Lock()
        self._settings: dict = {}

    def _lock(self):
        return self._lock_obj

    def _load_active_items_unlocked(self) -> list[FakeHistoryItem]:
        return list(self._items.values())

    def _append_ndjson(self, path: Any, payload: dict) -> None:
        self._tombstones.append(payload)

    @property
    def tombstones_path(self) -> str:
        return "fake_tombstones.ndjson"

    def compact_with_stats(self) -> dict:
        return {"before_active_count": len(self._items), "after_active_count": 0}

    def load_settings(self) -> dict:
        return dict(self._settings)

    def save_settings(self, settings: dict) -> dict:
        self._settings = dict(settings)
        return dict(settings)


class RescueForensicsPurgeTest(unittest.TestCase):
    """R1: rescue/, forensics/ и runtime_alive.marker очищаются при purge."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._dir = Path(self._tmpdir)

    def _purge(self) -> dict:
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        return svc.handle_purge_all_data({"confirm": True})

    def _seed_rescue_part(self) -> Path:
        """Открыть и оставить незакрытый .part-файл — по образцу
        continuous spill (RecordingSpillWriter.open() + append(), НЕ discard()).
        """
        import numpy as np

        writer = RecordingSpillWriter(
            rescue_dir=self._dir / "rescue",
            sample_rate=16000,
            channels=1,
            source="dictation",
        )
        self.assertTrue(writer.open())
        writer.append(np.ones(1600, dtype="float32") * 0.5)
        writer.close()  # close() оставляет файлы — сценарий спасения
        return writer.part_path

    def _seed_forensics(self) -> Path:
        """Каталог форензики по формату shutdown_forensics._collect_forensics:
        forensics/<ts>/{log_show,launchctl_print,own_logs_tail}.txt +
        {prev_shutdown_info,stale_marker}.json."""
        out_dir = self._dir / "forensics" / "20260724_120000_000000"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "log_show.txt").write_text("log show output", encoding="utf-8")
        (out_dir / "launchctl_print.txt").write_text("launchctl print output", encoding="utf-8")
        (out_dir / "own_logs_tail.txt").write_text("own logs tail", encoding="utf-8")
        (out_dir / "prev_shutdown_info.json").write_text('{"clean": false}', encoding="utf-8")
        (out_dir / "stale_marker.json").write_text('{"pid": 1234}', encoding="utf-8")
        return out_dir

    def _seed_marker(self) -> Path:
        marker = self._dir / _MARKER
        marker_payload = {"pid": 1234, "started_at_iso": "2026-07-24T12:00:00+00:00"}
        marker.write_text(json.dumps(marker_payload), encoding="utf-8")
        return marker

    def test_rescue_files_removed_and_counted(self) -> None:
        """.part + .meta.json в rescue/ удаляются, rescue_deleted в ответе >= 2."""
        part_path = self._seed_rescue_part()
        meta_path = part_path.with_name(part_path.name.replace(".f32.part", ".meta.json"))
        self.assertTrue(part_path.exists())
        self.assertTrue(meta_path.exists())

        result = self._purge()

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(part_path.exists(), ".part должен быть удалён")
        self.assertFalse(meta_path.exists(), ".meta.json должен быть удалён")
        self.assertGreaterEqual(result.get("rescue_deleted", 0), 2)
        self.assertNotIn("rescue", result.get("errors", []))

    def test_rescue_dir_removed_when_empty_after_purge(self) -> None:
        """rescue/ пуст (или отсутствует) после purge — данных не осталось."""
        self._seed_rescue_part()
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        rescue_dir = self._dir / "rescue"
        if rescue_dir.is_dir():
            self.assertEqual(list(rescue_dir.iterdir()), [], "rescue/ должен быть пуст после purge")

    def test_rescued_wav_removed(self) -> None:
        """Финализированный .rescued.wav (после recording_rescue) тоже стирается."""
        rescue_dir = self._dir / "rescue"
        rescue_dir.mkdir(parents=True, exist_ok=True)
        wav_path = rescue_dir / "abc123.rescued.wav"
        wav_path.write_bytes(b"RIFF....fake-wav-body....")

        result = self._purge()

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(wav_path.exists(), ".rescued.wav должен быть удалён")

    def test_forensics_dir_removed(self) -> None:
        """forensics/<ts>/*.txt|json удаляются целиком вместе с каталогом."""
        out_dir = self._seed_forensics()
        self.assertTrue(out_dir.exists())

        result = self._purge()

        self.assertTrue(result.get("ok"), result)
        self.assertFalse((self._dir / "forensics").exists(), "forensics/ должен быть удалён целиком")
        self.assertNotIn("forensics", result.get("errors", []))

    def test_runtime_alive_marker_removed(self) -> None:
        """runtime_alive.marker удаляется (ноль вреда — не PII, но чистим)."""
        marker = self._seed_marker()
        self.assertTrue(marker.exists())

        result = self._purge()

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(marker.exists(), "runtime_alive.marker должен быть удалён")
        self.assertNotIn("runtime_alive_marker", result.get("errors", []))

    def test_purge_without_rescue_or_forensics_no_crash(self) -> None:
        """purge на пустом data_dir не бросает и не добавляет новые шаги в errors."""
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("rescue_deleted", 0), 0)
        for step in ("rescue", "forensics", "runtime_alive_marker"):
            self.assertNotIn(step, result.get("errors", []), f"{step} не должен попасть в errors на пустом dir")

    def test_all_three_together(self) -> None:
        """Комбинированный сценарий: rescue + forensics + marker одновременно."""
        part_path = self._seed_rescue_part()
        out_dir = self._seed_forensics()
        marker = self._seed_marker()

        result = self._purge()

        self.assertTrue(result.get("ok"), result)
        self.assertFalse(part_path.exists())
        self.assertFalse(out_dir.exists())
        self.assertFalse((self._dir / "forensics").exists())
        self.assertFalse(marker.exists())
        self.assertEqual(result.get("errors", []), [])


if __name__ == "__main__":
    unittest.main()
