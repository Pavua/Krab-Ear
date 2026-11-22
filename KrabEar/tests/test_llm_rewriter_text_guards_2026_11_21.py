"""KE05B-11 / KE05B-14: контроль ответа LLM.

* ``fix_punctuation_only`` сравнивал слова как МНОЖЕСТВА: перестановка («да нет да» →
  «нет да да») проходила как «исправленная пунктуация».
* ``content`` не строка (list в мультимодальном формате) ронял ``rewrite``/``fix_punctuation_only``/
  ``summarize`` AttributeError'ом, хотя контракт — «никогда не raises».
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_rewriter import LLMRewriter  # noqa: E402


def _rw() -> LLMRewriter:
    rw = LLMRewriter(base_url="http://localhost:1234/v1", api_key="k", model="m")
    return rw


def _resp(content) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    return r


class PunctuationGuardKeepsOrder(unittest.TestCase):
    def test_reordered_words_are_rejected(self):
        rw = _rw()
        rw._session.post = MagicMock(return_value=_resp("нет да да"))
        self.assertIsNone(rw.fix_punctuation_only("да нет да"))

    def test_duplicate_swapped_for_other_word_is_rejected(self):
        rw = _rw()
        rw._session.post = MagicMock(return_value=_resp("да да нет"))
        self.assertIsNone(rw.fix_punctuation_only("нет да нет"))

    def test_legit_punctuation_is_accepted(self):
        rw = _rw()
        rw._session.post = MagicMock(return_value=_resp("Привет, как дела?"))
        self.assertEqual(rw.fix_punctuation_only("привет как дела"), "Привет, как дела?")

    def test_case_and_edge_punctuation_changes_are_accepted(self):
        rw = _rw()
        rw._session.post = MagicMock(return_value=_resp("Да. Нет, да!"))
        self.assertEqual(rw.fix_punctuation_only("да нет да"), "Да. Нет, да!")


class NonStringContentNeverRaises(unittest.TestCase):
    _BAD = ([{"type": "text", "text": "привет"}], {"a": 1}, 42)

    def test_rewrite(self):
        for bad in self._BAD:
            rw = _rw()
            rw._session.post = MagicMock(return_value=_resp(bad))
            res = rw.rewrite("привет мир")
            self.assertFalse(res.ok, repr(bad))
            self.assertEqual(res.fallback_reason, "parse_error", repr(bad))

    def test_fix_punctuation_only(self):
        for bad in self._BAD:
            rw = _rw()
            rw._session.post = MagicMock(return_value=_resp(bad))
            self.assertIsNone(rw.fix_punctuation_only("привет мир"), repr(bad))

    def test_summarize(self):
        for bad in self._BAD:
            rw = _rw()
            rw._session.post = MagicMock(return_value=_resp(bad))
            with patch("backend.lm_studio_lifecycle.probe_loaded_chat_models", return_value=None):
                res = rw.summarize("Длинный текст для краткого пересказа. Ещё одно предложение.")
            self.assertFalse(res.ok, repr(bad))
            self.assertEqual(res.fallback_reason, "parse_error", repr(bad))

    def test_failure_is_counted_by_breaker(self):
        for call in (
            lambda rw: rw.rewrite("привет мир"),
            lambda rw: rw.fix_punctuation_only("привет мир"),
        ):
            rw = _rw()
            rw._session.post = MagicMock(return_value=_resp([1]))
            with patch.object(rw._circuit, "record_failure") as failure:
                call(rw)
            failure.assert_called()


if __name__ == "__main__":
    unittest.main()
