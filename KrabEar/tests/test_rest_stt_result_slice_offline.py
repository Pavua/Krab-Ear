"""Actual REST post-transcriber slice; без импорта приложения/Flask/audio.

Проверяет только обработку уже полученного engine-result, не HTTP wiring,
executor drain или реальные privacy reads. Узлы берутся непрерывно из source.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

SOURCE = Path(__file__).resolve().parents[1] / "backend" / "rest_server.py"


def result_handler():
    tree = ast.parse(SOURCE.read_text())
    handler = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == "transcribe_audio")
    candidates = [n for n in ast.walk(handler) if isinstance(n, ast.Try)
                  and any(isinstance(s, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "elapsed_sec"
                                  for t in s.targets) for s in n.body)]
    if len(candidates) != 1:
        raise AssertionError("post-transcriber owner changed")
    body = candidates[0].body
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "elapsed_sec" for t in n.targets))
    suffix = body[start + 1:]
    if not isinstance(suffix[-1], ast.Return):
        raise AssertionError("result slice must end at actual response return")
    wrapper = ast.parse("def run():\n    pass\n")
    wrapper.body[0].body = suffix
    namespace = {}
    return compile(ast.fix_missing_locations(wrapper), str(SOURCE), "exec"), namespace


class RestResultSliceTests(unittest.TestCase):
    def invoke(self, result, call=False, private=False):
        code, ns = result_handler()
        deps = SimpleNamespace(store=Mock(), metrics=Mock())
        ns.update(result=result, call_profile=call, call_deadline=None,
                  _call_profile_privacy_enabled=lambda: private,
                  _privacy_mode_enabled=lambda: private, persist_history=not call,
                  chat_id=None, message_id=None, elapsed_sec=0.01, deps=deps,
                  jsonify=lambda value: value)
        exec(code, ns)
        response = ns["run"]()
        body, status = response if isinstance(response, tuple) else (response, 200)
        return body, status, deps

    def test_engine_error_is_not_success(self):
        for call in (False, True):
            for result in ({"status": "error", "error": "PRIVATE", "text": "PRIVATE"},
                           {"status": "error", "text": ""},
                           {"error": "PRIVATE", "text": ""}):
                with self.subTest(call=call, result=result):
                    body, status, deps = self.invoke(result, call)
                    self.assertEqual(status, 503)
                    self.assertEqual(body, {"status": "error", "error": "stt_failed", "text": ""})
                    deps.store.add_history_item.assert_not_called()
                    deps.metrics.record.assert_not_called()

    def test_privacy_wins(self):
        for call in (False, True):
            with self.subTest(call=call):
                body, status, deps = self.invoke({"status": "error", "error": "PRIVATE"}, call, True)
                self.assertEqual(status, 403)
                self.assertEqual(body, {"skipped": "privacy_mode", "text": ""})
                deps.store.add_history_item.assert_not_called()
                deps.metrics.record.assert_not_called()

    def test_success_and_silence_preserved(self):
        for call in (False, True):
            for result in ({"text": "", "reason": "vad_skip"},
                           {"text": "", "reason": "empty_audio"},
                           {"status": "ok", "error": None, "text": "ok"}):
                with self.subTest(call=call, result=result):
                    body, status, deps = self.invoke(result, call)
                    self.assertEqual(status, 200)
                    self.assertEqual(body["text"], result["text"])
                    self.assertEqual(body["status"], "ok")
                    deps.metrics.record.assert_called_once()
                    if not result["text"]:
                        self.assertEqual(body["reason"], result["reason"])


if __name__ == "__main__":
    unittest.main()
