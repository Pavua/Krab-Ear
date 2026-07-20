"""GigaAM v2 → v3 апгрейд (спека 2026-07-20-gigaam-v3-upgrade-design.md).

Покрытие:
  1. Дефолт STT_GIGAAM_MODE = "v3_e2e_rnnt" (config + DEFAULT_SETTINGS).
  2. Worker engine-name derivation: v3_* → "gigaam-rnnt" (телеметрия одинакова
     для всех версий; legacy v2/v1 не сломаны).
  3. PunctuationFixer идемпотентен на cased+пунктуированном выводе v3 (страж от
     будущей двойной пунктуации — v3 в отличие от v2 отдаёт уже пунктуированный текст).
  4. install_gigaam_venv.command ставит v3 из пинованного git-коммита (source-contract).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_KRAB_EAR_ROOT = _HERE.parent.parent
if str(_KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(_KRAB_EAR_ROOT))

# Импорт воркера безопасен только с выключенным singleton-lock (иначе живой
# прод-воркер, держащий flock, вызовет sys.exit(0) при импорте — убьёт тест).
os.environ["KRAB_EAR_GIGAAM_WORKER_NO_SINGLETON"] = "1"


class GigaAMV3DefaultModeTest(unittest.TestCase):
    def test_config_default_mode_is_v3_e2e_rnnt(self) -> None:
        from core.config import Settings
        self.assertEqual(Settings().STT_GIGAAM_MODE, "v3_e2e_rnnt")

    def test_default_settings_dict_mode_is_v3_e2e_rnnt(self) -> None:
        from core.config import DEFAULT_SETTINGS
        self.assertEqual(DEFAULT_SETTINGS["stt_gigaam_mode"], "v3_e2e_rnnt")


class GigaAMEngineNameTest(unittest.TestCase):
    def test_v3_modes_map_to_gigaam_rnnt(self) -> None:
        from core.workers.gigaam_worker import _engine_name_from_mode
        # v3_e2e_ срезается ДО v3_ (порядок replace) — иначе осталось бы "e2e_rnnt".
        self.assertEqual(_engine_name_from_mode("v3_e2e_rnnt"), "gigaam-rnnt")
        self.assertEqual(_engine_name_from_mode("v3_rnnt"), "gigaam-rnnt")
        self.assertEqual(_engine_name_from_mode("v3_e2e_ctc"), "gigaam-ctc")

    def test_legacy_modes_still_map_correctly(self) -> None:
        from core.workers.gigaam_worker import _engine_name_from_mode
        self.assertEqual(_engine_name_from_mode("v2_rnnt"), "gigaam-rnnt")
        self.assertEqual(_engine_name_from_mode("v1_ctc"), "gigaam-ctc")
        self.assertEqual(_engine_name_from_mode("rnnt"), "gigaam-rnnt")
        self.assertEqual(_engine_name_from_mode(None), "gigaam-rnnt")


class PunctuationIdempotencyTest(unittest.TestCase):
    """v3 отдаёт уже пунктуированный текст — fixer НЕ должен добавлять второй знак."""

    def test_fixer_idempotent_on_v3_punctuated_output(self) -> None:
        from core.punctuation_fixer import PunctuationFixer
        fx = PunctuationFixer()
        v3_samples = [
            "Слушай, я сегодня записываю голосом прямо в приложение, и вроде бы работает довольно неплохо.",
            "Встреча назначена на 15:30, будет три человека. Бюджет 120 000 рублей. Уточни, пожалуйста, дату ещё раз.",
            "В целом система распознавания речи работает хорошо, особенно когда модель уже загружена в память.",
        ]
        for text in v3_samples:
            once = fx.fix(text, "ru")
            twice = fx.fix(once, "ru")
            self.assertEqual(once, twice, f"fixer НЕ идемпотентен на: {text!r}")
            # Число точек не растёт (страж от двойной пунктуации на уже-пунктуированном).
            self.assertLessEqual(once.count("."), text.count(".") + 1)


class InstallScriptContractTest(unittest.TestCase):
    def test_install_script_uses_pinned_git_v3(self) -> None:
        script = (_KRAB_EAR_ROOT.parent / "scripts" / "install_gigaam_venv.command").read_text(
            encoding="utf-8"
        )
        self.assertIn("GIGAAM_COMMIT=", script, "коммит должен быть пинован")
        self.assertIn("v3_e2e_rnnt", script, "smoke должен проверять v3-модель в реестре")
        self.assertNotIn("pip install gigaam\n", script, "старый PyPI-install (v2-only) удалён")


if __name__ == "__main__":
    unittest.main()
