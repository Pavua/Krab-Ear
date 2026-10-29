"""Все исходные cache tests, включая настоящие thread/lock/fsync/throttle."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).with_name("test_translation_components_integration.py")
spec = importlib.util.spec_from_file_location("component_parent", path)
parent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parent)


class AggregateCacheAcceptance(parent.PersistedTranslationIntegration):
    probe_name = "translation_aggregate_probe.py"
    expected_tests = 70


if __name__ == "__main__":
    unittest.main(verbosity=2)
