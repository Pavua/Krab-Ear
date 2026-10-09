"""Регрессии доставки агента: стабильная TCC identity без ad-hoc fallback.

Запускаем настоящие make targets и shell scripts в отдельном каталоге.
Только внешние macOS-инструменты заменены: Keychain, подпись, Swift и lifecycle.
Это проверяет порядок/ошибки доставки, но не живое разрешение macOS TCC.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
IDENTITY = "A76605821367AD0ACD83289C8C22E7D853AA28C7"
OTHER_IDENTITY = "B" * 40
SCRIPTS = (
    "build_and_deploy.command", "update_agent.command",
    "verify_binaries.command", "check_two_binary_drift.sh",
)
ENTRYPOINTS = ("sign", "app", *SCRIPTS)

# Инструменты не запускают процессы macOS и не обращаются к настоящему Keychain.
FAKE_TOOL = r'''
import json, os, pathlib, shutil, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["SIGN_TEST_TRACE"], "a") as out:
    out.write(json.dumps([tool, args]) + "\n")
if tool == "security":
    print(os.environ["SIGN_TEST_IDENTITIES"])
    sys.exit(int(os.environ.get("SIGN_TEST_SECURITY_EXIT", "0")))
if tool == "codesign":
    target = args[-1]
    failing = os.environ.get("SIGN_TEST_FAIL_SIGN", "")
    is_verify = "--verify" in args or "-v" in args
    if (failing == "runtime" and "/runtime/" in target
        or failing == "bundle" and target.endswith(".app")) and not is_verify:
        print("fixture signing failed", file=sys.stderr)
        sys.exit(23)
    if os.environ.get("SIGN_TEST_FAIL_VERIFY") and is_verify:
        sys.exit(24)
elif tool == "cp":
    src, dst = args[-2:]
    if pathlib.Path(src).resolve() != pathlib.Path(dst).resolve():
        shutil.copy2(src, dst)
elif tool == "dwarfdump":
    path = pathlib.Path(args[-1])
    uuid = path.read_text().strip() if path.is_file() else "build"
    print("UUID: " + uuid + " (arm64) " + str(path))
elif tool == "dsymutil":
    pathlib.Path(args[-1]).mkdir(parents=True, exist_ok=True)
elif tool == "pgrep":
    if os.environ.get("SIGN_TEST_RUNNING"):
        print("424242")
    else:
        sys.exit(1)
elif tool == "ps":
    print("/fixture/foreign/KrabEarAgent")
elif tool == "stat":
    path = pathlib.Path(args[-1])
    print(int(path.stat().st_mtime) if args[-2] == "%m" else path.stat().st_size)
elif tool == "date":
    print("00:00:00")
elif tool == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted")
    print("fixture 9999999 1 9999998 0% /")
'''


class TestAgentSigningIdentity(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ear signing ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.trace = self.root / "trace.jsonl"
        self.trace.touch()
        for tool in (
            "security", "codesign", "cp", "swift", "dwarfdump", "dsymutil",
            "pgrep", "pkill", "kill", "ps", "open", "sleep", "stat", "date",
            "osascript", "df",
        ):
            executable = self.tools / tool
            executable.write_text(f"#!{Path(sys.executable).resolve()}\n" + FAKE_TOOL)
            executable.chmod(0o755)
        shutil.copy2(ROOT / "Makefile", self.root / "Makefile")
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in (*SCRIPTS, "agent_signing_identity.sh"):
            source = ROOT / "scripts" / name
            if source.exists():
                body = source.read_text()
                # Перехватываем также абсолютные tool paths старого verify script.
                # Его логика/ветвления остаются настоящими; lifecycle — всегда fake.
                for tool in (
                    "pgrep", "ps", "kill", "cp", "codesign", "open", "sleep",
                    "stat", "date", "df",
                ):
                    for prefix in ("/usr/bin/", "/bin/"):
                        body = body.replace(prefix + tool, shlex.quote(str(self.tools / tool)))
                (scripts / name).write_text(body)
        self.runtime = self.root / "native/runtime/KrabEarAgent"
        self.bundle = self.root / "Krab Ear.app"
        self.app_bin = self.bundle / "Contents/MacOS/KrabEarAgent"
        self.build = self.root / "native/KrabEarAgent/.build/release/KrabEarAgent"
        for path, value in (
            (self.runtime, "old-runtime"), (self.app_bin, "old-app"),
            (self.build, "build"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
            path.chmod(0o755)
        venv_python = self.root / ".venv_krab_ear/bin/python"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to(sys.executable)
        self.env = dict(os.environ)
        self.env.update({
            "PATH": str(self.tools) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.root / "fixture-home"),
            "SIGN_TEST_TRACE": str(self.trace),
            "SIGN_TEST_IDENTITIES": (
                f'  1) {IDENTITY} "Krab Ear Dev Local"\n'
                "     1 valid identities found"
            ),
        })

    def run_entry(self, entry: str, shell: str = "bash") -> subprocess.CompletedProcess:
        self.runtime.write_text("old-runtime")
        self.app_bin.write_text("old-app")
        if entry in ("sign", "app"):
            command = [shutil.which("make"), entry]
        else:
            # Документированный bash entrypoint; native zsh проверяется отдельно.
            if shutil.which(shell) is None:
                self.skipTest(f"shell {shell} unavailable")
            args = ["--no-sentry"] if entry.startswith("build_") else []
            if entry in ("verify_binaries.command", "check_two_binary_drift.sh"):
                args = ["--fix"]
            self.env["SIGN_TEST_RUNNING"] = "1" if entry.startswith("verify_") else ""
            command = [shell, str(self.root / "scripts" / entry), *args]
        return subprocess.run(
            command, cwd=self.root, env=self.env, capture_output=True,
            text=True, timeout=20,
        )

    def events(self) -> list:
        return [json.loads(row) for row in self.trace.read_text().splitlines()]

    def assert_preflight_refused(self, entry: str) -> None:
        result = self.run_entry(entry)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runtime.read_text(), "old-runtime")
        self.assertEqual(self.app_bin.read_text(), "old-app")
        prohibited = {"swift", "cp", "codesign", "pkill", "kill", "open"}
        self.assertFalse([event for event in self.events() if event[0] in prohibited])

    def test_missing_identity_refuses_every_install_path_before_mutation(self) -> None:
        for entry in ENTRYPOINTS:
            with self.subTest(entry=entry):
                self.env["SIGN_TEST_IDENTITIES"] = "0 valid identities found"
                self.assert_preflight_refused(entry)

    def test_ambiguous_identity_refuses_before_mutation(self) -> None:
        self.env["SIGN_TEST_IDENTITIES"] += (
            f'\n  2) {OTHER_IDENTITY} "Krab Ear Dev Local"'
        )
        self.assert_preflight_refused("sign")

    def test_revoked_identity_refuses_before_mutation(self) -> None:
        self.env["SIGN_TEST_IDENTITIES"] = (
            f'  1) {IDENTITY} "Krab Ear Dev Local" (CSSMERR_TP_CERT_REVOKED)\n'
            "0 valid identities found"
        )
        self.assert_preflight_refused("sign")

    def test_security_read_error_refuses_even_with_matching_stdout(self) -> None:
        self.env["SIGN_TEST_SECURITY_EXIT"] = "44"
        self.assert_preflight_refused("sign")

    def test_name_substring_does_not_select_another_identity(self) -> None:
        self.env["SIGN_TEST_IDENTITIES"] = (
            f'  1) {OTHER_IDENTITY} "Krab Ear Dev Local Backup"\n'
            "1 valid identities found"
        )
        self.assert_preflight_refused("sign")

    def test_make_targets_use_one_pinned_identity_for_runtime_and_bundle(self) -> None:
        for entry in ("sign", "app"):
            with self.subTest(entry=entry):
                self.trace.write_text("")
                result = self.run_entry(entry)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                signs = [args for tool, args in self.events() if tool == "codesign"
                         and ("--sign" in args or "-s" in args)]
                self.assertEqual(len(signs), 2, signs)
                for args in signs:
                    flag = "--sign" if "--sign" in args else "-s"
                    self.assertEqual(args[args.index(flag) + 1], IDENTITY)
                self.assertEqual(signs[0][-1], "native/runtime/KrabEarAgent")
                self.assertEqual(signs[1][-1], "Krab Ear.app")

    def test_sign_failure_propagates_without_relaunch(self) -> None:
        for entry in ENTRYPOINTS:
            with self.subTest(entry=entry):
                self.trace.write_text("")
                self.env["SIGN_TEST_FAIL_SIGN"] = "runtime"
                result = self.run_entry(entry)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse([event for event in self.events() if event[0] == "open"])
                signs = [args for tool, args in self.events() if tool == "codesign"
                         and ("--sign" in args or "-s" in args)]
                self.assertTrue(signs, result.stdout + result.stderr)
                self.assertFalse(any("-" in args for args in signs), signs)

    def test_bundle_sign_failure_propagates_without_relaunch(self) -> None:
        for entry in ("sign", "app", "update_agent.command", "build_and_deploy.command",
                      "verify_binaries.command"):
            with self.subTest(entry=entry):
                self.trace.write_text("")
                self.env["SIGN_TEST_FAIL_SIGN"] = "bundle"
                result = self.run_entry(entry)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse([event for event in self.events() if event[0] == "open"])

    def test_scripts_pin_same_certificate_and_verify_before_success(self) -> None:
        for entry in SCRIPTS:
            with self.subTest(entry=entry):
                self.trace.write_text("")
                result = self.run_entry(entry)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                events = self.events()
                self.assertEqual(sum(tool == "security" for tool, _ in events), 1)
                signs = [args for tool, args in events if tool == "codesign"
                         and "--sign" in args]
                expected = 1 if entry.endswith(".sh") else 2
                self.assertEqual(len(signs), expected, signs)
                for args in signs:
                    self.assertEqual(args[args.index("--sign") + 1], IDENTITY)
                    self.assertEqual(args[args.index("--identifier") + 1],
                                     "com.antigravity.krab-ear")
                verifies = [args for tool, args in events if tool == "codesign"
                            and "--verify" in args]
                self.assertEqual(len(verifies), expected)

    def test_failed_signature_verification_prevents_relaunch(self) -> None:
        for entry in ENTRYPOINTS:
            with self.subTest(entry=entry):
                self.trace.write_text("")
                self.env["SIGN_TEST_FAIL_VERIFY"] = "1"
                result = self.run_entry(entry)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue([args for tool, args in self.events()
                                 if tool == "codesign" and "--verify" in args])
                self.assertFalse([event for event in self.events() if event[0] == "open"])

    def test_native_zsh_delivery_paths_sign_and_verify(self) -> None:
        for entry in ("update_agent.command", "verify_binaries.command",
                      "build_and_deploy.command"):
            with self.subTest(entry=entry):
                self.trace.write_text("")
                result = self.run_entry(entry, shell="zsh")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                signs = [args for tool, args in self.events() if tool == "codesign"
                         and "--sign" in args]
                self.assertEqual(len(signs), 2, signs)

    def test_make_dry_run_does_not_execute_signing_recipe(self) -> None:
        result = subprocess.run(
            [shutil.which("make"), "-n", "app"], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.app_bin.read_text(), "old-app")
        self.assertEqual(self.runtime.read_text(), "old-runtime")

    def test_report_only_newer_build_never_executes_signing_hint(self) -> None:
        newer = time.time() + 10
        os.utime(self.build, (newer, newer))
        result = subprocess.run(
            ["bash", str(self.root / "scripts/verify_binaries.command")],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prohibited = {"security", "swift", "cp", "codesign", "pkill", "kill", "open"}
        self.assertFalse([event for event in self.events() if event[0] in prohibited])
        self.assertEqual(self.app_bin.read_text(), "old-app")
        self.assertEqual(self.runtime.read_text(), "old-runtime")


if __name__ == "__main__":
    unittest.main()
