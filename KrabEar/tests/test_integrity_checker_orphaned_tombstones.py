import unittest
from pathlib import Path
import tempfile
import json
from backend.integrity_checker import IntegrityChecker

class TestIntegrityChecker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        
        self.history_path = self.data_dir / "history.ndjson"
        self.tombstones_path = self.data_dir / "history_tombstones.ndjson"
        
        # history has item1
        self.history_path.write_text(json.dumps({"id": "item1", "ts": "2026-01-01T00:00:00Z", "text": "foo"}) + "\n")
        
        # tombstones has item2 (meaning item2 was deleted)
        self.tombstones_path.write_text(json.dumps({"id": "item2", "ts": "2026-01-01T00:00:00Z", "text": "deleted"}) + "\n")
        
        self.checker = IntegrityChecker()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_orphaned_tombstones_should_not_delete_valid_tombstones(self):
        report = self.checker.check_integrity(self.data_dir)
        
        # item2 is a valid tombstone (it's not in active_ids). It should NOT be flagged as orphaned.
        for check in report.checks:
            if check.name == "orphaned_tombstones":
                self.assertEqual(check.status, "ok", "Valid tombstone flagged as orphaned")

        # repair shouldn't delete it
        repair_result = self.checker.repair(self.data_dir, report)
        
        tombstones_content = self.tombstones_path.read_text()
        self.assertIn("item2", tombstones_content, "Valid tombstone was deleted by repair!")

if __name__ == '__main__':
    unittest.main()
