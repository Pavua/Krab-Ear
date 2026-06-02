"""
test_audit_purge_coverage.py — W1768 privacy-purge coverage guard tests.

Verifies the static-analysis guard scripts/audit_purge_coverage.py:

  (a) a synthetic module persisting a store the purge does NOT clear is
      reported as a gap;
  (b) a store the purge clears (exact id or basename) is NOT reported;
  (c) a store on the allowlist is NOT reported;
  (d) the guard runs on the real repo without crashing and emits a structured
      report (text + JSON), with a stable, well-formed gap set.

The guard is imported from scripts/ without installation (importlib), matching
the pattern of the other audit-scanner tests.
"""
import importlib.util
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).parent
_REPO_ROOT = _TESTS_DIR.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _load_guard():
    """Import audit_purge_coverage from scripts/ without installation.

    The module must be registered in ``sys.modules`` *before* ``exec_module``:
    on Python 3.14 the ``@dataclass`` machinery resolves
    ``sys.modules[cls.__module__]`` while processing the class, which would be
    ``None`` for an unregistered module.
    """
    guard_path = _SCRIPTS_DIR / "audit_purge_coverage.py"
    spec = importlib.util.spec_from_file_location("audit_purge_coverage", guard_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FixtureMixin(unittest.TestCase):
    """Helpers for writing synthetic backend/core modules to temp files."""

    @classmethod
    def setUpClass(cls):
        cls.guard = _load_guard()

    def _write_module(self, source: str) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        )
        tmp.write(textwrap.dedent(source))
        tmp.flush()
        tmp.close()
        self.addCleanup(lambda p=tmp.name: Path(p).unlink(missing_ok=True))
        return Path(tmp.name)


class TestDiscovery(_FixtureMixin):
    """Step 1 — discovery of persisted stores under the data dir."""

    def test_literal_filename_under_data_dir_is_discovered(self):
        mod = self._write_module(
            """
            from pathlib import Path

            class WidgetStore:
                def __init__(self, data_dir):
                    self.data_dir = Path(data_dir)
                    self._path = self.data_dir / "widgets.json"
            """
        )
        ids = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        self.assertIn("widgets.json", ids)

    def test_module_constant_filename_is_resolved(self):
        mod = self._write_module(
            """
            from pathlib import Path

            _STORE_FILE = "gizmos.ndjson"

            class GizmoStore:
                def __init__(self, data_dir):
                    self._data_dir = Path(data_dir)
                    self._path = self._data_dir / _STORE_FILE
            """
        )
        ids = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        self.assertIn("gizmos.ndjson", ids)

    def test_class_constant_filename_is_resolved(self):
        mod = self._write_module(
            """
            from pathlib import Path

            class Thing:
                _FILENAME = "things.json"

                def __init__(self, data_dir):
                    self._path = Path(data_dir) / self._FILENAME
            """
        )
        ids = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        self.assertIn("things.json", ids)

    def test_subdir_is_discovered_and_qualified(self):
        mod = self._write_module(
            """
            from pathlib import Path

            class ShareStore:
                def __init__(self, data_dir):
                    self._data_dir = Path(data_dir)
                    self._shares_dir = self._data_dir / "shares"
                    self._index = self._shares_dir / "index.json"

                def _ensure(self):
                    self._shares_dir.mkdir(parents=True, exist_ok=True)
            """
        )
        ids = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        # The nested file is qualified with its subdir path.
        self.assertIn("shares/index.json", ids)
        self.assertIn("shares/", ids)

    def test_non_data_dir_path_is_not_discovered(self):
        mod = self._write_module(
            """
            from pathlib import Path

            class HomeStore:
                def __init__(self):
                    # Rooted at the home dir, not the configurable data dir.
                    self._path = Path.home() / "Library" / "thing.json"
            """
        )
        ids = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        self.assertNotIn("thing.json", ids)

    def test_glob_family_is_discovered(self):
        mod = self._write_module(
            """
            from pathlib import Path

            class AuditStore:
                def __init__(self, data_dir):
                    self._data_dir = Path(data_dir)

                def _list(self):
                    return sorted(self._data_dir.glob("audit_*.ndjson"))
            """
        )
        ids = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        self.assertIn("audit_*.ndjson", ids)

    def test_bare_glob_is_not_treated_as_distinct_store(self):
        mod = self._write_module(
            """
            from pathlib import Path

            class TranscriptDir:
                def __init__(self, data_dir):
                    self._dir = Path(data_dir) / "transcripts"

                def _list(self):
                    return list(self._dir.glob("*.md"))
            """
        )
        ids = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        # A bare "*.md" glob is the containing subdir, not a distinct store id.
        self.assertNotIn("transcripts/*.md", ids)


