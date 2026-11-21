"""KE05A-14 / KE05A-02: auto_learned не врёт, строка "false" не включает самообучение, слова не в логе.

``_maybe_auto_learn_word`` возвращал True даже когда ``handle_set_settings({"stt_hotwords": ...})``
отвечал ``{"ok": False}``; ``bool("false")`` включал функцию; слова из исправлений писались в DEBUG-лог.
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_ops_service import LLMOpsService  # noqa: E402


def _svc(settings: dict, set_result):
    settings_svc = MagicMock()
    settings_svc.cached_settings.return_value = settings
    settings_svc.handle_set_settings.return_value = set_result
    return LLMOpsService(store=MagicMock(), settings_svc=settings_svc, transcriber=None), settings_svc


_ON = {"auto_learn_corrections_enabled": True, "stt_hotwords": []}


class AutoLearnHonestResult(unittest.TestCase):
    def test_rejected_set_settings_is_not_reported_as_learned(self):
        svc, _ = _svc(dict(_ON), {"ok": False, "error": "validation"})
        self.assertFalse(svc._maybe_auto_learn_word("новоеслово", "старое"))

    def test_accepted_set_settings_is_learned(self):
        svc, ss = _svc(dict(_ON), {"ok": True})
        self.assertTrue(svc._maybe_auto_learn_word("новоеслово", "старое"))
        ss.handle_set_settings.assert_called_once_with({"stt_hotwords": ["новоеслово"]})

    def test_legacy_none_result_keeps_old_behaviour(self):
        svc, _ = _svc(dict(_ON), None)
        self.assertTrue(svc._maybe_auto_learn_word("новоеслово", "старое"))

    def test_string_false_does_not_enable_auto_learn(self):
        svc, ss = _svc({"auto_learn_corrections_enabled": "false", "stt_hotwords": []}, {"ok": True})
        self.assertFalse(svc._maybe_auto_learn_word("новоеслово", "старое"))
        ss.handle_set_settings.assert_not_called()

    def test_words_are_not_written_to_debug_log(self):
        long_word = "ОченьДлинноеСекретноеСлово" * 4  # > 60 символов — ветка «skipping long token»
        svc, _ = _svc(dict(_ON), {"ok": True})
        with self.assertLogs("backend.llm_ops_service", level=logging.DEBUG) as cm:
            logging.getLogger("backend.llm_ops_service").debug("sentinel")
            svc._maybe_auto_learn_word(long_word, "старое")
            svc._maybe_auto_learn_word("а б в г д", "старое")
            svc._maybe_auto_learn_word("секретноеимя", "старое")
        joined = "\n".join(cm.output)
        self.assertNotIn("ОченьДлинное", joined)
        self.assertNotIn("секретноеимя", joined)
        self.assertNotIn("а б в г д", joined)


if __name__ == "__main__":
    unittest.main()
