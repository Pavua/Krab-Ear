"""Контракт ops-скриптов (ревью ChatGPT G2-Krab-Ear-02b2, 27.11.2026).

Скрипты запускаются по-настоящему, но против ЗАГЛУШЕК в ``PATH`` (pkill, pgrep,
tccutil, open, sleep, file, otool, swift, codesign) во временном дереве каталогов:
реальные процессы агента, ``open``, TCC и backend не затрагиваются.

Закрыто (находки сверены вручную с кодом базы 94f68198):
- KE02B2-09: ``safe_backend_restart --wait`` принимает только целые секунды;
- KE02B2-16/06/07/05: ``verify_binaries`` — неизвестные аргументы это ошибка,
  итог «в sync» не при отсутствии процесса/бинаря, ``pgrep -x`` вместо ``-f``,
  ``--fix`` не останавливает агент под записью (``--force`` — осознанный обход);
- KE02B2-02/11/13: ``update_agent`` — атомарная поставка, symlink-destination
  отклоняется, отсутствие процесса после ``open`` — ошибка (код 1);
- KE02B2-10: ``repair_permissions`` — busy-гейт, SIGTERM вместо SIGKILL, провал
  сброса Accessibility/Microphone не глотается;
- KE02B2-03/04/08: ``verify_bundled_runtime_selfcontained`` — вложенные ``.env``/
  логи, ошибки find/file/otool и symlink наружу больше не дают ложный PASS.

Не закрыто (отдельный дизайн): 01 (атомарная maintenance-аренда через backend),
12 (урезанный env не доказывает самодостаточность), 14 (выбор источника по mtime),
15 (ping ≠ готовность REST).
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
ZSH = shutil.which("zsh")
BASH = shutil.which("bash") or "/bin/bash"
needs_zsh = pytest.mark.skipif(ZSH is None, reason="нужен zsh (shebang скриптов #!/bin/zsh)")
needs_macos = pytest.mark.skipif(sys.platform != "darwin", reason="скрипт использует BSD-утилиты (stat -f, shasum)")


def _exe(path: Path, body: str, shebang: str = "#!/bin/sh\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shebang + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _copy(tmp_path: Path, *names: str) -> Path:
    """Копия реальных скриптов в tmp_path/scripts; корень проекта = tmp_path."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    for name in names:
        shutil.copy2(SCRIPTS / name, scripts / name)
    return tmp_path


