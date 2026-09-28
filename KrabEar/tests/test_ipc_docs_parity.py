"""Паритет IPC-документации с диспетчером (волна 0.4).

Каждый ключ _build_dispatch_table обязан быть задокументирован в
docs/IPC_API_REFERENCE.md. Исключение: clear_privacy_audit_log — намеренно
удалён из dispatch (W957 SECURITY, service.py:3167), в доке обязан нести
маркер удаления, а не молчать.
"""
from __future__ import annotations

import ast
import functools
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICE = REPO / "KrabEar" / "backend" / "service.py"
DOC = REPO / "docs" / "IPC_API_REFERENCE.md"
BACKEND = REPO / "KrabEar" / "backend"

# Документирован, но НЕ в dispatch — и это правильно (возврат запрещён).
KNOWN_REMOVED = {"clear_privacy_audit_log"}
REMOVED_MARKER = "намеренно удалён"

# Заголовок секции: первый backtick-токен (возможно, список через ` / `) + хвост.
# Вид хвоста и количество токенов отличают секцию МЕТОДА от секции ПОЛЯ ОТВЕТА.
_SECTION_RE = re.compile(r"^### ((?:`[\w]+`(?: / )?)+)(.*)$", re.M)


def _dispatch_entries() -> dict[str, ast.AST]:
    """Таблица dispatch как {имя: AST-значения} (сырое, для разрешения цепочки)."""
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    builders = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_build_dispatch_table"
    ]
    assert len(builders) == 1, "ожидался ровно один _build_dispatch_table"
    tables = [
        n.value for n in ast.walk(builders[0])
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
        and sum(isinstance(k, ast.Constant) and isinstance(k.value, str)
                for k in n.value.keys) > 50
    ]
    assert len(tables) == 1, "таблица dispatch не найдена"
    return {k.value: v for k, v in zip(tables[0].keys, tables[0].values)
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


@functools.lru_cache(maxsize=1)
def dispatch_keys() -> set[str]:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    builders = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_build_dispatch_table"
    ]
    assert len(builders) == 1, "ожидался ровно один _build_dispatch_table"
    returns = [n for n in ast.walk(builders[0]) if isinstance(n, ast.Return)]
    tables = [
        n.value for n in returns
        if isinstance(n.value, ast.Dict)
        and sum(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in n.value.keys) > 50
    ]
    assert len(tables) == 1, "таблица dispatch не найдена"
    return {k.value for k in tables[0].keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


@functools.lru_cache(maxsize=1)
def documented_names() -> set[str]:
    """Имена методов, задокументированных как СЕКЦИИ (`### \\`method\\``).

    Регекс A5.2c1: волна документирова поля ОТВЕТА (`deletion_ledger_purged`,
    `encryption_key_shredded`, `stale_copies_removed`) и коды `errors` тоже
    заголовками `###`. Старый regex брал ПЕРВЫЙ токен ЛЮБОГО `###` и считал его
    именем метода, поэтому `### \\`compact\\` / \\`history_ciphertext\\` / …`
    превращался в «документированный метод compact», которого в dispatch нет.

    Теперь вид заголовка решает, метод это или под-секция поля:
      * один токен, и он есть в dispatch -> секция МЕТОДА;
      * один токен, его нет в dispatch   -> под-секция ПОЛЯ (проверяется по коду)
        либо KNOWN_REMOVED — «намеренно удалённый» метод;
      * несколько токенов через ` / `    -> под-секция ПОЛЯ/КОДА ОШИБКИ.
    Это структурный признак, а не allowlist имён: новое поле ответа попадёт в
    documented_response_names() и будет проверено по РЕАЛЬНОМУ коду.
    """
    dispatch = dispatch_keys()
    names: set[str] = set()
    for m in _SECTION_RE.finditer(DOC.read_text(encoding="utf-8")):
        tokens = re.findall(r"`([\w]+)`", m.group(1))
        if len(tokens) == 1 and (tokens[0] in dispatch or tokens[0] in KNOWN_REMOVED):
            names.add(tokens[0])
    return names


@functools.lru_cache(maxsize=1)
def documented_response_names() -> dict[str, str]:
    """Поля/коды ошибок, задокументированные как `###` СЕКЦИИ: {имя -> секция-метод}."""
    out: dict[str, str] = {}
    current = ""
    # dispatch_keys() парсит service.py: без кэша ниже он вызывался на КАЖДОЙ
    # строке доки и давал 10.5с на функцию вместо 0.03с.
    dispatch = dispatch_keys()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        m = _SECTION_RE.match(line)
        if not m:
            continue
        tokens = re.findall(r"`([\w]+)`", m.group(1))
        if len(tokens) == 1 and (tokens[0] in dispatch or tokens[0] in KNOWN_REMOVED):
            current = tokens[0]
            continue
        for tok in tokens:
            out.setdefault(tok, current or "?")
    return out


@functools.lru_cache(maxsize=1)
def _service_module_index() -> dict[str, str]:
    """{leaf-имя метода -> "backend.<модуль>"} по всем сервисным модулям.

    Один проход вместо сканирования всех модулей на каждый из 360 методов:
    гейт и так стал в ~8 раз медленнее (3.5с -> 30с), а это цена НЕ за ослабление
    проверки, а за её глубину. Индекс строится один раз и кэшируется.
    """
    index: dict[str, str] = {}
    for candidate in sorted(BACKEND.glob("*service*.py")):
        tree = ast.parse(candidate.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("handle_"):
                index.setdefault(node.name[len("handle_"):], "backend." + candidate.stem)
            else:
                index.setdefault(node.name, "backend." + candidate.stem)
    return index


@functools.lru_cache(maxsize=1)
def _handler_return_keys() -> dict[str, frozenset[str]]:
    """Ключи словарей, которые методы РЕАЛЬНО возвращают (по dispatch -> сервис).

    Точка зрения гейта исправлена осознанно (A5.2c1): `_build_dispatch_table`
    отдаёт ТОНКИЕ обёртки (`self._handle_purge_all_data`), а контракт ответа
    формирует `HistoryService.handle_purge_all_data`. Гейт, смотревший только
    на объявление в service.py, не видел ни одного поля ответа. Теперь он идёт
    по цепочке `dispatch -> атрибут -> класс -> модуль -> функция` и читает
    настоящий `return {...}`, поэтому удаление поля из ответа валит гейт.
    """
    svc_tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    attr2cls: dict[str, str] = {}
    for node in ast.walk(svc_tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == "self"
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)):
            attr2cls[node.targets[0].attr] = node.value.func.id
    imports: dict[str, str] = {}
    for node in ast.walk(svc_tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("backend."):
            for alias in node.names:
                imports[alias.asname or alias.name] = "backend." + node.module.rsplit(".", 1)[-1]
    cls2mod = {cls: imports.get(cls) for cls in attr2cls.values()}

    out: dict[str, set[str]] = {}
    for name, value in _dispatch_entries().items():
        # dispatch -> self._handle_purge_all_data -> self._history.handle_purge_all_data
        if not (isinstance(value, ast.Attribute) and value.attr.startswith("_handle_")):
            continue
        wrapper = _wrapper_fns(svc_tree, value.attr)
        if not wrapper:
            continue
        for fn in wrapper:
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if not node.func.attr.startswith("handle_"):
                    continue
                leaf = node.func.attr[len("handle_"):]
                # self._history -> HistoryService -> backend.history_service
                mod = None
                if isinstance(node.func.value, ast.Attribute) and \
                        isinstance(node.func.value.value, ast.Name) and \
                        node.func.value.value.id == "self":
                    cls = attr2cls.get(node.func.value.attr)
                    mod = cls2mod.get(cls) if cls else None
                if not mod:
                    mod = _service_module_index().get(leaf)
                if not mod:
                    continue
                path = BACKEND / (mod.split(".")[-1] + ".py")
                if not path.is_file():
                    continue
                for impl in _functions_named(ast.parse(path.read_text(encoding="utf-8")), leaf):
                    keys = _largest_dict_keys(impl)
                    if keys:
                        out.setdefault(name, set()).update(keys)
    return out


def _wrapper_fns(tree: ast.Module, attr: str) -> list[ast.AST]:
    """Функция-обёртка по ТОЧНОМУ имени атрибута (`_handle_purge_all_data`)."""
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == attr]


def _functions_named(tree: ast.Module, name: str) -> list[ast.AST]:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            hits += [m for m in node.body
                     if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and m.name in (name, f"handle_{name}")]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                name, f"handle_{name}"):
            hits.append(node)
    return hits


