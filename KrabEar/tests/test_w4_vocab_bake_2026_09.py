"""F1: лексика W4 запечена (phonetic-пары, hotwords-seed, фильтр dimatorzok)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.phonetic_vocabulary import PhoneticVocabulary

# вариант -> канон (утверждено владельцем 17.09; maby и кетопрофеновая
# ловушка исключены осознанно — см. карточку).
PAIRS = {
    "openclow": "openclaw",
    "травмадол": "трамадол",
    "траммадол": "трамадол",
    "тромадол": "трамадол",
    "рамадол": "трамадол",
    "пегабалин": "прегабалин",
    "пегбалин": "прегабалин",
    "прегалин": "прегабалин",
    "пригаболин": "прегабалин",
    "парацетамон": "парацетамол",
    "парацемолом": "парацетамол",
    "oxicodon": "oxicodona",
    "oxycodona": "oxicodona",
    "oxipodona": "oxicodona",
    "oxigodona": "oxicodona",
    "pregabalin": "pregabalina",
    "pregabalino": "pregabalina",
    "pregabalín": "pregabalina",
    "pegabarina": "pregabalina",
    "fartestamol": "paracetamol",
    "ramadol": "tramadol",
    "tromadol": "tramadol",
    "dramadol": "tramadol",
    "висперед": "whisper",
    "лрд": "p0lrd",
    "lrd": "p0lrd",
    "оxicodona": "oxicodona",
    "пregabalina": "pregabalina",
}

CLEAN_SENTENCES = [
    "Принимаю трамадол утром и прегабалин вечером",
    "Take tramadol and pregabalina daily",
    "Открой оверлей и проверь openclaw",
]


def _vocab(entries):
    return PhoneticVocabulary(
        settings_get=lambda k, d: True if k == "phonetic_vocab_enabled" else d,
        entries_provider=lambda: entries,
    )


class W4VocabBakeTests(unittest.TestCase):
    def test_live_entries_cover_approved_pairs(self) -> None:
        """Запечённые entries покрывают все утверждённые пары (через live-сервис)."""
        from backend.phonetic_vocab_service import PhoneticVocabService
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=_Path(d))
            # seed: записать entries кодом из Task 2 (дифф seed-файла), прочитать назад
            seeded = {(e["canonical"], v) for e in svc.get_entries() for v in e.get("variants", [])}
        for variant, canon in PAIRS.items():
            self.assertIn((canon, variant), seeded, variant)

    def test_pairs_correct_text(self) -> None:
        from backend.phonetic_vocab_service import PhoneticVocabService
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=_Path(d))
            vocab = _vocab(svc.get_entries())
        for variant, canon in PAIRS.items():
            with self.subTest(variant=variant):
                self.assertEqual(vocab.correct(f"сказал {variant} вчера"), f"сказал {canon} вчера")

    def test_clean_text_untouched_and_idempotent(self) -> None:
        from backend.phonetic_vocab_service import PhoneticVocabService
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as d:
            svc = PhoneticVocabService(data_dir=_Path(d))
            vocab = _vocab(svc.get_entries())
        for s in CLEAN_SENTENCES:
            with self.subTest(s=s):
                once = vocab.correct(s)
                self.assertEqual(once, s)
                self.assertEqual(vocab.correct(once), once)

    def test_disabled_is_passthrough(self) -> None:
        vocab = PhoneticVocabulary(
            settings_get=lambda k, d: False,
            entries_provider=lambda: [{"canonical": "трамадол", "variants": ["травмадол"]}],
        )
        self.assertEqual(vocab.correct("пил травмадол"), "пил травмадол")

    def test_bare_dimatorzok_tail_is_stripped(self) -> None:
        from core.utils import TextUtils
        self.assertEqual(TextUtils._strip_hallucinations("текст диктовки диматорзок"), "текст диктовки")
        self.assertEqual(TextUtils._strip_hallucinations("Диматорзок"), "")
        self.assertEqual(TextUtils._strip_hallucinations("торговля идёт бойко"), "торговля идёт бойко")

    def test_seed_hotwords_contain_owner_terms(self) -> None:
        # seed_hotwords(settings_svc) пишет в settings и возвращает int —
        # здесь проверяем дефолтный список (плоский), из которого seed берёт слова.
        from backend.default_hotwords import get_default_hotwords
        flat = get_default_hotwords()
        for w in ("оверлей", "openclaw"):
            self.assertIn(w, flat)

    def test_rest_engine_wired_via_ast(self) -> None:
        """REST-движок получает провайдер и настройки (AST, без импорта rest_server —
        модуль тяжёлый: engine и store конструируются на import)."""
        import ast as _ast
        src = (PROJECT_ROOT / "backend" / "rest_server.py").read_text(encoding="utf-8")
        tree = _ast.parse(src)
        assigns = [
            (t.attr, n.lineno) for n in _ast.walk(tree)
            if isinstance(n, _ast.Assign)
            for t in n.targets
            if isinstance(t, _ast.Attribute) and t.attr in {"_phonetic_provider", "_settings_get"}
        ]
        attrs = {a for a, _ in assigns}
        self.assertIn("_phonetic_provider", attrs, "REST-движку не назначен phonetic-провайдер")
        self.assertIn("_settings_get", attrs, "REST-движку не назначены настройки")


if __name__ == "__main__":
    unittest.main()