def _env(tmp_path: Path, **extra: str) -> dict:
    stub = tmp_path / "stubbin"
    stub.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    if not (stub / "launchctl").exists():
        # Реальный launchctl в тестах недопустим (kickstart -k тронул бы живой агент владельца):
        # по умолчанию юнит НЕ загружен.
        _exe(stub / "launchctl", 'echo "launchctl $*" >> "$STUB_LOG"\n[ "$1" = print ] && exit 1\nexit 0\n')
    return dict(
        os.environ,
        PATH=f"{stub}{os.pathsep}{os.environ['PATH']}",
        HOME=str(home),
        STUB_LOG=str(log),
        **extra,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _run(argv: list[str], env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)


def _gate(tmp_path: Path, rc: int) -> None:
    """Подставной safe_backend_restart: --check-only отвечает кодом rc."""
    _exe(tmp_path / "scripts" / "safe_backend_restart.command", f'echo "gate $*" >> "$STUB_LOG"\nexit {rc}\n')


# ═══ KE02B2-09: safe_backend_restart --wait ═════════════════════════════════


class TestSafeRestartWaitValidation:
    SCRIPT = SCRIPTS / "safe_backend_restart.command"

    @pytest.mark.parametrize(
        "args",
        [["--wait"], ["--wait", "abc"], ["--wait", "--with-rest"], ["--wait", "-5"], ["--wait", "1.5"], ["--wait", ""]],
    )
    def test_invalid_wait_is_usage_error_before_any_ipc(self, tmp_path, args):
        res = _run([BASH, str(self.SCRIPT), *args], _env(tmp_path), timeout=10)
        assert res.returncode == 2, res.stdout + res.stderr
        assert "--wait" in res.stderr

    def test_valid_wait_passes_parsing(self, tmp_path):
        # сокета нет → состояние UNKNOWN → отказ (rc 1), но НЕ usage-ошибка (rc 2)
        res = _run([BASH, str(self.SCRIPT), "--wait", "0", "--check-only"], _env(tmp_path), timeout=30)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "REFUSED" in res.stderr


# ═══ verify_binaries ═══════════════════════════════════════════════════════


def _vb_tree(tmp_path: Path, app: str | None, runtime: str | None, build: str | None = None) -> Path:
    root = _copy(tmp_path, "verify_binaries.command")
    (root / "scripts" / "agent_signing_identity.sh").write_text('resolve_agent_signing_identity() { echo TEST; }\n')
    for rel, content in (
        ("Krab Ear.app/Contents/MacOS/KrabEarAgent", app),
        ("native/runtime/KrabEarAgent", runtime),
        ("native/KrabEarAgent/.build/release/KrabEarAgent", build),
    ):
        if content is not None:
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
    return root


def _pgrep_stub(tmp_path: Path, pids: list[int]) -> Path:
    stub = tmp_path / "pgrep_stub"
    if pids:
        body = "".join(f"echo {p}\n" for p in pids)
        _exe(stub, 'echo "pgrep $*" >> "$STUB_LOG"\n' + body)
    else:
        _exe(stub, 'echo "pgrep $*" >> "$STUB_LOG"\nexit 1\n')
    return stub


@needs_zsh
@needs_macos
class TestVerifyBinaries:
    def _vb(self, root: Path, tmp_path: Path, pgrep: Path, *args: str) -> subprocess.CompletedProcess:
        env = _env(tmp_path, PGREP_BIN=str(pgrep))
        return _run([ZSH, str(root / "scripts" / "verify_binaries.command"), *args], env)

    def test_unknown_argument_is_an_error_not_report_only(self, tmp_path):
        root = _vb_tree(tmp_path, "AAAA", "AAAA")
        res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, []), "--fx")
        assert res.returncode == 2, res.stdout + res.stderr
        assert "--fx" in res.stderr

    def test_force_without_fix_is_a_usage_error(self, tmp_path):
        root = _vb_tree(tmp_path, "AAAA", "AAAA")
        res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, []), "--force")
        assert res.returncode == 2, res.stdout + res.stderr

    def test_no_process_is_not_reported_as_all_in_sync(self, tmp_path):
        root = _vb_tree(tmp_path, "AAAA", "AAAA")
        res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, []))
        assert res.returncode == 0, res.stdout + res.stderr
        assert "All binaries in sync" not in res.stdout
        assert "НЕ запущен" in res.stdout

    def test_missing_app_binary_is_drift(self, tmp_path):
        root = _vb_tree(tmp_path, None, "AAAA")
        res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, []))
        assert res.returncode == 1, res.stdout + res.stderr
        assert "DRIFT" in res.stdout

    def test_different_sized_deploy_copies_are_drift(self, tmp_path):
        root = _vb_tree(tmp_path, "AAAA", "AAAAAAAAAA")
        res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, []))
        assert res.returncode == 1, res.stdout + res.stderr
        assert "различаются" in res.stdout

    @pytest.mark.skipif(shutil.which("clang") is None, reason="нужен clang для реального Mach-O")
    def test_correctly_synced_signed_pair_is_not_drift(self, tmp_path):
        """Ревью Opus #2304 (P1): подпись бандла даёт +32 байта к размеру копии, но содержимое то же.
        Реальные подписанные Mach-O (ad-hoc, во временном дереве; живой агент/TCC не затрагиваются)."""
        root = _vb_tree(tmp_path, None, None)
        src = tmp_path / "t.c"
        src.write_text("int main(void){return 0;}\n")
        built = tmp_path / "built"
        subprocess.run(["clang", "-o", str(built), str(src)], check=True, capture_output=True, timeout=120)
        app_dir = root / "Krab Ear.app"
        app_bin = app_dir / "Contents" / "MacOS" / "KrabEarAgent"
        app_bin.parent.mkdir(parents=True, exist_ok=True)
        (app_dir / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleIdentifier</key>'
            "<string>com.antigravity.krab-ear-test</string><key>CFBundleExecutable</key>"
            "<string>KrabEarAgent</string></dict></plist>")
        runtime = root / "native" / "runtime" / "KrabEarAgent"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, app_bin)
        shutil.copy2(built, runtime)
        ident = ["--force", "--sign", "-", "--identifier", "com.antigravity.krab-ear-test"]
        subprocess.run(["/usr/bin/codesign", *ident, str(runtime)], check=True, capture_output=True, timeout=60)
        subprocess.run(["/usr/bin/codesign", *ident, str(app_dir)], check=True, capture_output=True, timeout=60)
        assert app_bin.stat().st_size != runtime.stat().st_size, "предусловие: подписанные копии различаются размером"
        res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, []))
        assert res.returncode == 0, res.stdout + res.stderr
        assert "различаются" not in res.stdout

    def test_pgrep_is_exact_name_not_substring(self, tmp_path):
        root = _vb_tree(tmp_path, "AAAA", "AAAA")
        self._vb(root, tmp_path, _pgrep_stub(tmp_path, []))
        pgrep_calls = [c for c in _calls(tmp_path) if c.startswith("pgrep")]
        assert pgrep_calls, "pgrep не вызывался"
        assert all("-x" in c and "-f" not in c.split() for c in pgrep_calls), pgrep_calls

    def test_fix_refuses_under_active_recording_and_leaves_process_alive(self, tmp_path):
        root = _vb_tree(tmp_path, None, None)
        _gate(tmp_path, rc=1)
        victim = subprocess.Popen(["sleep", "30"])
        try:
            res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, [victim.pid]), "--fix")
            assert res.returncode == 1, res.stdout + res.stderr
            assert "ничего не остановлено" in res.stderr
            assert victim.poll() is None, "процесс остановлен под активной записью"
            assert any(c.startswith("gate --check-only") for c in _calls(tmp_path))
        finally:
            victim.kill()
            victim.wait()

    def test_fix_force_skips_the_gate(self, tmp_path):
        root = _vb_tree(tmp_path, None, None)
        _gate(tmp_path, rc=1)
        victim = subprocess.Popen(["sleep", "30"])
        try:
            res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, [victim.pid]), "--fix", "--force")
            # источников бинарника нет → rc 2 уже ПОСЛЕ остановки; гейт не вызывался
            assert res.returncode == 2, res.stdout + res.stderr
            assert victim.wait(timeout=10) == -signal.SIGTERM
            assert not any(c.startswith("gate") for c in _calls(tmp_path))
        finally:
            if victim.poll() is None:
                victim.kill()
                victim.wait()

    def test_fix_does_not_guess_among_several_instances(self, tmp_path):
        root = _vb_tree(tmp_path, None, None)
        _gate(tmp_path, rc=0)
        a, b = subprocess.Popen(["sleep", "30"]), subprocess.Popen(["sleep", "30"])
        try:
            res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, [a.pid, b.pid]), "--fix")
            assert res.returncode == 1, res.stdout + res.stderr
            assert a.poll() is None and b.poll() is None, "угадали и остановили один из экземпляров"
        finally:
            for p in (a, b):
                p.kill()
                p.wait()

    def test_symlink_destination_is_refused_and_victim_untouched(self, tmp_path):
        root = _vb_tree(tmp_path, None, "RUNTIME-OLD-LONGER", build="NEW")
        victim = tmp_path / "victim.txt"
        victim.write_text("DO-NOT-TOUCH")
        app = root / "Krab Ear.app" / "Contents" / "MacOS" / "KrabEarAgent"
        app.parent.mkdir(parents=True, exist_ok=True)
        app.symlink_to(victim)
        os.utime(root / "native/KrabEarAgent/.build/release/KrabEarAgent", (time.time() + 100, time.time() + 100))
        res = self._vb(root, tmp_path, _pgrep_stub(tmp_path, []), "--fix", "--force")
        assert res.returncode == 2, res.stdout + res.stderr
        assert "символическая ссылка" in res.stderr
        assert victim.read_text() == "DO-NOT-TOUCH"