class TestCoverageMatching(_FixtureMixin):
    """Step 2/3 — gap classification via _is_covered()."""

    # (a) NOT covered -> reported
    def test_uncovered_store_is_a_gap(self):
        self.assertFalse(
            self.guard._is_covered("widgets.json", covered=set(), allowlisted=set())
        )

    # (b) exact-id covered -> not reported
    def test_exact_covered_store_not_a_gap(self):
        self.assertTrue(
            self.guard._is_covered(
                "widgets.json", covered={"widgets.json"}, allowlisted=set()
            )
        )

    # (b') basename covered -> not reported (file under a subdir cleared by name)
    def test_basename_covered_store_not_a_gap(self):
        self.assertTrue(
            self.guard._is_covered(
                "archive/archive.ndjson",
                covered={"archive.ndjson"},
                allowlisted=set(),
            )
        )

    # (b'') file under a covered directory prefix -> not reported
    def test_file_under_covered_dir_not_a_gap(self):
        self.assertTrue(
            self.guard._is_covered(
                "backups/auto_backup_meta.json",
                covered={"backups/"},
                allowlisted=set(),
            )
        )

    def test_directory_whose_children_are_all_covered_not_a_gap(self):
        # archive/ is covered when its only discovered child file is cleared.
        self.assertTrue(
            self.guard._is_covered(
                "archive/",
                covered={"archive.ndjson"},
                allowlisted=set(),
                discovered_ids={"archive/", "archive/archive.ndjson"},
            )
        )

    # (c) allowlisted store -> not reported
    def test_allowlisted_store_not_a_gap(self):
        self.assertTrue(
            self.guard._is_covered(
                "feature_flags.json", covered=set(), allowlisted={"feature_flags.json"}
            )
        )


class TestEndToEndSyntheticTree(_FixtureMixin):
    """End-to-end gap detection on a synthetic data-dir module set."""

    def test_synthetic_uncovered_store_surfaces_as_gap(self):
        # Build a fake module that persists a store, then assert that with an
        # empty coverage/allowlist set the store is classified as a gap.
        mod = self._write_module(
            """
            from pathlib import Path

            _SECRETS_FILE = "secrets.ndjson"

            class SecretStore:
                def __init__(self, data_dir):
                    self._data_dir = Path(data_dir)
                    self._path = self._data_dir / _SECRETS_FILE
            """
        )
        discovered = {r.store_id for r in self.guard.discover_stores_in_module(mod)}
        self.assertIn("secrets.ndjson", discovered)

        # Not covered, not allowlisted -> gap.
        self.assertFalse(
            self.guard._is_covered("secrets.ndjson", set(), set())
        )
        # Covered (e.g. a collaborator clears it) -> not a gap.
        self.assertTrue(
            self.guard._is_covered("secrets.ndjson", {"secrets.ndjson"}, set())
        )
        # Allowlisted -> not a gap.
        self.assertTrue(
            self.guard._is_covered("secrets.ndjson", set(), {"secrets.ndjson"})
        )


class TestRealRepo(_FixtureMixin):
    """(d) The guard runs on the real repo without crashing, structured output."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = cls.guard.run_audit()

    def test_discovers_known_pii_stores(self):
        # Sanity: discovery must find the core history + transcript stores.
        ids = set(self.result.discovered.keys())
        self.assertIn("history.ndjson", ids)
        self.assertIn("transcripts/", ids)

    def test_known_covered_stores_are_not_gaps(self):
        # Stores the purge demonstrably wipes must not appear as gaps.
        gap_ids = {ref.store_id for ref in self.result.gaps}
        for covered_id in (
            "history.ndjson",
            "archive.ndjson",
            "bookmarks.ndjson",
            "call_sessions.ndjson",
            "speaker_fingerprints.json",
            "vocabulary.json",
            "webhooks.json",
            "embeddings.npy",
        ):
            self.assertNotIn(covered_id, gap_ids, f"{covered_id} should be covered")

    def test_allowlist_entries_are_not_gaps(self):
        gap_ids = {ref.store_id for ref in self.result.gaps}
        for allow_id in self.result.allowlisted:
            self.assertNotIn(allow_id, gap_ids)

    def test_text_report_is_structured(self):
        text = self.guard.format_report(self.result)
        self.assertIn("PRIVACY-PURGE COVERAGE AUDIT", text)
        self.assertIn("discovered stores", text)
        self.assertIn("UNCOVERED GAPS", text)

    def test_json_report_is_valid_and_complete(self):
        payload = json.loads(self.guard.format_json(self.result))
        for key in (
            "discovered_count",
            "covered_count",
            "allowlisted_count",
            "gap_count",
            "gaps",
            "covered",
            "allowlisted",
            "discovered",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["gap_count"], len(payload["gaps"]))
        # Every gap carries module + location for the coordinator's fix.
        for gap in payload["gaps"]:
            self.assertTrue(gap["store_id"])
            self.assertTrue(gap["module"])
            self.assertIn(":", gap["location"])

    def test_fail_on_found_exit_code_matches_gap_presence(self):
        # main(--fail-on-found) returns 1 iff there is at least one gap.
        rc = self.guard.main(["--fail-on-found"])
        expected = 1 if self.result.gaps else 0
        self.assertEqual(rc, expected)

    def test_runs_fast(self):
        # The audit must be cheap enough for CI on every push (<5s budget).
        import time

        t0 = time.monotonic()
        self.guard.run_audit()
        self.assertLess(time.monotonic() - t0, 5.0)


if __name__ == "__main__":
    unittest.main()