def _largest_dict_keys(fn: ast.AST) -> set[str]:
    """Ключи самого «богатого» return-dict функции: успешный ответ — он и полнее."""
    best: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = {k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if len(keys) > len(best):
                best = keys
    return best


def all_documented_heading_names() -> set[str]:
    """ПЕРВЫЙ токен КАЖДОГО `###`-заголовка — то, что видел бы старый regex.

    Нужен отдельной от `documented_names()`: инвариант гейта — «у каждого
    заголовка есть оправдание В КОДЕ», а не «каждый заголовок является
    методом». Смешивать эти два множества нельзя: иначе проверка
    «документированного метода нет в dispatch» стала бы тавтологией.
    """
    out: set[str] = set()
    for m in _SECTION_RE.finditer(DOC.read_text(encoding="utf-8")):
        tokens = re.findall(r"`([\w]+)`", m.group(1))
        if tokens:
            out.add(tokens[0])
    return out


def _unproven_names() -> list[str]:
    """Заголовки, не оправданные ничем в коде: ни метод, ни поле ответа, ни код."""
    dispatch = dispatch_keys()
    returns = _handler_return_keys()
    codes = _purge_error_codes()
    documented = documented_response_names()
    unproven: list[str] = []
    for name in sorted(all_documented_heading_names()):
        if name in dispatch or name in KNOWN_REMOVED:
            continue
        method = documented.get(name)
        if name in returns.get(method or "", set()) or name in codes:
            continue
        unproven.append(f"{name} (секция {method})")
    return unproven


class IpcDocsParityTests(unittest.TestCase):
    def test_every_dispatched_method_is_documented(self) -> None:
        missing = sorted(dispatch_keys() - documented_names())
        self.assertEqual(missing, [])

    def test_every_documented_name_exists_or_is_marked_removed(self) -> None:
        """Каждый `###`-заголовок оправдан КОДОМ: метод, маркер удаления или поле.

        Регекс A5.2c1: старый вариант сравнивал «документированные имена» только с
        dispatch, поэтому и падал (поля ответа не методы), и — что хуже — после
        наивной починки стал бы тавтологией. Здесь оправдание трёх видов, и
        неоправданное имя валит гейт независимо от того, метод это или поле.
        """
        unproven = _unproven_names()
        self.assertEqual(unproven, [],
                         f"задокументированные имена не оправданы кодом: {unproven}")
        text = DOC.read_text(encoding="utf-8")
        for name in sorted(all_documented_heading_names() & KNOWN_REMOVED):
            section = text.split(f"### `{name}`", 1)[1].split("\n## ", 1)[0]
            self.assertIn(REMOVED_MARKER, section.lower(),
                          f"{name}: секция обязана нести маркер удаления")

    def test_documented_response_fields_exist_in_real_handler_response(self) -> None:
        """Документированное поле ответа обязано быть в РЕАЛЬНОМ коде.

        Это и есть смысл гейта: `deletion_ledger_purged`,
        `encryption_key_shredded`, `stale_copies_removed` и коды `errors`
        (`compact`, `history_ciphertext`, `tombstones`, `tombstone_registry`)
        проверяются по `return {...}` хендлера, а не терпятся.
        """
        documented = documented_response_names()
        self.assertTrue(
            documented,
            "гейт обязан видеть задокументированные поля ответа; их нет — "
            "проверка молча ничего не делает",
        )
        returns = _handler_return_keys()
        unproven: list[str] = []
        for name, method in sorted(documented.items()):
            # Коды `errors` живут в списке secondary_errors, а не в ключах ответа.
            if _is_error_code(name, method, returns):
                continue
            if name in returns.get(method, set()) or name in returns.get(_handler_alias(method), set()):
                continue
            unproven.append(f"{name} (секция {method})")
        self.assertEqual(unproven, [],
                         f"задокументированные поля не найдены в реальном ответе: {unproven}")

    def test_purge_response_fields_are_covered_not_tolerated(self) -> None:
        """A5.2c1: 4 имени обязаны быть ПОКРЫТЫ гейтом, а не пропущены им."""
        documented = documented_response_names()
        for name in ("deletion_ledger_purged", "stale_copies_removed",
                     "encryption_key_shredded", "compact"):
            self.assertIn(name, documented,
                          f"{name}: гейт перестал видеть это имя — он его ТЕРПИТ, "
                          f"а не проверяет")
        returns = _handler_return_keys()
        self.assertIn("purge_all_data", returns,
                      "гейт не разрешил dispatch -> HistoryService.handle_purge_all_data; "
                      "проверка полей ответа деградирует в ничто")
        for name in ("deletion_ledger_purged", "stale_copies_removed",
                     "encryption_key_shredded"):
            self.assertIn(name, returns["purge_all_data"],
                          f"{name} документирован, но его нет в реальном ответе хендлера")
        self.assertIn("compact", _purge_error_codes(),
                      "код ошибки `compact` документирован, но purge его не эмитит")

    def test_counts_are_not_pinned_but_sane(self) -> None:
        # Гарды от протухания методики, НЕ от дрейфа тоталов: тоталы не вшиваем.
        self.assertGreater(len(dispatch_keys()), 300)


def _handler_alias(method: str) -> str:
    return method


@functools.lru_cache(maxsize=1)
def _purge_error_codes() -> set[str]:
    """Коды, которые purge РЕАЛЬНО кладёт в `errors` — по транзитивному замыканию.

    Замыкание нужно, потому что коды эмитятся не только в теле хендлера:
    `history_ciphertext` живёт в модульном помощнике `_purge_wipe_managed_journals`
    (N1′), а не в `handle_purge_all_data`. Сканирование только тела хендлера дало
    бы ложное «код не эмитится» — ровно та ложная тревога, из-за которой потом
    добавили бы имя в исключение.
    """
    path = BACKEND / "history_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    purges = _functions_named(tree, "purge_all_data")
    if not purges:
        return set()
    modules = {n.name: n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes: dict[str, dict[str, ast.AST]] = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        classes[cls.name] = {m.name: m for m in cls.body
                             if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    owner = None
    for fn in purges:
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in classes:
                owner = node.id
    scope: dict[str, ast.AST] = dict(modules)
    if owner:
        scope.update(classes[owner])

    closure: dict[str, ast.AST] = {}
    stack = list(purges)
    while stack:
        fn = stack.pop()
        key = f"{owner}.{fn.name}" if owner and fn.name in classes.get(owner, {}) else fn.name
        if key in closure:
            continue
        closure[key] = fn
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            callee = node.func.id
            if callee in scope and callee not in closure:
                stack.append(scope[callee])

    codes: set[str] = set()
    for fn in closure.values():
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_flag_step_error" and len(node.args) > 1
                    and isinstance(node.args[1], ast.Constant)):
                codes.add(node.args[1].value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "secondary_errors" and node.args
                    and isinstance(node.args[0], ast.Constant)):
                codes.add(node.args[0].value)
    return codes


def _is_error_code(name: str, method: str, returns: dict[str, set[str]]) -> bool:
    if method not in ("purge_all_data", "?"):
        return False
    return name in _purge_error_codes()


if __name__ == "__main__":
    unittest.main()