# ═══ update_agent ══════════════════════════════════════════════════════════


def _ua_tree(tmp_path: Path, agent_appears: bool, *, running_pid: int | None = None, kill_old: bool = False,
             always_old: bool = False) -> Path:
    """running_pid — «прежний агент» (реальный процесс теста); kill_old — pkill-заглушка его гасит;
    always_old — pgrep и ПОСЛЕ open продолжает отдавать прежний PID (старый не выдаёт себя за новый)."""
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "update_agent.command", root / "scripts")
    (root / "scripts" / "agent_signing_identity.sh").write_text('resolve_agent_signing_identity() { echo TEST; }\n')
    _exe(root / ".venv_krab_ear" / "bin" / "python", "exit 0\n")
    (root / "native" / "runtime").mkdir(parents=True)
    (root / "native" / "runtime" / "KrabEarAgent").write_text("OLD-RUNTIME")
    macos = root / "Krab Ear.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "KrabEarAgent").write_text("OLD-BUNDLE")
    (root / "native" / "KrabEarAgent").mkdir(parents=True)
    stub = tmp_path / "stubbin"
    build_out = f"{root}/native/KrabEarAgent/.build/release"
    _exe(
        stub / "swift",
        f'mkdir -p "{build_out}"; echo NEW-BUILD > "{build_out}/KrabEarAgent"; chmod +x "{build_out}/KrabEarAgent"; exit 0\n',
    )
    _exe(stub / "codesign", "exit 0\n")
    kill = f'kill {running_pid} 2>/dev/null\n' if (kill_old and running_pid) else ""
    _exe(stub / "pkill", f'echo "pkill $*" >> "$STUB_LOG"\n{kill}exit 0\n')
    _exe(stub / "sleep", "exit 0\n")
    _exe(stub / "open", 'echo "open $*" >> "$STUB_LOG"\nexit 0\n')
    old = f"echo {running_pid}; exit 0\n" if running_pid else "exit 1\n"
    if agent_appears and not always_old:
        # агент «появляется» только после open — новым PID, которого раньше не было
        pg = 'if grep -q "^open " "$STUB_LOG" 2>/dev/null; then echo 99999; exit 0; fi\n' + old
    else:
        pg = old
    _exe(stub / "pgrep", pg)
    return root


