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

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

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


class GigaAMV3AdapterContractTest(unittest.TestCase):
    """Дефолт v3 обязан доходить до адаптера, а не до fallback STT."""

    def test_default_mode_is_accepted_by_adapter(self) -> None:
        from core.config import Settings
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter(
            device="cpu",
            mode=Settings().STT_GIGAAM_MODE,
            transport="subprocess",
        )

        self.assertEqual(adapter._mode, "v3_e2e_rnnt")
        self.assertEqual(adapter._engine_name(), "gigaam-rnnt")

    def test_all_v3_asr_modes_are_accepted(self) -> None:
        from core.pipeline.stt_gigaam import GigaAMAdapter

        expected = {
            "v3_rnnt": "gigaam-rnnt",
            "v3_ctc": "gigaam-ctc",
            "v3_e2e_rnnt": "gigaam-rnnt",
            "v3_e2e_ctc": "gigaam-ctc",
        }
        for mode, engine in expected.items():
            with self.subTest(mode=mode):
                adapter = GigaAMAdapter(device="cpu", mode=mode)
                self.assertEqual(adapter._engine_name(), engine)

    def test_settings_validator_rejects_unknown_mode_to_v3_default(self) -> None:
        from backend.settings_validator import SettingsValidator

        result = SettingsValidator().validate({"stt_gigaam_mode": "nonsense"})

        self.assertTrue(result.valid)
        self.assertEqual(result.fixed["stt_gigaam_mode"], "v3_e2e_rnnt")
        self.assertEqual(len(result.warnings), 1)


class GigaAMV3LongformContractTest(unittest.TestCase):
    """Longform v3 возвращает dataclass-объекты, а не legacy list[dict]."""

    @staticmethod
    def _v3_result() -> SimpleNamespace:
        return SimpleNamespace(
            segments=[
                SimpleNamespace(text="Первый сегмент.", start=0.0, end=1.0),
                SimpleNamespace(text="Второй сегмент.", start=1.0, end=2.0),
            ],
        )

    def test_in_process_adapter_extracts_v3_segment_text(self) -> None:
        from core.pipeline.stt_gigaam import GigaAMAdapter

        adapter = GigaAMAdapter(device="cpu", mode="v3_e2e_rnnt")
        adapter._get_model = lambda: SimpleNamespace(
            transcribe_longform=lambda _path: self._v3_result(),
        )

        text, engine = adapter._transcribe_in_process(
            "/tmp/fake-v3-longform.wav",
            longform=True,
        )

        self.assertEqual(text, "Первый сегмент.\n\nВторой сегмент.")
        self.assertEqual(engine, "gigaam-rnnt-longform")

    def test_worker_extracts_v3_segment_text(self) -> None:
        import core.workers.gigaam_worker as gigaam_worker

        old_model = gigaam_worker._MODEL
        old_mode = gigaam_worker._MODE
        gigaam_worker._MODEL = SimpleNamespace(
            transcribe_longform=lambda _path: self._v3_result(),
        )
        gigaam_worker._MODE = "v3_e2e_rnnt"
        try:
            result = gigaam_worker._handle_transcribe({
                "audio_path": "/tmp/fake-v3-longform.wav",
                "longform": True,
            })
        finally:
            gigaam_worker._MODEL = old_model
            gigaam_worker._MODE = old_mode

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["text"], "Первый сегмент.\n\nВторой сегмент.")
        self.assertEqual(result["segments_count"], 2)
        self.assertEqual(result["engine"], "gigaam-rnnt-longform")

    def test_worker_keeps_vendor_progress_out_of_json_stdout(self) -> None:
        """Progress pyannote не должен загрязнять однострочный JSON-протокол."""
        import core.workers.gigaam_worker as gigaam_worker

        def _noisy_longform(_path: str) -> SimpleNamespace:
            print("filtered by duration: 0/1 samples")
            return self._v3_result()

        old_model = gigaam_worker._MODEL
        old_mode = gigaam_worker._MODE
        gigaam_worker._MODEL = SimpleNamespace(transcribe_longform=_noisy_longform)
        gigaam_worker._MODE = "v3_e2e_rnnt"
        protocol_stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(protocol_stdout):
                result = gigaam_worker._handle_transcribe({
                    "audio_path": "/tmp/fake-v3-longform.wav",
                    "longform": True,
                })
        finally:
            gigaam_worker._MODEL = old_model
            gigaam_worker._MODE = old_mode

        self.assertTrue(result["ok"], result)
        self.assertEqual(protocol_stdout.getvalue(), "")


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
    @staticmethod
    def _script() -> str:
        """Читает one-click installer как проверяемый source-contract."""
        return (
            _KRAB_EAR_ROOT.parent / "scripts" / "install_gigaam_venv.command"
        ).read_text(encoding="utf-8")

    def test_install_script_uses_pinned_git_v3(self) -> None:
        script = self._script()
        self.assertIn("GIGAAM_COMMIT=", script, "коммит должен быть пинован")
        self.assertIn("v3_e2e_rnnt", script, "smoke должен проверять v3-модель в реестре")
        self.assertNotIn("pip install gigaam\n", script, "старый PyPI-install (v2-only) удалён")

    def test_install_script_includes_and_verifies_longform_dependencies(self) -> None:
        """Чистая one-click установка обязана поддерживать заявленный longform."""
        script = self._script()

        self.assertIn("[longform]", script)
        self.assertIn("import huggingface_hub", script)
        self.assertIn("import pyannote.audio", script)
        self.assertIn("import torchcodec", script)


if __name__ == "__main__":
    unittest.main()
