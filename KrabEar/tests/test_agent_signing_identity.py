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
    # Процесс агента «появляется» после open (как в реальности) либо задан заранее через SIGN_TEST_RUNNING.
    rows = [json.loads(row) for row in pathlib.Path(os.environ["SIGN_TEST_TRACE"]).read_text().splitlines()]
    opened = any(r[0] == "open" or (r[0] == "launchctl" and r[1][:1] == ["kickstart"]) for r in rows)
    # Старый агент (до open) — 424242; после open «поднимается» НОВЫЙ процесс с другим PID.
    if opened:
        print("515151")
    elif os.environ.get("SIGN_TEST_RUNNING"):
        print("424242")
    else:
        sys.exit(1)
elif tool == "launchctl":
    # По умолчанию юнит агента НЕ загружен (реальный launchctl в тестах недопустим: kickstart -k тронул бы
    # живой агент владельца). SIGN_TEST_UNIT_LOADED=1 моделирует загруженный launchd-юнит.
    if args[:1] == ["print"]:
        sys.exit(0 if os.environ.get("SIGN_TEST_UNIT_LOADED") else 1)
elif tool == "ps":
    # `ps -p PID -o stat=` — проверка «жив ли старый PID»: SIGTERM в фикстуре срабатывает, процесса уже нет.
    # `-o etime=` — возраст процесса (SIGN_TEST_ETIME), пусто = неизвестно.
    if "etime=" in args:
        if os.environ.get("SIGN_TEST_ETIME"):
            print(os.environ["SIGN_TEST_ETIME"])
        else:
            sys.exit(1)
    elif "stat=" not in args:
        print("/fixture/foreign/KrabEarAgent")
elif tool == "stat":
    path = pathlib.Path(args[-1])
    print(int(path.stat().st_mtime) if args[-2] == "%m" else path.stat().st_size)
elif tool == "date":
    # `date +%s` — настоящая метка времени (скрипты считают возраст процесса от неё); остальное — заглушка.
    if "+%s" in args:
        import time
        print(int(time.time()))
    else:
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
            "osascript", "df", "launchctl",
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
        # busy-гейт verify_binaries --fix (KE02B2-05) зовёт safe_backend_restart --check-only; его логика
        # покрыта собственными тестами, здесь backend «свободен».
        gate = scripts / "safe_backend_restart.command"
        gate.write_text("#!/bin/bash\nexit 0\n")
        gate.chmod(0o755)
        self.runtime = self.root / "native/runtime/KrabEarAgent"
        self.bundle = self.root / "Krab Ear.app"
        self.app_bin = self.bundle / "Contents/MacOS/KrabEarAgent"
        self.build = self.root / "native/KrabEarAgent/.build/release/KrabEarAgent"
        for path, value in (
            (self.runtime, "old-rtm"), (self.app_bin, "old-app"),
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
            # verify_binaries берёт pgrep из PGREP_BIN (подмена по тексту `/usr/bin/pgrep` ломает его
            # значение по умолчанию кавычками): заглушка передаётся явно.
            "PGREP_BIN": str(self.tools / "pgrep"),
            # то же для PS_BIN: подстановка пути в значение по умолчанию ломается кавычками
            "PS_BIN": str(self.tools / "ps"),
            "HOME": str(self.root / "fixture-home"),
            "SIGN_TEST_TRACE": str(self.trace),
            "SIGN_TEST_IDENTITIES": (
                f'  1) {IDENTITY} "Krab Ear Dev Local"\n'
                "     1 valid identities found"
            ),
        })

    def run_entry(self, entry: str, shell: str = "bash") -> subprocess.CompletedProcess:
        self.runtime.write_text("old-rtm")
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
        self.assertEqual(self.runtime.read_text(), "old-rtm")
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
        self.assertEqual(self.runtime.read_text(), "old-rtm")

    # ── ревью Opus #2304 (P2): под launchd KeepAlive старый бинарник поднимается ДО установки ──
    def test_launchd_unit_loaded_restarts_via_kickstart_not_open(self) -> None:
        for entry in ("update_agent.command", "verify_binaries.command"):
            with self.subTest(entry=entry):
                self.trace.write_text("")
                self.env["SIGN_TEST_UNIT_LOADED"] = "1"
                self.env["SIGN_TEST_ETIME"] = "00:00"  # процесс стартовал после установки (возраст 0 с)
                result = self.run_entry(entry)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                kinds = [(e[0], e[1][:1]) for e in self.events()]
                self.assertIn(("launchctl", ["kickstart"]), kinds)
                self.assertNotIn("open", [e[0] for e in self.events()], "под launchd .app не открываем — open лишь активирует старый")

    def test_launchd_old_process_started_before_install_is_not_success(self) -> None:
        for entry in ("update_agent.command", "verify_binaries.command"):
            with self.subTest(entry=entry):
                self.trace.write_text("")
                self.env["SIGN_TEST_UNIT_LOADED"] = "1"
                self.env["SIGN_TEST_ETIME"] = "05:00"  # процесс живёт 5 минут — это старый, поднятый до установки
                result = self.run_entry(entry)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Готово", result.stdout)
                self.assertNotIn("restarted", result.stdout)

    def test_unit_not_loaded_uses_open(self) -> None:
        self.env.pop("SIGN_TEST_UNIT_LOADED", None)
        result = self.run_entry("update_agent.command")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        names = [e[0] for e in self.events()]
        self.assertIn("open", names)
        self.assertFalse([e for e in self.events() if e[0] == "launchctl" and e[1][:1] == ["kickstart"]])

    def test_report_only_newer_build_never_executes_signing_hint(self) -> None:
        newer = time.time() + 10
        os.utime(self.build, (newer, newer))
        # Копии синхронизированы (одно содержимое): иначе отчёт справедливо покажет DRIFT .app ≠ runtime.
        self.runtime.write_text("synced")
        self.app_bin.write_text("synced")
        result = subprocess.run(
            ["bash", str(self.root / "scripts/verify_binaries.command")],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prohibited = {"security", "swift", "cp", "codesign", "pkill", "kill", "open"}
        # Сверка копий по содержимому БЕЗ подписи работает на временных копиях (verify_binaries.XXXXXX):
        # cp/codesign --remove-signature над ними — не подпись и не изменение доставленных файлов.

        def _tmp_copy_op(event) -> bool:
            return event[0] in ("cp", "codesign") and bool(event[1]) and "verify_binaries." in event[1][-1]

        offending = [event for event in self.events() if event[0] in prohibited and not _tmp_copy_op(event)]
        self.assertFalse(offending, offending)
        self.assertEqual(self.app_bin.read_text(), "synced")
        self.assertEqual(self.runtime.read_text(), "synced")


if __name__ == "__main__":
    unittest.main()
