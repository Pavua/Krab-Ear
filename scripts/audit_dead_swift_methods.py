#!/usr/bin/env python3
"""audit_dead_swift_methods.py — мёртвые методы Swift-агента.

ROOT CAUSE
----------
Python-сторона закрыта пятью гардами (orphan imports, decorative wiring, dead
extracted modules, dispatch targets, dead IPC handlers). Swift-половина не
покрыта ничем, а класс «хендлер написан, проводки нет» там живой: три @objc-метода
панели истории и меню пресетов полностью реализованы и никуда не подключены —
ровно та же беда, что `setupErrorBus`, который был мёртв в проде при 100% зелёных
тестах.

🔴 КРИТЕРИЙ — самое важное в этом гарде. Наивное «имя не встречается как `name(`»
даёт ~78 ложных срабатываний из 95 на живом дереве. Гард с таким шумом не доживает
до второго использования, поэтому реализованы правила-исключения:

  * `override` — компилятор Swift не даст его скомпилировать без совпадения с
    супертипом, значит вызывает фреймворк;
  * trailing-closure вызов `obj.method { … }` — БЕЗ скобок (иначе теряются 12
    живых методов вроде `setWedgeProbe`);
  * соответствие протоколу засчитывается ТОЛЬКО вместе с именем требования:
    «тип конформит *Delegate ⇒ всё внутри живое» ложно почти в половине случаев
    и скрыло бы реальную находку внутри AgentAppDelegate;
  * известные lifecycle-имена AppKit/Foundation — их зовёт рантайм;
  * bare-reference `views.forEach(obj.method)` — метод как значение;
  * короткие/частые имена (`record`, `clear`, `start`, …) уходят в отдельную
    категорию needs_review: счёт по имени там недостоверен в ОБЕ стороны —
    `AgentRecoveryLogger.record` реально мёртв, но выглядел живым из-за
    посторонних `record(...)` в тестовых spy-классах;
  * вызовы только из `Tests/` — это test_only, а не мёртвый код.

`@objc` без `#selector` намеренно НЕ исключается: динамического
`NSSelectorFromString`/`Selector("…")` в проекте нет, поэтому непроводной
@objc-хендлер — настоящая находка.

Swift AST в проекте недоступен, разбор построчно-регексовый — по образцу
scripts/audit_dead_ipc_handlers.py и scripts/audit_ipc_contract_drift.py.

Usage::

    python3 scripts/audit_dead_swift_methods.py                  # отчёт, exit 0
    python3 scripts/audit_dead_swift_methods.py --fail-on-found  # режим CI
    python3 scripts/audit_dead_swift_methods.py --json           # машинный вывод
    python3 scripts/audit_dead_swift_methods.py --selftest       # проверка классификатора

Exit 0 → находок нет (или режим report-only).
Exit 1 → `--fail-on-found` и есть находки вне allowlist.
Exit 2 → сломанное окружение (каталог исходников не найден).

Allowlist: ``scripts/audit_dead_swift_methods_allowlist.txt``; строки вида
``method:<Имя>``, ``#`` — комментарий.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SWIFT_SOURCES = REPO_ROOT / "native" / "KrabEarAgent" / "Sources"
SWIFT_TESTS = REPO_ROOT / "native" / "KrabEarAgent" / "Tests"
ALLOWLIST_FILE = REPO_ROOT / "scripts" / "audit_dead_swift_methods_allowlist.txt"

VERDICT_DEAD = "dead"
VERDICT_TEST_ONLY = "test_only"
VERDICT_NEEDS_REVIEW = "needs_review"

_DECL_RE = re.compile(
    r"^\s*(?P<mods>(?:@\w+(?:\([^)]*\))?\s+|private\s+|fileprivate\s+|internal\s+|public\s+|open\s+"
    r"|static\s+|class\s+|final\s+|override\s+|mutating\s+|nonisolated\s+|convenience\s+)*)"
    r"func\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_TYPE_RE = re.compile(
    r"^\s*(?:public\s+|internal\s+|private\s+|fileprivate\s+|final\s+|@\w+\s+)*"
    r"(?:class|struct|enum|extension|actor)\s+(?P<type>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*:\s*(?P<conf>[^{]+))?"
)

# Имена, которые вызывает рантайм AppKit/Foundation, а не Swift-код.
LIFECYCLE_NAMES = frozenset({
    "applicationDidFinishLaunching", "applicationWillTerminate", "applicationShouldTerminate",
    "applicationShouldHandleReopen", "applicationShouldTerminateAfterLastWindowClosed",
    "viewDidLoad", "viewWillAppear", "viewDidAppear", "viewWillDisappear", "loadView",
    "mouseEntered", "mouseExited", "mouseDown", "mouseUp", "mouseDragged", "mouseMoved",
    "draw", "drawFocusRingMask", "layout", "updateLayer", "deinit",
    "windowDidResize", "windowWillClose", "windowDidBecomeKey", "windowDidResignKey",
    "draggingEntered", "draggingExited", "draggingUpdated", "performDragOperation",
    "prepareForDragOperation", "concludeDragOperation", "validateMenuItem",
    "menuWillOpen", "menuDidClose", "numberOfRows", "controlTextDidChange",
    "observeValue", "encode", "copy", "isEqual", "hash",
})

# Префиксы имён требований делегатных протоколов.
PROTOCOL_NAME_PREFIXES = (
    "tableView", "outlineView", "collectionView", "windowDid", "windowWill", "windowShould",
    "audioPlayer", "urlSession", "webView", "searchField", "controlText", "textView",
    "textField", "scrollView", "splitView", "menu", "application", "userNotificationCenter",
    "captureOutput", "stream",
)
_CONFORMANCE_RE = re.compile(r"(Delegate|DataSource|Validation|Observer)\b")

# Короткие/частые имена: счёт по имени недостоверен в обе стороны.
COMMON_NAMES = frozenset({
    "record", "clear", "start", "stop", "close", "run", "handle", "update", "reset",
    "send", "add", "remove", "get", "set", "load", "save", "show", "hide", "open",
    "cancel", "flush", "apply", "refresh", "toggle", "log", "emit", "push", "pop",
})


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    symbol: str
    verdict: str
    reason: str


@dataclass(frozen=True)
class _Decl:
    file: str
    line: int
    name: str
    mods: str
    conformances: str


def _strip_comment(line: str) -> str:
    idx = line.find("//")
    return line[:idx] if idx >= 0 else line


def _is_test_path(path: str) -> bool:
    return "/Tests/" in f"/{path}" or path.startswith("Tests/")


def _collect_decls(sources: dict[str, str]) -> list[_Decl]:
    decls: list[_Decl] = []
    for path, text in sources.items():
        if _is_test_path(path):
            continue
        current_conf = ""
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = _strip_comment(raw)
            type_m = _TYPE_RE.match(line)
            if type_m:
                current_conf = (type_m.group("conf") or "").strip()
            decl_m = _DECL_RE.match(line)
            if decl_m:
                decls.append(_Decl(
                    file=path, line=lineno, name=decl_m.group("name"),
                    mods=decl_m.group("mods") or "", conformances=current_conf,
                ))
    return decls


def _mentions(sources: dict[str, str], name: str, *, tests: bool) -> bool:
    """Есть ли упоминание имени как вызова/ссылки (не как объявления)."""
    call = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}\s*[({{]")
    selector = re.compile(rf"#selector\([^)]*(?<![A-Za-z0-9_]){re.escape(name)}\b")
    bare = re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(name)}(?![A-Za-z0-9_])")
    decl_here = re.compile(rf"func\s+{re.escape(name)}\b")
    for path, text in sources.items():
        if _is_test_path(path) != tests:
            continue
        for raw in text.splitlines():
            line = _strip_comment(raw)
            if decl_here.search(line):
                continue
            if call.search(line) or selector.search(line):
                return True
            # bare-reference: метод передан как значение (forEach(obj.method))
            if bare.search(line) or re.search(
                rf"\.{re.escape(name)}(?![A-Za-z0-9_(\{{])", line
            ):
                return True
    return False


def _protocol_requirement(decl: _Decl) -> bool:
    if not _CONFORMANCE_RE.search(decl.conformances):
        return False
    return decl.name.startswith(PROTOCOL_NAME_PREFIXES)


def find_dead_methods(sources: dict[str, str]) -> list[Finding]:
    """Чистая функция от исходников — её же использует ``--selftest``."""
    findings: list[Finding] = []
    for decl in _collect_decls(sources):
        if "override" in decl.mods:
            continue
        if decl.name in LIFECYCLE_NAMES or _protocol_requirement(decl):
            continue
        if _mentions(sources, decl.name, tests=False):
            continue
        if decl.name in COMMON_NAMES:
            verdict, reason = VERDICT_NEEDS_REVIEW, (
                "частое имя: счёт по имени недостоверен, нужна ручная проверка получателя"
            )
        elif _mentions(sources, decl.name, tests=True):
            verdict, reason = VERDICT_TEST_ONLY, "вызывается только из Tests/ — жив для тестов, мёртв в проде"
        else:
            verdict, reason = VERDICT_DEAD, "ни одного вызова, селектора или ссылки"
        findings.append(Finding(decl.file, decl.line, decl.name, verdict, reason))
    return findings


def load_allowlist(path: Path = ALLOWLIST_FILE) -> set[str]:
    if not path.exists():
        return set()
    entries: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("method:"):
            entries.add(line.split(":", 1)[1].strip())
    return entries


def _read_sources() -> dict[str, str]:
    sources: dict[str, str] = {}
    for root in (SWIFT_SOURCES, SWIFT_TESTS):
        if not root.exists():
            continue
        for swift in sorted(root.rglob("*.swift")):
            sources[str(swift.relative_to(REPO_ROOT))] = swift.read_text(
                encoding="utf-8", errors="replace"
            )
    return sources


def format_report(findings: list[Finding]) -> str:
    lines = ["=" * 78, "DEAD SWIFT METHODS AUDIT (W6)", "=" * 78]
    dead = [f for f in findings if f.verdict == VERDICT_DEAD]
    if not findings:
        lines.append("CLEAN — мёртвых Swift-методов не найдено.")
        return "\n".join(lines)
    for verdict in (VERDICT_DEAD, VERDICT_TEST_ONLY, VERDICT_NEEDS_REVIEW):
        group = [f for f in findings if f.verdict == verdict]
        if not group:
            continue
        lines.append(f"--- {verdict}: {len(group)} ---")
        for f in group:
            lines.append(f"  {f.file}:{f.line}  {f.symbol}")
            lines.append(f"      reason : {f.reason}")
    lines.append(f"ИТОГО мёртвых: {len(dead)}")
    return "\n".join(lines)


def format_json(findings: list[Finding]) -> str:
    return json.dumps(
        {"findings": [f.__dict__ for f in findings], "count": len(findings)},
        ensure_ascii=False, indent=2,
    )


def selftest() -> int:
    """Известно-плохие и известно-хорошие образцы через ТОТ ЖЕ детектор."""
    bad = [
        ("private без вызовов",
         {"Sources/A.swift": "class A {\n    private func zzUnusedHelper() {}\n}\n"}, "zzUnusedHelper"),
        ("@objc без #selector",
         {"Sources/B.swift": "class B {\n    @objc func zzOnUnwiredTap() {}\n}\n"}, "zzOnUnwiredTap"),
    ]
    good = [
        ("override", {"Sources/C.swift": "class C: NSView {\n    override func zzDrawIt() {}\n}\n"}, "zzDrawIt"),
        ("trailing closure", {
            "Sources/D.swift": "class D {\n    func zzWithClosure(_ p: () -> Void) {}\n}\n",
            "Sources/E.swift": "func w(d: D) {\n    d.zzWithClosure { }\n}\n",
        }, "zzWithClosure"),
        ("lifecycle", {"Sources/F.swift": "class F {\n    func viewDidLoad() {}\n}\n"}, "viewDidLoad"),
    ]
    failures: list[str] = []
    for label, src, name in bad:
        verdicts = {f.symbol: f.verdict for f in find_dead_methods(src)}
        if verdicts.get(name) != VERDICT_DEAD:
            failures.append(f"НЕ отловлен известно-плохой случай: {label}")
    for label, src, name in good:
        verdicts = {f.symbol: f.verdict for f in find_dead_methods(src)}
        if name in verdicts:
            failures.append(f"ложная тревога на известно-хорошем: {label} -> {verdicts[name]}")
    if failures:
        for line in failures:
            print("SELFTEST FAIL:", line)
        return 1
    print(f"SELFTEST OK: {len(bad)} известно-плохих отловлены, {len(good)} известно-хороших чисты.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Аудит мёртвых Swift-методов")
    parser.add_argument("--fail-on-found", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if not SWIFT_SOURCES.exists():
        print(f"ERROR: каталог исходников не найден: {SWIFT_SOURCES}", file=sys.stderr)
        return 2

    allowed = load_allowlist()
    findings = [f for f in find_dead_methods(_read_sources()) if f.symbol not in allowed]
    print(format_json(findings) if args.json else format_report(findings))

    if args.fail_on_found and any(f.verdict == VERDICT_DEAD for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