@needs_zsh
class TestUpdateAgentDelivery:
    def _run(self, root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
        return _run([ZSH, str(root / "scripts" / "update_agent.command"), *args], _env(tmp_path))

    @staticmethod
    def _ua_gate(root: Path, rc: int) -> None:
        _exe(root / "scripts" / "safe_backend_restart.command", f'echo "gate $*" >> "$STUB_LOG"\nexit {rc}\n')

    @staticmethod
    def _live_agent() -> subprocess.Popen:
        return subprocess.Popen(["sleep", "60"])

    # ── ревью Opus #2304 (P2): busy-гейт и «старый процесс не выдаёт себя за новый» ──
    def test_busy_backend_refuses_before_stopping_agent_or_replacing_binaries(self, tmp_path):
        victim = self._live_agent()
        try:
            root = _ua_tree(tmp_path, agent_appears=True, running_pid=victim.pid, kill_old=True)
            self._ua_gate(root, rc=1)
            res = self._run(root, tmp_path)
            assert res.returncode == 1, res.stdout + res.stderr
            assert victim.poll() is None, "агент остановлен под активной записью"
            assert not any(c.startswith("pkill") for c in _calls(tmp_path))
            assert (root / "native" / "runtime" / "KrabEarAgent").read_text() == "OLD-RUNTIME"
        finally:
            victim.kill()
            victim.wait()

    def test_force_bypasses_the_gate(self, tmp_path):
        victim = self._live_agent()
        try:
            root = _ua_tree(tmp_path, agent_appears=True, running_pid=victim.pid, kill_old=True)
            self._ua_gate(root, rc=1)
            res = self._run(root, tmp_path, "--force")
            assert res.returncode == 0, res.stdout + res.stderr
            assert not any(c.startswith("gate") for c in _calls(tmp_path))
        finally:
            victim.kill()
            victim.wait()

    def test_gate_is_not_called_when_no_agent_is_running(self, tmp_path):
        root = _ua_tree(tmp_path, agent_appears=True)
        self._ua_gate(root, rc=1)
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        assert not any(c.startswith("gate") for c in _calls(tmp_path))

    def test_unknown_argument_is_usage_error(self, tmp_path):
        root = _ua_tree(tmp_path, agent_appears=True)
        assert self._run(root, tmp_path, "--forse").returncode == 2

    def test_old_agent_that_ignores_sigterm_aborts_before_binaries_change(self, tmp_path):
        victim = self._live_agent()
        try:
            root = _ua_tree(tmp_path, agent_appears=True, running_pid=victim.pid, kill_old=False, always_old=True)
            self._ua_gate(root, rc=0)
            res = self._run(root, tmp_path)
            assert res.returncode == 1, res.stdout + res.stderr
            assert (root / "native" / "runtime" / "KrabEarAgent").read_text() == "OLD-RUNTIME"
            assert not any(c.startswith("open") for c in _calls(tmp_path))
        finally:
            victim.kill()
            victim.wait()

    def test_stale_old_pid_after_open_is_not_a_successful_start(self, tmp_path):
        victim = self._live_agent()
        try:
            root = _ua_tree(tmp_path, agent_appears=True, running_pid=victim.pid, kill_old=True, always_old=True)
            self._ua_gate(root, rc=0)
            res = self._run(root, tmp_path)
            assert res.returncode == 1, res.stdout + res.stderr
            assert "не появился" in res.stdout
        finally:
            victim.kill()
            victim.wait()

    # ── ревью Opus #2304 (P2): launchd поднял СТАРЫЙ бинарник до установки — это не успех ──
    def _launchd_tree(self, tmp_path: Path, *, kick_starts_new: bool) -> tuple[Path, subprocess.Popen]:
        victim = self._live_agent()
        root = _ua_tree(tmp_path, agent_appears=True, running_pid=victim.pid, kill_old=True)
        self._ua_gate(root, rc=0)
        stub = tmp_path / "stubbin"
        etimes = tmp_path / "etimes"
        etimes.mkdir()
        (etimes / "88888").write_text("05:00\n")   # старый, поднятый launchd до установки
        (etimes / "77777").write_text("00:00\n")   # новый, после kickstart
        _exe(stub / "ps", 'if [ "$3" = "-o" ] && [ "$4" = "etime=" ]; then cat "' + str(etimes)
             + '/$2" 2>/dev/null || exit 1; exit 0; fi\nexec /bin/ps "$@"\n')
        _exe(stub / "launchctl",
             'echo "launchctl $*" >> "$STUB_LOG"\n[ "$1" = print ] && exit 0\n'
             '[ "$1" = kickstart ] && touch "$STUB_LOG.kicked"\nexit 0\n')
        new = "77777" if kick_starts_new else "88888"
        _exe(stub / "pgrep",
             'if [ -f "$STUB_LOG.kicked" ]; then echo ' + new + '; exit 0; fi\n'
             'if [ -f "$STUB_LOG.killed" ]; then echo 88888; exit 0; fi\n'
             f'echo {victim.pid}; exit 0\n')
        _exe(stub / "pkill", 'echo "pkill $*" >> "$STUB_LOG"\ntouch "$STUB_LOG.killed"\n'
             f'kill {victim.pid} 2>/dev/null\nexit 0\n')
        return root, victim

    def test_launchd_unit_is_restarted_with_kickstart_not_open(self, tmp_path):
        root, victim = self._launchd_tree(tmp_path, kick_starts_new=True)
        try:
            env = _env(tmp_path, PS_BIN=str(tmp_path / "stubbin" / "ps"))
            res = _run([ZSH, str(root / "scripts" / "update_agent.command")], env)
            assert res.returncode == 0, res.stdout + res.stderr
            calls = _calls(tmp_path)
            assert any(c.startswith("launchctl kickstart -k") for c in calls), calls
            assert not any(c.startswith("open") for c in calls), calls
        finally:
            victim.kill()
            victim.wait()

    def test_launchd_old_binary_respawned_before_install_is_not_reported_as_updated(self, tmp_path):
        root, victim = self._launchd_tree(tmp_path, kick_starts_new=False)
        try:
            env = _env(tmp_path, PS_BIN=str(tmp_path / "stubbin" / "ps"))
            res = _run([ZSH, str(root / "scripts" / "update_agent.command")], env)
            assert res.returncode == 1, res.stdout + res.stderr
            assert "Готово" not in res.stdout
        finally:
            victim.kill()
            victim.wait()

    def test_launchd_respawn_with_new_pid_is_success(self, tmp_path):
        victim = self._live_agent()
        try:
            root = _ua_tree(tmp_path, agent_appears=True, running_pid=victim.pid, kill_old=True)
            self._ua_gate(root, rc=0)
            res = self._run(root, tmp_path)
            assert res.returncode == 0, res.stdout + res.stderr
        finally:
            victim.kill()
            victim.wait()

    def test_agent_that_never_appears_is_an_error_not_a_warning(self, tmp_path):
        root = _ua_tree(tmp_path, agent_appears=False)
        res = self._run(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "не появился" in res.stdout

    def test_success_leaves_no_temp_files_and_new_binaries_in_place(self, tmp_path):
        root = _ua_tree(tmp_path, agent_appears=True)
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        for d in (root / "native" / "runtime", root / "Krab Ear.app" / "Contents" / "MacOS"):
            assert [p.name for p in d.iterdir()] == ["KrabEarAgent"], list(d.iterdir())
            assert (d / "KrabEarAgent").read_text().strip() == "NEW-BUILD"
            assert os.access(d / "KrabEarAgent", os.X_OK)

    def test_symlink_destination_is_refused_and_victim_untouched(self, tmp_path):
        root = _ua_tree(tmp_path, agent_appears=True)
        victim = tmp_path / "victim.txt"
        victim.write_text("DO-NOT-TOUCH")
        app = root / "Krab Ear.app" / "Contents" / "MacOS" / "KrabEarAgent"
        app.unlink()
        app.symlink_to(victim)
        res = self._run(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "символическая ссылка" in res.stdout
        assert victim.read_text() == "DO-NOT-TOUCH"


# ═══ repair_permissions ════════════════════════════════════════════════════


def _rp_tree(tmp_path: Path, *, agent_dies: bool = True, tcc_fail: str = "", old_pid: int | None = None,
             respawn_pid: int | None = None, kill_old: bool = False) -> Path:
    """old_pid — работающий агент (реальный процесс теста); kill_old — pkill-заглушка его гасит;
    respawn_pid — после pkill pgrep отдаёт ДРУГОЙ PID (как launchd KeepAlive)."""
    root = _copy(tmp_path, "repair_permissions.command")
    (root / "Krab Ear.app").mkdir()
    stub = tmp_path / "stubbin"
    kill = f'kill {old_pid} 2>/dev/null\n' if (kill_old and old_pid) else ""
    _exe(stub / "pkill", f'echo "pkill $*" >> "$STUB_LOG"\ntouch "$STUB_LOG.killed"\n{kill}exit 0\n')
    if old_pid is None:
        _exe(stub / "pgrep", "exit 1\n")
    else:
        respawn = f'[ -f "$STUB_LOG.killed" ] && echo {respawn_pid} && exit 0\n' if respawn_pid else ""
        _exe(stub / "pgrep", f"{respawn}echo {old_pid}\nexit 0\n")
    _exe(stub / "sleep", "exit 0\n")
    _exe(stub / "open", 'echo "open $*" >> "$STUB_LOG"\nexit 0\n')
    # tcc_fail: подстрока вызова, на которой tccutil падает (например "Accessibility com.antigravity")
    _exe(
        stub / "tccutil",
        'echo "tccutil $*" >> "$STUB_LOG"\n'
        + (f'case "$*" in *"{tcc_fail}"*) exit 1;; esac\n' if tcc_fail else "")
        + "exit 0\n",
    )
    return root


@needs_zsh
class TestRepairPermissions:
    def _run(self, root: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
        return _run([ZSH, str(root / "scripts" / "repair_permissions.command"), *args], _env(tmp_path))

    def test_busy_backend_refuses_before_any_mutation(self, tmp_path):
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=1)
        res = self._run(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        calls = _calls(tmp_path)
        assert not any(c.startswith(("pkill", "tccutil", "open")) for c in calls), calls
        assert any(c.startswith("gate --check-only") for c in calls)

    def test_idle_backend_stops_with_sigterm_never_sigkill(self, tmp_path):
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=0)
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        pkills = [c for c in _calls(tmp_path) if c.startswith("pkill")]
        assert pkills
        assert not any("-9" in c or "KILL" in c for c in pkills), pkills

    def test_force_skips_the_gate(self, tmp_path):
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=1)
        res = self._run(root, tmp_path, "--force")
        assert res.returncode == 0, res.stdout + res.stderr
        assert not any(c.startswith("gate") for c in _calls(tmp_path))

    def test_agent_that_ignores_sigterm_aborts_without_sigkill(self, tmp_path):
        victim = subprocess.Popen(["sleep", "60"])
        try:
            root = _rp_tree(tmp_path, old_pid=victim.pid)
            _gate(tmp_path, rc=0)
            res = self._run(root, tmp_path)
            assert res.returncode == 1, res.stdout + res.stderr
            calls = _calls(tmp_path)
            assert not any(c.startswith("tccutil") for c in calls), "TCC сброшен при живом агенте"
            assert not any("-9" in c for c in calls if c.startswith("pkill")), calls
        finally:
            victim.kill()
            victim.wait()

    def test_loaded_launchd_unit_is_kickstarted_not_opened(self, tmp_path):
        """Юнит с KeepAlive уже поднял агента до сброса TCC: `open` только активировал бы его (со старым
        TCC), поэтому финальный перезапуск — kickstart -k, а не open bundle."""
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=0)
        _exe(tmp_path / "stubbin" / "launchctl",
             'echo "launchctl $*" >> "$STUB_LOG"\n[ "$1" = print ] && exit 0\nexit 0\n')
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        calls = _calls(tmp_path)
        assert any(c.startswith("launchctl kickstart -k") and "ai.krab.ear.agent" in c for c in calls), calls
        assert not any(c.startswith("open") and "Krab Ear.app" in c for c in calls), calls

    def test_failed_kickstart_is_an_error_not_silent_success(self, tmp_path):
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=0)
        _exe(tmp_path / "stubbin" / "launchctl",
             'echo "launchctl $*" >> "$STUB_LOG"\n[ "$1" = print ] && exit 0\n[ "$1" = kickstart ] && exit 1\nexit 0\n')
        res = self._run(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr

    def test_without_launchd_unit_bundle_is_opened(self, tmp_path):
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=0)
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        calls = _calls(tmp_path)
        assert any(c.startswith("open") and "Krab Ear.app" in c for c in calls), calls
        assert not any(c.startswith("launchctl kickstart") for c in calls), calls
        assert f"Выбери: {root / 'Krab Ear.app'}" in res.stdout, res.stdout

    @staticmethod
    def _launchctl_print(tmp_path: Path, print_body: str) -> None:
        """launchctl-заглушка: `print` выводит print_body (формат реального launchctl print)."""
        (tmp_path / "print.out").write_text(print_body)
        _exe(tmp_path / "stubbin" / "launchctl",
             'echo "launchctl $*" >> "$STUB_LOG"\n'
             f'[ "$1" = print ] && cat "{tmp_path / "print.out"}" && exit 0\nexit 0\n')

    @staticmethod
    def _choose_line(stdout: str) -> str:
        lines = [ln for ln in stdout.splitlines() if "Выбери:" in ln]
        assert len(lines) == 1, stdout
        return lines[0]

    def test_loaded_unit_instruction_names_the_unit_program_bundle(self, tmp_path):
        """Ревью Opus #2319 (P3): под launchd TCC-запись нужна для того бинаря, который запускает юнит
        (прод может идти из release-копии), а не для `Krab Ear.app` рядом со скриптом."""
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=0)
        prog = "/opt/ear release/Krab Ear.app/Contents/MacOS/KrabEarAgent"
        self._launchctl_print(
            tmp_path,
            "gui/501/ai.krab.ear.agent = {\n\tactive count = 1\n"
            "\tpath = /Users/x/Library/LaunchAgents/ai.krab.ear.agent.plist\n"
            "\ttype = LaunchAgent\n\tstate = running\n\n"
            f"\tprogram = {prog}\n\targuments = {{\n\t\t{prog}\n\t}}\n}}\n",
        )
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        line = self._choose_line(res.stdout)
        assert line.rstrip().endswith("Выбери: /opt/ear release/Krab Ear.app"), line
        assert prog in res.stdout, res.stdout  # программа юнита названа при kickstart
        calls = _calls(tmp_path)
        assert any(c.startswith("launchctl kickstart -k") for c in calls), calls

    def test_loaded_unit_with_bare_binary_program_names_the_binary(self, tmp_path):
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=0)
        prog = "/opt/ear/native/runtime/KrabEarAgent"
        self._launchctl_print(tmp_path, f"gui/501/ai.krab.ear.agent = {{\n\tprogram = {prog}\n}}\n")
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        assert self._choose_line(res.stdout).rstrip().endswith(f"Выбери: {prog}"), res.stdout

    def test_loaded_unit_without_program_falls_back_with_warning(self, tmp_path):
        """Путь программы не разобран — подсказываем bundle рядом со скриптом, но честно предупреждаем;
        перезапуск юнита всё равно идёт через kickstart."""
        root = _rp_tree(tmp_path)
        _gate(tmp_path, rc=0)
        self._launchctl_print(tmp_path, "gui/501/ai.krab.ear.agent = {\n\tstate = running\n}\n")
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        assert self._choose_line(res.stdout).rstrip().endswith(f"Выбери: {root / 'Krab Ear.app'}")
        assert "не удалось определить" in res.stderr, res.stderr
        assert any(c.startswith("launchctl kickstart -k") for c in _calls(tmp_path))

    def test_launchd_respawned_agent_does_not_abort_the_reset(self, tmp_path):
        """Ревью Opus #2304 (P2): под launchd KeepAlive агент поднимается заново сразу после SIGTERM —
        ждать надо смерти СТАРОГО PID, а не «процессов нет», иначе скрипт падал бы до сброса TCC."""
        old, new = subprocess.Popen(["sleep", "60"]), subprocess.Popen(["sleep", "60"])
        try:
            root = _rp_tree(tmp_path, old_pid=old.pid, respawn_pid=new.pid, kill_old=True)
            _gate(tmp_path, rc=0)
            res = self._run(root, tmp_path)
            assert res.returncode == 0, res.stdout + res.stderr
            assert any(c.startswith("tccutil") for c in _calls(tmp_path))
        finally:
            for p in (old, new):
                p.kill()
                p.wait()

    def test_failed_accessibility_reset_is_not_swallowed(self, tmp_path):
        root = _rp_tree(tmp_path, tcc_fail="Accessibility com.antigravity.krab-ear")
        _gate(tmp_path, rc=0)
        res = self._run(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "НЕ полностью" in res.stderr

    def test_missing_legacy_identifier_is_not_a_failure(self, tmp_path):
        root = _rp_tree(tmp_path, tcc_fail="Accessibility com.krabear.agent")
        _gate(tmp_path, rc=0)
        res = self._run(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr

    def test_unknown_argument_is_usage_error(self, tmp_path):
        root = _rp_tree(tmp_path)
        res = self._run(root, tmp_path, "--bogus")
        assert res.returncode == 2, res.stdout + res.stderr


# ═══ verify_bundled_runtime_selfcontained ══════════════════════════════════


def _vendor(tmp_path: Path, *, file_rc: int = 0, otool_body: str = "echo clean", macho: bool = True) -> Path:
    """Синтетический бандл + заглушки file/otool; python — заглушка, импорты «успешны»."""
    root = _copy(tmp_path, "verify_bundled_runtime_selfcontained.command")
    vendor = tmp_path / "vendor"
    _exe(vendor / ".venv_krab_ear" / "bin" / "python", 'echo "imports ok"\n')
    (vendor / "KrabEar" / "backend").mkdir(parents=True)
    (vendor / "KrabEar" / "backend" / "service.py").write_text("x = 1\n")
    lib = vendor / "lib" / "libfoo.dylib"
    lib.parent.mkdir(parents=True)
    lib.write_text("macho")
    stub = tmp_path / "stubbin"
    # file печатает «путь: описание» как настоящий; Mach-O — только libfoo.dylib
    _exe(
        stub / "file",
        f'[ {file_rc} -ne 0 ] && exit {file_rc}\n'
        'for f; do case "$f" in *libfoo.dylib) echo "$f: Mach-O 64-bit dynamically linked shared library arm64";;\n'
        '*) echo "$f: ASCII text";; esac; done\n'
        if macho
        else 'for f; do echo "$f: ASCII text"; done\n',
    )
    _exe(stub / "otool", otool_body + "\n")
    return root


def _run_vendor(root: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    return _run(
        [BASH, str(root / "scripts" / "verify_bundled_runtime_selfcontained.command"), str(tmp_path / "vendor")],
        _env(tmp_path),
        timeout=120,
    )


class TestVerifyBundledSelfContained:
    def test_clean_bundle_passes(self, tmp_path):
        root = _vendor(tmp_path)
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr

    def test_homebrew_dependency_still_fails(self, tmp_path):
        root = _vendor(tmp_path, otool_body='echo "/opt/homebrew/lib/libomp.dylib (compat 5)"')
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "Homebrew" in res.stderr

    @pytest.mark.parametrize("rel", ["pkg/sub/.env", "pkg/run.log", "pkg/.pytest_cache", "pkg/.venv_other", "pkg/.env.local"])
    def test_nested_forbidden_files_are_found(self, tmp_path, rel):
        root = _vendor(tmp_path)
        target = tmp_path / "vendor" / "KrabEar" / rel
        if rel.endswith(("cache", "other")):
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SECRET")
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "не место в дистрибутиве" in res.stderr

    def test_root_data_dir_is_forbidden_but_nested_data_package_is_fine(self, tmp_path):
        root = _vendor(tmp_path)
        (tmp_path / "vendor" / "KrabEar" / "pkg" / "data").mkdir(parents=True)
        assert _run_vendor(root, tmp_path).returncode == 0
        (tmp_path / "vendor" / "KrabEar" / "data").mkdir()
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr

    def test_otool_failure_is_not_reported_as_clean(self, tmp_path):
        root = _vendor(tmp_path, otool_body='echo "otool: truncated object" >&2; exit 1')
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "otool" in res.stderr

    def test_file_failure_is_not_reported_as_clean(self, tmp_path):
        root = _vendor(tmp_path, file_rc=1)
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "недостоверен" in res.stderr or "неполный" in res.stderr

    def test_empty_scan_still_fails_closed(self, tmp_path):
        root = _vendor(tmp_path, macho=False)
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "0 исполняемых Mach-O" in res.stderr

    def test_symlink_escaping_the_bundle_fails(self, tmp_path):
        root = _vendor(tmp_path)
        (tmp_path / "vendor" / "lib" / "libbar.dylib").symlink_to("/usr/bin/env")
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "symlink" in res.stderr

    def test_dangling_symlink_fails(self, tmp_path):
        root = _vendor(tmp_path)
        (tmp_path / "vendor" / "lib" / "libgone.dylib").symlink_to("nonexistent.dylib")
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 1, res.stdout + res.stderr

    def test_symlink_inside_the_bundle_is_fine(self, tmp_path):
        root = _vendor(tmp_path)
        (tmp_path / "vendor" / "lib" / "libfoo.1.dylib").symlink_to("libfoo.dylib")
        res = _run_vendor(root, tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
