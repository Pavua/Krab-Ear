"""Контракт внешних потребителей Krab Ear (Краб, Voice Gateway).

Соседние проекты зовут Ear по именам, которых не видит их CI. 17.09.2026
сверка стыка нашла у Краба вызов несуществующего IPC `get_history` и проверку
здоровья на порту, который никто не слушает. Гард держит сторону Ear: всё из
docs/contracts/ear-external-surface.md существует в исходниках. Разбор AST,
без импорта backend — не нужны ML-зависимости.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

KRABEAR = Path(__file__).resolve().parents[1]
CONTRACT_DOC = KRABEAR.parent / "docs" / "contracts" / "ear-external-surface.md"

IPC_METHODS = ("ping", "synthesize_speech", "get_history_page")

# полный путь -> (переменная blueprint, путь внутри blueprint, HTTP-метод)
REST_ROUTES = {
    "/health": ("monitoring_blp", "/health", "GET"),
    "/v1/stt/transcribe": ("v1_blp", "/stt/transcribe", "POST"),
    "/v1/tts/synthesize": ("v1_blp", "/tts/synthesize", "POST"),
}

# (файл относительно KrabEar/, функция, ключ, который читает потребитель)
RESPONSE_KEYS = (
    ("backend/health_check_service.py", "handle_ping", "status"),
    ("backend/tts_service.py", "handle_synthesize_speech", "wav_bytes_b64"),
    ("backend/history_service.py", "handle_get_history_page", "items"),
    ("backend/rest_server.py", "synthesize_speech", "wav_bytes_b64"),
    ("backend/rest_server.py", "transcribe_audio", "text"),
    ("backend/rest_server.py", "transcribe_audio", "segments"),
)


def functions_named(tree: ast.AST, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def dict_string_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for key in sub.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    return keys


def blueprint_prefixes(tree: ast.AST) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "Blueprint"):
            continue
        for kw in node.value.keywords:
            if kw.arg == "url_prefix" and isinstance(kw.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        prefixes[target.id] = kw.value.value
    return prefixes


def registered_routes(tree: ast.AST) -> set[tuple[str, str, str]]:
    """(blueprint, путь, HTTP-метод) из декораторов `@<bp>.route("/p", methods=[...])`."""
    routes: set[tuple[str, str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route"
                and isinstance(dec.func.value, ast.Name)
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                continue
            methods = ["GET"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
            for method in methods:
                routes.add((dec.func.value.id, dec.args[0].value, method))
    return routes


def parse(rel: str) -> ast.AST:
    return ast.parse((KRABEAR / rel).read_text(encoding="utf-8"))


class ExternalSurfaceContractTests(unittest.TestCase):
    def test_ipc_methods_are_dispatched(self) -> None:
        builders = functions_named(parse("backend/service.py"), "_build_dispatch_table")
        self.assertEqual(len(builders), 1, "ожидался ровно один _build_dispatch_table")
        keys = dict_string_keys(builders[0])
        missing = [m for m in IPC_METHODS if m not in keys]
        self.assertEqual(missing, [], "внешние потребители зовут эти IPC-методы — см. контракт")

    def test_rest_routes_are_registered(self) -> None:
        tree = parse("backend/rest_server.py")
        prefixes = blueprint_prefixes(tree)
        routes = registered_routes(tree)
        problems: list[str] = []
        for full_path, (bp, path, method) in REST_ROUTES.items():
            if prefixes.get(bp, None) is None:
                problems.append(f"{full_path}: blueprint {bp} не найден")
                continue
            if prefixes[bp] + path != full_path:
                problems.append(f"{full_path}: префикс {bp}={prefixes[bp]!r} даёт {prefixes[bp] + path}")
            if (bp, path, method) not in routes:
                problems.append(f"{full_path}: нет @{bp}.route({path!r}, methods=[{method!r}])")
        self.assertEqual(problems, [])

    def test_response_keys_read_by_consumers_exist(self) -> None:
        problems: list[str] = []
        for rel, func, key in RESPONSE_KEYS:
            defs = functions_named(parse(rel), func)
            if len(defs) != 1:
                problems.append(f"{rel}:{func}: найдено определений {len(defs)}, ожидалось 1")
                continue
            if key not in dict_string_keys(defs[0]):
                problems.append(f"{rel}:{func}: нет ключа {key!r} в словарях ответа")
        self.assertEqual(problems, [])

    def test_every_guarded_name_is_documented(self) -> None:
        text = CONTRACT_DOC.read_text(encoding="utf-8")
        names = list(IPC_METHODS) + list(REST_ROUTES) + sorted({k for _, _, k in RESPONSE_KEYS})
        undocumented = [n for n in names if f"`{n}" not in text and f"{n}`" not in text]
        self.assertEqual(undocumented, [], "гард и документ контракта разошлись")


class DetectorSelfTest(unittest.TestCase):
    """Гард, тихо переставший находить нарушение, отчитывался бы зелёным вечно."""

    def test_route_detector_sees_prefix_and_method(self) -> None:
        tree = ast.parse(
            'v1_blp = Blueprint("v1", __name__, url_prefix="/v1")\n'
            '@v1_blp.route("/stt/transcribe", methods=["GET"])\n'
            "def transcribe_audio():\n"
            '    return {"text": ""}\n'
        )
        self.assertEqual(blueprint_prefixes(tree), {"v1_blp": "/v1"})
        self.assertNotIn(("v1_blp", "/stt/transcribe", "POST"), registered_routes(tree))
        self.assertIn(("v1_blp", "/stt/transcribe", "GET"), registered_routes(tree))

    def test_key_detector_misses_renamed_key(self) -> None:
        tree = ast.parse('def handle_get_history_page(p):\n    return {"itemz": []}\n')
        (func,) = functions_named(tree, "handle_get_history_page")
        self.assertNotIn("items", dict_string_keys(func))


if __name__ == "__main__":
    unittest.main()
