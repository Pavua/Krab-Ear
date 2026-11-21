"""KE05A-13 / KE05A-08 / KE05A-02 / KE05A-18: замена слова и приём аудио.

* ``re.subn(new, ...)`` трактовал новое слово как шаблон замены (``\\1``, ``\\g<0>``);
* ложный ``ok=True``, если ``update_history_item_text`` вернул False;
* исправленные слова писались в INFO-лог (содержимое транскрипта);
* ``base64.b64decode`` без ``validate=True`` принимал мусор, а ``bool("false")`` форсировал flush.
"""

from __future__ import annotations

import base64
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.live_subs_service import LiveSubsService  # noqa: E402
from backend.llm_ops_service import LLMOpsService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class ReplaceWordHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = LLMOpsService(store=self.store, settings_svc=None, transcriber=None)

    def _add(self, text: str) -> str:
        self.store.add_history_item(text=text, paste_status="ok")
        with self.store._lock():
            return self.store._load_active_items_unlocked()[-1].id

    def _text(self) -> str:
        with self.store._lock():
            return self.store._load_active_items_unlocked()[-1].text

    def test_new_word_is_literal_not_a_regex_template(self):
        hid = self._add("принял препарат кот вчера")
        for new in (r"\1", r"\g<0>", r"a\nb", "\\\\", r"\U"):
            res = self.svc.handle_replace_word_in_last_transcript(
                {"old_word": "кот", "new_word": new, "history_id": hid}
            )
            self.assertTrue(res["ok"], f"{new!r}: {res}")
            self.assertIn(new, self._text())
            self.assertEqual(res["replaced_count"], 1)
            # вернуть исходный текст для следующей итерации
            self.store.update_history_item_text(hid, "принял препарат кот вчера")

    def test_failed_update_is_not_reported_as_ok(self):
        hid = self._add("слово кот тут")
        with patch.object(self.store, "update_history_item_text", return_value=False):
            res = self.svc.handle_replace_word_in_last_transcript(
                {"old_word": "кот", "new_word": "пёс", "history_id": hid}
            )
        self.assertFalse(res["ok"])
        self.assertNotIn("new_text", res)
        self.assertNotIn("auto_learned", res)

    def test_replaced_words_are_not_logged(self):
        hid = self._add("слово ЕкзотичноеСлово тут")
        with self.assertLogs("backend.llm_ops_service", level=logging.DEBUG) as cm, patch.object(
            self.svc, "_maybe_auto_learn_word", return_value=False
        ):
            self.svc.handle_replace_word_in_last_transcript(
                {"old_word": "ЕкзотичноеСлово", "new_word": "СекретноеИмя", "history_id": hid}
            )
            logging.getLogger("backend.llm_ops_service").debug("sentinel")
        joined = "\n".join(cm.output)
        self.assertNotIn("ЕкзотичноеСлово", joined)
        self.assertNotIn("СекретноеИмя", joined)


def _live_service() -> LiveSubsService:
    svc = LiveSubsService.__new__(LiveSubsService)
    return svc


class IngestInputParsing(unittest.TestCase):
    def _service_with_spy(self):
        from backend import live_subs_service as mod

        self.assertTrue(hasattr(mod, "LiveSubsService"))
        svc = LiveSubsService.__new__(LiveSubsService)
        svc._read_capture_epoch = None
        svc._admit_ingest = lambda epoch: (1, None)
        svc._reject_ingest = lambda reason: {"status": "rejected", "reason": reason}
        svc.buffer_duration_sec = lambda sr: 0.0
        seen: dict = {}

        def fake_ingest(**kw):
            seen.update(kw)
            return None

        svc.ingest = fake_ingest
        return svc, seen

    def test_garbage_base64_is_rejected(self):
        svc, seen = self._service_with_spy()
        for bad in ("@@@@", "abc$def==", "not base64 at all!"):
            with self.assertRaises(ValueError):
                svc.handle_ingest({"audio_chunk": bad})
        self.assertEqual(seen, {})

    def test_valid_base64_still_accepted_even_with_line_breaks(self):
        svc, seen = self._service_with_spy()
        raw = np.zeros(160, dtype=np.int16).tobytes()
        b64 = base64.b64encode(raw).decode()
        wrapped = b64[:20] + "\n" + b64[20:]
        svc.handle_ingest({"audio_chunk": wrapped})
        self.assertEqual(seen["audio_bytes"], raw)

    def test_string_false_is_final_does_not_force_flush(self):
        svc, seen = self._service_with_spy()
        raw = np.zeros(160, dtype=np.int16).tobytes()
        svc.handle_ingest({"audio_chunk": base64.b64encode(raw).decode(), "is_final": "false"})
        self.assertIs(seen["is_final"], False)

    def test_string_true_is_final_flushes(self):
        svc, seen = self._service_with_spy()
        raw = np.zeros(160, dtype=np.int16).tobytes()
        svc.handle_ingest({"audio_chunk": base64.b64encode(raw).decode(), "is_final": "true"})
        self.assertIs(seen["is_final"], True)


if __name__ == "__main__":
    unittest.main()
