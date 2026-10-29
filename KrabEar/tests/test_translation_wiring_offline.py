"""Реальная связка prefix/stores/translation/settings без полного приложения."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).with_name("test_translation_components_integration.py")
spec = importlib.util.spec_from_file_location("component_parent", path)
parent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parent)


class TranslationWiringAcceptance(parent.PersistedTranslationIntegration):
    probe_name = "translation_wiring_probe.py"
    expected_tests = 5


if __name__ == "__main__":
    unittest.main(verbosity=2)
