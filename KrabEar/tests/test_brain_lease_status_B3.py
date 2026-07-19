"""B3 brain-lease visibility — IPC `get_brain_lease_status` + diagnostics section.

Spec: docs/superpowers/specs/2026-07-19-b3-brain-lease-visibility-design.md

ALL tests use a TEMP lock path via KRAB_EAR_BRAIN_LEASE_PATH env var
(reversibly set in setUp/tearDown) and NEVER touch
~/.openclaw/lm_studio_brain.lock so CI is safe.

Coverage:
  1. free lock  -> held=False, all schema keys present with null values
  2. held lock  -> owner/pid/acquired_ts/exp_ts/seconds_left populated
  3. expired    -> held=False (expired owner is never reported)
  4. llm_brain_lease_enabled=False (runtime settings) -> enabled=False
  5. malformed payload written by a foreign process -> types coerced, no raise
  6. dispatch key present in service.py + brain_lease section in get_diagnostics
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_KRAB_EAR_ROOT = _HERE.parent.parent
if str(_KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(_KRAB_EAR_ROOT))

from backend.brain_lease import acquire_brain_lease  # noqa: E402
from backend.health_check_service import HealthCheckService  # noqa: E402

_SERVICE_PY = _KRAB_EAR_ROOT / "backend" / "service.py"

_SCHEMA_KEYS = {
    "enabled", "held", "owner", "pid", "acquired_ts", "exp_ts", "seconds_left",
}


def _make_service(settings: dict | None = None) -> HealthCheckService:
    """Minimal HealthCheckService with duck-typed collaborators (no BackendService,
    no daemon threads — the #1782 tearDown lesson does not apply here)."""
    store = SimpleNamespace(count_active_items=lambda: 0, data_dir="/tmp/nonexistent")
    settings_svc = SimpleNamespace(
        cached_settings=lambda: dict(settings or {}),
        _cache_ttl=5.0,
        _cache=None,
    )
    return HealthCheckService(
        store=store,
        health_checker=SimpleNamespace(check_all=lambda: {}),
        startup_diagnostics=SimpleNamespace(),
        integrity_checker=SimpleNamespace(),
        settings_svc=settings_svc,
    )


class BrainLeaseStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._lock_path = Path(self._tmp.name) / "brain_lease.lock"
        self._prev_env = os.environ.get("KRAB_EAR_BRAIN_LEASE_PATH")
        os.environ["KRAB_EAR_BRAIN_LEASE_PATH"] = str(self._lock_path)

    def tearDown(self) -> None:
        # Reversible env mutation (sys.modules-stub lesson: no order-dependent leftovers).
        if self._prev_env is None:
            os.environ.pop("KRAB_EAR_BRAIN_LEASE_PATH", None)
        else:
            os.environ["KRAB_EAR_BRAIN_LEASE_PATH"] = self._prev_env
        self._tmp.cleanup()

    # 1 ------------------------------------------------------------------
    def test_free_lease_schema_parity(self) -> None:
        svc = _make_service()
        result = svc.handle_get_brain_lease_status({})
        self.assertTrue(result["ok"])
        self.assertEqual(_SCHEMA_KEYS | {"ok"}, set(result.keys()))
        self.assertTrue(result["enabled"])
        self.assertFalse(result["held"])
        for key in ("owner", "pid", "acquired_ts", "exp_ts", "seconds_left"):
            self.assertIsNone(result[key], f"{key} must be null when free")

    # 2 ------------------------------------------------------------------
    def test_held_lease_reports_owner(self) -> None:
        self.assertTrue(
            acquire_brain_lease("krab", ttl_sec=60.0, lock_path=self._lock_path)
        )
        result = _make_service().handle_get_brain_lease_status({})
        self.assertTrue(result["held"])
        self.assertEqual("krab", result["owner"])
        self.assertIsInstance(result["pid"], int)
        self.assertIsInstance(result["acquired_ts"], float)
        self.assertIsInstance(result["exp_ts"], float)
        self.assertGreater(result["seconds_left"], 0.0)
        self.assertLessEqual(result["seconds_left"], 60.0)

    # 3 ------------------------------------------------------------------
    def test_expired_lease_reports_free(self) -> None:
        self.assertTrue(
            acquire_brain_lease("krab_ear", ttl_sec=-5.0, lock_path=self._lock_path)
        )
        result = _make_service().handle_get_brain_lease_status({})
        self.assertFalse(result["held"])
        self.assertIsNone(result["owner"])

    # 4 ------------------------------------------------------------------
    def test_disabled_setting_reflected(self) -> None:
        svc = _make_service({"llm_brain_lease_enabled": False})
        result = svc.handle_get_brain_lease_status({})
        self.assertFalse(result["enabled"])
        # held is still reported honestly even when the feature toggle is off.
        self.assertIn("held", result)

    # 5 ------------------------------------------------------------------
    def test_malformed_foreign_payload_coerced(self) -> None:
        # A foreign process wrote a payload with a non-numeric pid — the handler
        # must not trust the wire schema: coerce bad fields to null, no raise.
        now = time.time()
        self._lock_path.write_text(
            json.dumps({"owner": "krab", "pid": "abc", "exp_ts": now + 60.0})
        )
        result = _make_service().handle_get_brain_lease_status({})
        self.assertTrue(result["held"])
        self.assertEqual("krab", result["owner"])
        self.assertIsNone(result["pid"])
        self.assertIsNone(result["acquired_ts"])  # absent in payload -> null

    # 6 ------------------------------------------------------------------
    def test_dispatch_key_and_diagnostics_section(self) -> None:
        src = _SERVICE_PY.read_text(encoding="utf-8")
        keys = set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', src))
        self.assertIn("get_brain_lease_status", keys)

        diag = _make_service().handle_get_diagnostics({})
        self.assertIn("brain_lease", diag)
        self.assertEqual(_SCHEMA_KEYS, set(diag["brain_lease"].keys()))


if __name__ == "__main__":
    unittest.main()
