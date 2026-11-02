"""Golden-set ослышки 2026-11-02: существующий словарь должен реально кормить STT.

Живой прогон (изолированный, без IPC) дал:
  es_007 turbo: «receta médica» → «Reset America»
  es_004: oxicodona → «psicodona» / «o se podona»
  ru_021 GigaAM: openclaw → «Open Clow»
  en_001 turbo: work → war  — НЕ чиним словарём (war — легитимное слово)

Механизмы продукта, не второй словарь:
  1) phonetic_vocab — пост-STT, оба движка (engine._postprocess_text_deterministic)
  2) stt_hotwords + stt_hotwords_{ru,es,en} → Whisper initial_prompt
     (build_initial_prompt / mlx_whisper). GigaAM prompt не принимает.

GigaAM «Open Clow» лечится только phonetic-слоём, не initial_prompt.
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from unittest.mock import patch

from backend.phonetic_vocab_service import PhoneticVocabService
from core.config import settings
from core.engine import AudioEngine
from core.phonetic_vocabulary import PhoneticVocabulary
from core.pipeline.stt_gigaam import GigaAMAdapter, _GigaAMSubprocessSession


# Наблюдавшиеся ослышки golden-набора → канон существующего словаря.
GOLDEN_PAIRS = {
    "open clow": "openclaw",
    "open clevel": "openclaw",
    "psicodona": "oxicodona",
    "o se podona": "oxicodona",
    "tramodol": "tramadol",
    "Reset America": "receta médica",
}


def _vocab(entries, enabled=True):
    return PhoneticVocabulary(
        settings_get=lambda k, d: True if k == "phonetic_vocab_enabled" else d,
        entries_provider=lambda: entries,
    )


def _engine_with_runtime(runtime: dict) -> AudioEngine:
    eng = AudioEngine.__new__(AudioEngine)
    eng._settings_get = lambda k, d=None: runtime[k] if k in runtime else d
    return eng


def _prompt(eng: AudioEngine, **over) -> str:
    kw = dict(
        is_preview=False,
        context_free=False,
        domain="casual",
        extra_vocabulary=None,
        history_context=None,
        stt_hotwords=None,
        resolved_lang="ru",
        diarize=False,
    )
    kw.update(over)
    return eng._build_dynamic_prompt(object(), **kw)


class GoldenPhoneticMishearsTests(unittest.TestCase):
    def test_seeded_entries_correct_observed_mishears(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=Path(d))
            vocab = _vocab(svc.get_entries())
        for variant, canon in GOLDEN_PAIRS.items():
            with self.subTest(variant=variant):
                self.assertEqual(
                    vocab.correct(f"сказал {variant} вчера"),
                    f"сказал {canon} вчера",
                )

    def test_existing_seeded_store_gains_missing_variants(self) -> None:
        """Живой phonetic_vocab.json уже есть: новые варианты должны доехать
        без перезаписи пользовательских записей и без воскрешения удалённых."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "phonetic_vocab.json"
            path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {"canonical": "openclaw", "variants": ["openclow"]},
                            {"canonical": "oxicodona", "variants": ["oxicodon"]},
                            {"canonical": "tramadol", "variants": ["tromadol"]},
                            {
                                "canonical": "Krab Voice Gateway",
                                "variants": ["Крабвой"],
                            },
                        ],
                        "updated_at": "2026-09-17T22:55:20+00:00",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            svc = PhoneticVocabService(data_dir=Path(d))
            entries = svc.get_entries()

        by_canon = {e["canonical"]: e["variants"] for e in entries}
        self.assertIn("Krab Voice Gateway", by_canon)
        self.assertIn("open clow", [v.casefold() for v in by_canon["openclaw"]])
        self.assertIn("psicodona", [v.casefold() for v in by_canon["oxicodona"]])
        self.assertIn("tramodol", [v.casefold() for v in by_canon["tramadol"]])
        receta = by_canon.get("receta médica") or by_canon.get("receta medica")
        self.assertIsNotNone(receta)
        self.assertIn("reset america", [v.casefold() for v in receta])
        # Не воскрешаем W4-канон, которого пользователь не держал.
        self.assertNotIn("парацетамол", by_canon)
        self.assertNotIn("whisper", by_canon)

    def test_empty_user_store_is_not_refilled_with_catalog(self) -> None:
        """После удаления последней записи файл жив и пуст — каталог не вливаем."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "phonetic_vocab.json"
            path.write_text(
                json.dumps({"entries": [], "updated_at": "2026-11-02T00:00:00+00:00"}),
                encoding="utf-8",
            )
            svc = PhoneticVocabService(data_dir=Path(d))
            self.assertEqual(svc.get_entries(), [])

    def test_purge_same_process_does_not_resurrect(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=Path(d))
            self.assertGreater(len(svc.get_entries()), 0)
            svc.clear_all()
            self.assertEqual(svc.get_entries(), [])


class RuntimeLanguageHotwordsPromptTests(unittest.TestCase):
    """Герметично от живого settings.json: pydantic Settings читает его на import."""

    def test_runtime_es_hotwords_enter_whisper_prompt_for_es(self) -> None:
        """Runtime stt_hotwords_es должен попасть в initial_prompt Whisper при ES."""
        sentinel = "oxicodona_SENTINEL_GOLDEN"
        eng = _engine_with_runtime({"stt_hotwords_es": [sentinel]})
        with patch.object(settings, "STT_HOTWORDS_ES", []):
            es_prompt = _prompt(eng, resolved_lang="es")
        self.assertIn(sentinel, es_prompt)

    def test_es_hotwords_do_not_occupy_russian_prompt(self) -> None:
        sentinel = "oxicodona_SENTINEL_GOLDEN"
        eng = _engine_with_runtime({"stt_hotwords_es": [sentinel]})
        with patch.object(settings, "STT_HOTWORDS_ES", []):
            ru_prompt = _prompt(eng, resolved_lang="ru")
        self.assertNotIn(sentinel, ru_prompt)

    def test_baked_es_terms_present_even_without_runtime_list(self) -> None:
        eng = _engine_with_runtime({"stt_hotwords_es": []})
        with patch.object(settings, "STT_HOTWORDS_ES", []):
            es_prompt = _prompt(eng, resolved_lang="es")
        for term in ("oxicodona", "tramadol", "receta médica", "pregabalina"):
            with self.subTest(term=term):
                self.assertIn(term, es_prompt)

    def test_gigaam_transcribe_has_no_initial_prompt(self) -> None:
        """GigaAM структурно не читает Whisper-словарь: «Open Clow» этим не лечится."""
        for fn in (GigaAMAdapter.transcribe, _GigaAMSubprocessSession.transcribe):
            params = inspect.signature(fn).parameters
            self.assertNotIn("initial_prompt", params, fn.__qualname__)
            self.assertNotIn("hotwords", params, fn.__qualname__)
            self.assertNotIn("stt_hotwords", params, fn.__qualname__)


if __name__ == "__main__":
    unittest.main()
