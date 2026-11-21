"""KE05C-18 / KE05C-25: GC леджера и сохранность чужой схемы.

* Запись чужого владельца с ``updated_ts`` далеко в будущем проходила GC (``now - ts`` < 0)
  и жила практически вечно.
* Леджер новой схемы (``v=2``, дополнительные поля верхнего уровня) перезаписывался как ``v=1``,
  теряя поля, которых эта версия не знает.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.memory_ledger import LedgerClient  # noqa: E402


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


class FutureTimestampGc(unittest.TestCase):
    def test_far_future_foreign_entry_is_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            now = time.time()
            _write(
                path,
                {
                    "v": 1,
                    "entries": {
                        "krab/brain": {"updated_ts": now + 10**9, "owner": "krab"},
                        "krab/fresh": {"updated_ts": now - 1.0, "owner": "krab"},
                        "krab/skewed": {"updated_ts": now + 2.0, "owner": "krab"},
                        "krab/inf": {"updated_ts": float("inf"), "owner": "krab"},
                    },
                },
            )
            LedgerClient("ear", path=path).publish_own({"x": {"state": "idle"}})
            keys = set(json.loads(path.read_text())["entries"])
            self.assertNotIn("krab/brain", keys, "запись из далёкого будущего пережила GC")
            self.assertNotIn("krab/inf", keys)
            self.assertIn("krab/fresh", keys)
            self.assertIn("krab/skewed", keys, "небольшой перекос часов не должен выкидывать запись")
            self.assertIn("ear/x", keys)


class ForeignSchemaPreserved(unittest.TestCase):
    def test_newer_schema_version_and_extra_fields_survive_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            now = time.time()
            _write(
                path,
                {
                    "v": 2,
                    "generation": 7,
                    "entries": {"krab/brain": {"updated_ts": now, "owner": "krab", "extra_v2": "keep"}},
                },
            )
            LedgerClient("ear", path=path).publish_own({"x": {"state": "idle"}})
            doc = json.loads(path.read_text())
            self.assertEqual(doc["v"], 2, "версия схемы понижена")
            self.assertEqual(doc.get("generation"), 7, "поле верхнего уровня потеряно")
            self.assertEqual(doc["entries"]["krab/brain"]["extra_v2"], "keep")
            self.assertIn("ear/x", doc["entries"])

    def test_current_schema_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            LedgerClient("ear", path=path).publish_own({"x": {"state": "idle"}})
            doc = json.loads(path.read_text())
            self.assertEqual(doc["v"], 1)
            self.assertEqual(set(doc), {"v", "entries"})

    def test_garbage_version_falls_back_to_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            _write(path, {"v": "banana", "entries": {}})
            LedgerClient("ear", path=path).publish_own({"x": {"state": "idle"}})
            self.assertEqual(json.loads(path.read_text())["v"], 1)


if __name__ == "__main__":
    unittest.main()
