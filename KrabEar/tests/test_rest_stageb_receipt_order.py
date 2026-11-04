"""Финальный receipt учитывает teardown; actual AST-tail, не HTTP/ASR proof."""
import ast
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class StageBReceiptOrderTests(unittest.TestCase):
    def invoke(self, cleanup_violation):
        source = Path(__file__).with_name('rest_stt_stageb_probe.py')
        tree = ast.parse(source.read_text())
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
        owner = next(n for n in run.body if isinstance(n, ast.Try))
        # Сохраняем непрерывный финальный tail и исходный finally без перестановок.
        start = next(i for i, n in enumerate(owner.body) if isinstance(n, ast.Assert)
                     and isinstance(n.test, ast.Call) and isinstance(n.test.func, ast.Name)
                     and n.test.func.id == 'all' and 'is_alive' in ast.unparse(n))
        owner.body = owner.body[start:]
        run.body = run.body[run.body.index(owner):]
        boundary = SimpleNamespace(workers=[Mock(is_alive=lambda: False) for _ in range(8)],
                                   violations=[])

        def cleanup():
            if cleanup_violation:
                # Как swallowed audit denial в REST cleanup: исключение наружу не идёт.
                boundary.violations.append('synthetic teardown denial')

        rs = SimpleNamespace(_rest_engine_cleanup=cleanup,
                             transcriber=SimpleNamespace(close=lambda: True))
        namespace = dict(boundary=boundary, rs=rs, atexit=SimpleNamespace(unregister=lambda f: None),
                         json=json, cases=[], failures=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[run], type_ignores=[])),
                     str(source), 'exec'), namespace)
        output = io.StringIO()
        error = None
        with contextlib.redirect_stdout(output):
            try:
                namespace['run']()
            except AssertionError as exc:
                error = exc
        return error, output.getvalue()

    def test_swallowed_cleanup_denial_cannot_emit_success_receipt(self):
        error, output = self.invoke(cleanup_violation=True)
        self.assertIsNotNone(error)
        self.assertNotIn('REST_STAGEB_RECEIPT=', output)

    def test_clean_teardown_emits_receipt(self):
        error, output = self.invoke(cleanup_violation=False)
        self.assertIsNone(error)
        self.assertIn('REST_STAGEB_RECEIPT=', output)


if __name__ == '__main__':
    unittest.main()
