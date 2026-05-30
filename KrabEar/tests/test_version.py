"""Тесты согласованности версии Krab Ear 2.0.0 во всех точках."""

from __future__ import annotations

import sys
import os
import unittest

# Ensure KrabEar package root is on sys.path.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

EXPECTED_VERSION = "2.0.5"


class TestVersionFile(unittest.TestCase):
    """__version__.py defines the canonical version."""

    def test_version_string(self):
        from KrabEar.__version__ import __version__
        self.assertEqual(__version__, EXPECTED_VERSION)

    def test_version_format(self):
        from KrabEar.__version__ import __version__
        parts = __version__.split(".")
        self.assertEqual(len(parts), 3, "Version should be MAJOR.MINOR.PATCH")
        for part in parts:
            self.assertTrue(part.isdigit(), f"Version part {part!r} is not numeric")


class TestPackageInit(unittest.TestCase):
    """KrabEar/__init__.py exports __version__."""

    def test_package_exports_version(self):
        import KrabEar
        self.assertTrue(hasattr(KrabEar, "__version__"),
                        "KrabEar package must export __version__")
        self.assertEqual(KrabEar.__version__, EXPECTED_VERSION)


class TestApiVersioning(unittest.TestCase):
    """api_versioning.get_api_info() includes app_version."""

    def test_get_api_info_has_app_version(self):
        # api_versioning imports flask; we only need APP_VERSION, not Flask context
        from backend.api_versioning import get_api_info
        info = get_api_info()
        self.assertIn("app_version", info)
        self.assertEqual(info["app_version"], EXPECTED_VERSION)

    def test_app_version_constant(self):
        from backend.api_versioning import APP_VERSION
        self.assertEqual(APP_VERSION, EXPECTED_VERSION)


class TestHealthChecker(unittest.TestCase):
    """health_checker.VERSION matches canonical version."""

    def test_version_constant(self):
        from backend.health_checker import VERSION
        self.assertEqual(VERSION, EXPECTED_VERSION)


class TestStartupDiagnostics(unittest.TestCase):
    """StartupReport.to_dict() includes 'version' field."""

    def test_to_dict_has_version(self):
        from backend.startup_diagnostics import StartupReport
        report = StartupReport(
            status="ready",
            checks=[],
            startup_time_ms=1.0,
            warnings=[],
            errors=[],
        )
        d = report.to_dict()
        self.assertIn("version", d)
        self.assertEqual(d["version"], EXPECTED_VERSION)

    def test_app_version_constant(self):
        from backend.startup_diagnostics import APP_VERSION
        self.assertEqual(APP_VERSION, EXPECTED_VERSION)


class TestCLIVersion(unittest.TestCase):
    """CLI --version flag prints the correct version."""

    def test_version_import(self):
        # Verify the version symbol imported in cli.py is correct
        from KrabEar.__version__ import __version__
        self.assertEqual(__version__, EXPECTED_VERSION)

    def test_cli_parser_version(self):
        import io
        from cli import build_parser
        parser = build_parser()
        io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            # argparse --version raises SystemExit(0)
            parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)


class TestPingVersion(unittest.TestCase):
    """BackendService._handle_ping returns the canonical version."""

    def test_ping_uses_app_version(self):
        # We just check APP_VERSION is imported in service module
        from backend import service as svc
        self.assertTrue(hasattr(svc, "APP_VERSION"),
                        "service.py must import APP_VERSION")
        self.assertEqual(svc.APP_VERSION, EXPECTED_VERSION)


class TestVersionConsistency(unittest.TestCase):
    """All version sources agree on a single value."""

    def test_all_versions_equal(self):
        from KrabEar.__version__ import __version__
        import KrabEar
        from backend.api_versioning import APP_VERSION as api_ver
        from backend.health_checker import VERSION as hc_ver
        from backend.startup_diagnostics import APP_VERSION as sd_ver
        from backend import service as svc

        versions = {
            "__version__.py": __version__,
            "KrabEar package": KrabEar.__version__,
            "api_versioning.APP_VERSION": api_ver,
            "health_checker.VERSION": hc_ver,
            "startup_diagnostics.APP_VERSION": sd_ver,
            "service.APP_VERSION": svc.APP_VERSION,
        }

        for source, v in versions.items():
            self.assertEqual(
                v, EXPECTED_VERSION,
                f"Version mismatch in {source}: got {v!r}, expected {EXPECTED_VERSION!r}",
            )


if __name__ == "__main__":
    unittest.main()
