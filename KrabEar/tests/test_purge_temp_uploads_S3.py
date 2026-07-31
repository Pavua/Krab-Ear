"""S3 задача 3: privacy-purge покрытие `temp_uploads/` (сырое REST-аудио).

`TEMP_DIR = settings.DATA_DIR / "temp_uploads"` (`rest_server.py`) хранит сырое
пользовательское аудио, загруженное через `POST /v1/stt/transcribe`, до его
транскрибации и штатного удаления. `handle_purge_all_data` про этот каталог не
знал — единственный persisted-стор пользовательских данных проекта, выпавший
из privacy-purge (`scripts/audit_purge_coverage.py` его не видит: путь строится
от `settings.DATA_DIR`, а не от одного из паттернов, которые статический guard
умеет распознать, — поэтому дыра невидима статическому аудиту и доказывается
только этим живым тестом).

Путь в тесте строится от `store.data_dir` (принятый в history_service
паттерн, см. `history_service.py:72`), а НЕ от `settings.DATA_DIR` — до
выравнивания каталогов данных (S3 задача 1) эти два пути в проде разные.

Фикстуры — по образцу `test_purge_rescue_forensics_r1.py`
(FakeStore + `HistoryService(store=store)`, без `BackendService` — тред-тирдаун
из CLAUDE.md на неё не распространяется).
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.history_service import HistoryService  # noqa: E402


class FakeHistoryItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.ts = "2024-01-01T00:00:00+00:00"

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": "тестовый текст"}


class FakeStore:
    """Минимальный StateStore fake для purge-тестов (паттерн test_purge_cluster_w1770.py)."""

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


class TempUploadsPurgeTest(unittest.TestCase):
    """temp_uploads/ (сырое аудио POST /v1/stt/transcribe) стирается при purge."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._dir = Path(self._tmpdir)

    def _purge(self) -> dict:
        store = FakeStore(data_dir=self._tmpdir)
        svc = HistoryService(store=store)
        return svc.handle_purge_all_data({"confirm": True})

    def _seed_temp_uploads(self, count: int = 2) -> list[Path]:
        """Каталог temp_uploads/ — путь строится от store.data_dir, не settings."""
        temp_uploads_dir = self._dir / "temp_uploads"
        temp_uploads_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in range(count):
            p = temp_uploads_dir / f"upload_{i}.wav"
            p.write_bytes(b"RIFF....fake-wav-body....")
            paths.append(p)
        return paths

    def test_temp_uploads_files_removed(self) -> None:
        """Сырые загрузки в temp_uploads/ удаляются purge'ем."""
        paths = self._seed_temp_uploads(count=3)
        for p in paths:
            self.assertTrue(p.exists())

        result = self._purge()

        self.assertTrue(result.get("ok"), result)
        for p in paths:
            self.assertFalse(p.exists(), f"{p} должен быть удалён purge'ом")
        self.assertNotIn("temp_uploads", result.get("errors", []))

    def test_temp_uploads_dir_survives_purge(self) -> None:
        """Каталог temp_uploads/ сам ОБЯЗАН остаться — его ждёт TEMP_DIR.mkdir
        на импорте rest_server; стирается только содержимое, не сам каталог."""
        self._seed_temp_uploads(count=1)
        temp_uploads_dir = self._dir / "temp_uploads"

        result = self._purge()

        self.assertTrue(result.get("ok"), result)
        self.assertTrue(temp_uploads_dir.is_dir(), "temp_uploads/ должен остаться после purge")
        self.assertEqual(list(temp_uploads_dir.iterdir()), [], "temp_uploads/ должен быть пуст после purge")

    def test_purge_without_temp_uploads_no_crash(self) -> None:
        """purge на data_dir без temp_uploads/ не бросает и не пятнает errors."""
        result = self._purge()
        self.assertTrue(result.get("ok"), result)
        self.assertNotIn("temp_uploads", result.get("errors", []))


if __name__ == "__main__":
    unittest.main()
