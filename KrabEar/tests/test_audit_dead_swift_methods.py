"""W6 — детектор мёртвых Swift-методов: ловушки важнее самого поиска.

Наивный критерий «имя не встречается как `name(`» даёт ~78 ложных срабатываний
из 95 на живом дереве агента. Тесты ниже фиксируют каждое правило-исключение,
потому что гард с таким шумом не доживает до второго использования.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_dead_swift_methods.py"

# 🔴 НЕ skip: пропущенный тест — зелёный, и RED-фаза стала бы невидимой
# (pytest отдаёт exit 5 «нет тестов», что читается как успех в цепочке гейтов).
assert _SCRIPT.exists(), f"гард отсутствует: {_SCRIPT}"

_spec = importlib.util.spec_from_file_location("audit_dead_swift_methods", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["audit_dead_swift_methods"] = _mod
_spec.loader.exec_module(_mod)

find_dead_methods = _mod.find_dead_methods
DEAD = _mod.VERDICT_DEAD
TEST_ONLY = _mod.VERDICT_TEST_ONLY
NEEDS_REVIEW = _mod.VERDICT_NEEDS_REVIEW


def _verdicts(sources: dict[str, str]) -> dict[str, str]:
    """Имя метода → вердикт. Живые методы в результат не попадают."""
    return {f.symbol: f.verdict for f in find_dead_methods(sources)}


class DeadDetectionTest(unittest.TestCase):
    def test_private_method_without_callers_is_dead(self):
        """private + ноль вызовов = гарантированно мёртв (извне файла не позвать)."""
        src = {"Sources/A.swift": "class A {\n    private func hasSavedPosition() -> Bool { true }\n}\n"}
        self.assertEqual(_verdicts(src).get("hasSavedPosition"), DEAD)

    def test_objc_without_selector_is_dead_not_excluded(self):
        """🔴 @objc без #selector — кандидат, а НЕ исключение.

        Динамического NSSelectorFromString/Selector("…") в этом проекте нет,
        поэтому непроводной @objc-хендлер — реальная находка (3 из 3 в разведке).
        """
        src = {"Sources/B.swift": "class B {\n    @objc func onSummarizeItem() {}\n}\n"}
        self.assertEqual(_verdicts(src).get("onSummarizeItem"), DEAD)


class FalsePositiveRulesTest(unittest.TestCase):
    def test_override_is_never_dead(self):
        """Компилятор не даст override без совпадения с супертипом — зовёт фреймворк."""
        src = {"Sources/C.swift": "class C: NSView {\n    override func viewDidLoad() {}\n}\n"}
        self.assertNotIn("viewDidLoad", _verdicts(src))

    def test_trailing_closure_call_counts_as_alive(self):
        """🔴 `monitor.setWedgeProbe { … }` — вызов БЕЗ скобок; 12 живых методов иначе теряются."""
        src = {
            "Sources/D.swift": "class D {\n    func setWedgeProbe(_ p: () -> Void) {}\n}\n",
            "Sources/E.swift": "func wire(d: D) {\n    d.setWedgeProbe { print(\"x\") }\n}\n",
        }
        self.assertNotIn("setWedgeProbe", _verdicts(src))

    def test_delegate_method_needs_both_conformance_and_name(self):
        """Конформность + известное имя требования протокола = живой (зовёт AppKit)."""
        src = {
            "Sources/F.swift": (
                "class F: NSObject, NSTableViewDelegate {\n"
                "    func tableViewSelectionDidChange(_ n: Notification) {}\n"
                "}\n"
            )
        }
        self.assertNotIn("tableViewSelectionDidChange", _verdicts(src))

    def test_conformance_alone_does_not_excuse_unrelated_method(self):
        """🔴 Наивное «тип конформит Delegate ⇒ всё живое» скрыло бы реальную находку.

        В разведке так пряталось 18 из 40 — включая `isBackendNotRecordingError`
        внутри AgentAppDelegate.
        """
        src = {
            "Sources/G.swift": (
                "class G: NSObject, NSApplicationDelegate {\n"
                "    func isBackendNotRecordingError(_ e: Error) -> Bool { false }\n"
                "}\n"
            )
        }
        self.assertEqual(_verdicts(src).get("isBackendNotRecordingError"), DEAD)

    def test_bare_reference_counts_as_alive(self):
        """Передача метода как значения: `[...].forEach(relaxHorizontalCompression)`."""
        src = {
            "Sources/H.swift": "class H {\n    func relaxHorizontalCompression(_ v: NSView) {}\n}\n",
            "Sources/I.swift": ("func apply(h: H, views: [NSView]) {\n"
                                "    views.forEach(h.relaxHorizontalCompression)\n}\n"),
        }
        self.assertNotIn("relaxHorizontalCompression", _verdicts(src))

    def test_lifecycle_name_is_alive(self):
        """Framework-callback по имени — вызывает рантайм, не Swift-код."""
        src = {"Sources/J.swift": "class J {\n    func applicationDidFinishLaunching(_ n: Notification) {}\n}\n"}
        self.assertNotIn("applicationDidFinishLaunching", _verdicts(src))


class CategoryTest(unittest.TestCase):
    def test_called_only_from_tests_is_test_only_not_dead(self):
        """Живой для тестов, мёртвый в проде — отдельная метка, не «мёртвый код»."""
        src = {
            "Sources/K.swift": "class K {\n    func _testSetRecording(_ v: Bool) {}\n}\n",
            "Tests/KTests.swift": "func t(k: K) {\n    k._testSetRecording(true)\n}\n",
        }
        self.assertEqual(_verdicts(src).get("_testSetRecording"), TEST_ONLY)

    def test_common_name_collision_goes_to_needs_review(self):
        """🔴 Короткие имена: счёт по имени недостоверен в ОБЕ стороны.

        `AgentRecoveryLogger.record` реально мёртв, но выглядел живым из-за
        посторонних `record(...)` в двух тестовых spy-классах.
        """
        src = {
            "Sources/L.swift": "class AgentRecoveryLogger {\n    func record(stage: String) {}\n}\n",
            "Tests/MTests.swift": ("class Spy {\n    func record(_ s: String) {}\n}\n"
                                   "func t(s: Spy) { s.record(\"x\") }\n"),
        }
        self.assertEqual(_verdicts(src).get("record"), NEEDS_REVIEW)


class SelftestContractTest(unittest.TestCase):
    def test_selftest_passes_on_its_own_samples(self):
        """Гард, тихо переставший классифицировать, отчитывался бы CLEAN вечно."""
        self.assertEqual(_mod.selftest(), 0)


if __name__ == "__main__":
    unittest.main()
