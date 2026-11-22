"""KEG2-03/05/06/07/19: shell-скрипты не делают необратимого без проверки.

Тесты запускают НАСТОЯЩИЕ скрипты в изолированном дереве (``tmp_path``) с
подставными ``launchctl`` / ``pkill`` / ``sleep`` в PATH и подставным
``safe_backend_restart.command``; реальные launchd, backend и сокет не
затрагиваются.

* KEG2-03 ``remove_agent``: останавливал backend без проверки активной записи.
* KEG2-05 ``profile_gigaam_worker``: ``kickstart -k gui/501/...`` без busy-гейта.
* KEG2-06 ``cleanup_merged_worktrees``: ``worktree remove --force`` + ``branch -D``
  уничтожали незакоммиченный WIP.
* KEG2-07 ``build_bundled_runtime``: ``rm -rf`` произвольного ``--output``.
* KEG2-19 ``migrate_to_canonical_launchagent``: ``bootstrap || true`` + «Bootstrap complete».
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
UID = str(os.getuid())
ZSH = shutil.which("zsh")
needs_zsh = pytest.mark.skipif(ZSH is None, reason="zsh не установлен (shebang скриптов #!/bin/zsh)")


def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _tree(tmp_path: Path, *names: str) -> Path:
    """Копия реальных скриптов в tmp_path/scripts; корень проекта = tmp_path."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in names:
        shutil.copy2(SCRIPTS / name, scripts / name)
    return tmp_path


def _stubs(tmp_path: Path, launchctl: str = "exit 0\n") -> dict:
    bindir = tmp_path / "stubbin"
    log = tmp_path / "calls.log"
    _exe(bindir / "launchctl", 'printf "launchctl %s\\n" "$*" >> "$STUB_LOG"\n' + launchctl)
    _exe(bindir / "pkill", 'printf "pkill %s\\n" "$*" >> "$STUB_LOG"\nexit 0\n')
    _exe(bindir / "sleep", "exit 0\n")
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    env = dict(
        os.environ,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        HOME=str(home),
        STUB_LOG=str(log),
    )
    return {"env": env, "log": log, "home": home}


def _calls(ctx: dict) -> list[str]:
    return ctx["log"].read_text().splitlines() if ctx["log"].exists() else []


# ── KEG2-03 remove_agent ────────────────────────────────────────────────────


def _remove_agent_ctx(tmp_path: Path, gate_rc: int) -> dict:
    _tree(tmp_path, "remove_agent.command")
    _exe(tmp_path / "scripts" / "safe_backend_restart.command", f'echo "$*" >> "$STUB_LOG.gate"\nexit {gate_rc}\n')
    ctx = _stubs(tmp_path)
    la = ctx["home"] / "Library" / "LaunchAgents"
    (la / "ai.krab.ear.backend.plist").write_text("x")
    (la / "ai.krab.ear.agent.plist").write_text("x")
    (la / "com.krabear.agent.plist").write_text("x")
    return ctx


def _run_remove_agent(tmp_path: Path, ctx: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [ZSH, str(tmp_path / "scripts" / "remove_agent.command"), *args],
        env=ctx["env"], capture_output=True, text=True, timeout=30,
    )


@needs_zsh
class TestRemoveAgent:
    def test_busy_backend_is_not_stopped(self, tmp_path):
        ctx = _remove_agent_ctx(tmp_path, gate_rc=1)
        res = _run_remove_agent(tmp_path, ctx)
        assert res.returncode != 0, res.stdout
        joined = "\n".join(_calls(ctx))
        assert "ai.krab.ear.backend" not in joined, joined
        assert "pkill" not in joined, joined
        assert "остановлен" not in res.stdout

    def test_gate_asks_for_check_only(self, tmp_path):
        ctx = _remove_agent_ctx(tmp_path, gate_rc=0)
        _run_remove_agent(tmp_path, ctx)
        gate_log = Path(str(ctx["log"]) + ".gate")
        assert gate_log.exists(), "busy-гейт не вызван"
        assert "--check-only" in gate_log.read_text()

    def test_idle_backend_stops_and_removes_current_agent_unit(self, tmp_path):
        ctx = _remove_agent_ctx(tmp_path, gate_rc=0)
        res = _run_remove_agent(tmp_path, ctx)
        assert res.returncode == 0, res.stdout + res.stderr
        joined = "\n".join(_calls(ctx))
        assert f"bootout gui/{UID}/ai.krab.ear.backend" in joined
        # актуальный LaunchAgent агента тоже снимается, иначе KeepAlive его вернёт
        assert f"bootout gui/{UID}/ai.krab.ear.agent" in joined
        la = ctx["home"] / "Library" / "LaunchAgents"
        assert not (la / "ai.krab.ear.agent.plist").exists()
        assert not (la / "com.krabear.agent.plist").exists()

    def test_force_skips_the_gate(self, tmp_path):
        ctx = _remove_agent_ctx(tmp_path, gate_rc=1)
        res = _run_remove_agent(tmp_path, ctx, "--force")
        assert res.returncode == 0, res.stdout + res.stderr
        assert f"bootout gui/{UID}/ai.krab.ear.backend" in "\n".join(_calls(ctx))


# ── KEG2-05 profile_gigaam_worker ───────────────────────────────────────────


def _profile_ctx(tmp_path: Path, gate_rc: int) -> dict:
    _tree(tmp_path, "profile_gigaam_worker.command")
    (tmp_path / "scripts" / "memory_baseline.py").write_text("")
    _exe(tmp_path / "scripts" / "safe_backend_restart.command", f'echo "$*" >> "$STUB_LOG.gate"\nexit {gate_rc}\n')
    _exe(tmp_path / ".venv_krab_ear" / "bin" / "python", 'printf "python %s\\n" "$*" >> "$STUB_LOG"\nexit 0\n')
    return _stubs(tmp_path)


def _run_profile(tmp_path: Path, ctx: dict) -> subprocess.CompletedProcess:
    env = dict(ctx["env"], CYCLES="1", OUTPUT=str(tmp_path / "out.csv"))
    return subprocess.run(
        [ZSH, str(tmp_path / "scripts" / "profile_gigaam_worker.command")],
        env=env, capture_output=True, text=True, timeout=30,
    )


@needs_zsh
class TestProfileGigaam:
    def test_refusal_aborts_before_any_mutation(self, tmp_path):
        ctx = _profile_ctx(tmp_path, gate_rc=1)
        res = _run_profile(tmp_path, ctx)
        assert res.returncode != 0, res.stdout
        joined = "\n".join(_calls(ctx))
        assert "kickstart" not in joined, joined
        assert "setenv" not in joined, "env выставлен до подтверждения безопасного рестарта"
        assert "--once" not in joined, "профилирование (baseline) продолжилось на нерестартованном backend"

    def test_never_hardcodes_uid_501(self, tmp_path):
        ctx = _profile_ctx(tmp_path, gate_rc=0)
        res = _run_profile(tmp_path, ctx)
        assert res.returncode == 0, res.stdout + res.stderr
        joined = "\n".join(_calls(ctx))
        assert "gui/501" not in joined or UID == "501", joined
        assert "kickstart -k" not in joined, "голый kickstart вместо safe_backend_restart"

    def test_restart_goes_through_safe_script(self, tmp_path):
        ctx = _profile_ctx(tmp_path, gate_rc=0)
        _run_profile(tmp_path, ctx)
        assert Path(str(ctx["log"]) + ".gate").exists(), "safe_backend_restart не вызван"


# ── KEG2-06 cleanup_merged_worktrees ────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "codex/krab-ear-v2", str(origin))
    work = tmp_path / "main"
    _git(tmp_path, "clone", str(origin), str(work))
    _git(work, "checkout", "-b", "codex/krab-ear-v2")
    (work / "scripts").mkdir()
    shutil.copy2(SCRIPTS / "cleanup_merged_worktrees.command", work / "scripts")
    (work / "a.txt").write_text("a")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "codex/krab-ear-v2")
    return work


def _add_merged_worktree(repo: Path, name: str) -> Path:
    path = repo.parent / name
    _git(repo, "worktree", "add", "-b", name, str(path), "codex/krab-ear-v2")  # ancestor == merged
    return path


def _cleanup(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(repo / "scripts" / "cleanup_merged_worktrees.command"), "--execute"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )


class TestCleanupMergedWorktrees:
    def test_dirty_worktree_survives_execute(self, repo):
        wt = _add_merged_worktree(repo, "wip-dirty")
        (wt / "precious.txt").write_text("незакоммиченная работа")
        (wt / "a.txt").write_text("правка отслеживаемого файла")
        _cleanup(repo)
        assert (wt / "precious.txt").read_text() == "незакоммиченная работа"
        assert (wt / "a.txt").read_text() == "правка отслеживаемого файла"

    def test_clean_merged_worktree_is_removed(self, repo):
        wt = _add_merged_worktree(repo, "done-clean")
        res = _cleanup(repo)
        assert not wt.exists(), res.stdout + res.stderr
        assert "done-clean" not in _git(repo, "branch", "--list", "done-clean")

    def test_unmerged_commits_branch_is_not_force_deleted(self, repo):
        # ветка «слита» по ancestor-проверке относительно origin, но в ней есть коммит, которого
        # нет в HEAD основной копии: безопасное удаление (-d) обязано это увидеть либо оставить ветку.
        wt = _add_merged_worktree(repo, "has-extra")
        (wt / "extra.txt").write_text("e")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "extra")
        _git(repo, "push", "origin", "has-extra:codex/krab-ear-v2")  # теперь ветка предок origin
        _git(repo, "fetch", "origin")
        res = _cleanup(repo)
        # worktree чист, ветка слита в origin → удаление допустимо; но не через force-метод,
        # перезаписывающий состояние: главное, что скрипт не падает и не оставляет мусор.
        assert res.returncode == 0, res.stdout + res.stderr

    def test_source_has_no_force_flags(self):
        text = (SCRIPTS / "cleanup_merged_worktrees.command").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "--force" not in code
        assert "branch -D" not in code


# ── KEG2-07 build_bundled_runtime ───────────────────────────────────────────


def _run_build(tmp_path: Path, output: str) -> subprocess.CompletedProcess:
    _tree(tmp_path, "build_bundled_runtime.command")
    (tmp_path / "KrabEar").mkdir(exist_ok=True)
    ctx = _stubs(tmp_path)
    # curl не должен вызываться вовсе: проверка --output идёт ДО скачивания
    _exe(tmp_path / "stubbin" / "curl", 'echo "curl $*" >> "$STUB_LOG"\nexit 22\n')
    res = subprocess.run(
        ["/bin/bash", str(tmp_path / "scripts" / "build_bundled_runtime.command"), "--output", output],
        env=ctx["env"], capture_output=True, text=True, timeout=30,
    )
    res.ctx = ctx  # type: ignore[attr-defined]
    return res


class TestBuildBundledRuntime:
    @pytest.mark.parametrize(
        "output",
        ["/", "$HOME", "$ROOT", "$ROOT/KrabEar", "$ROOT/..", ".", "", "$ROOT/scripts"],
    )
    def test_dangerous_output_refused_before_anything_runs(self, tmp_path, output):
        home = tmp_path / "home"
        resolved = output.replace("$HOME", str(home)).replace("$ROOT", str(tmp_path))
        res = _run_build(tmp_path, resolved)
        assert res.returncode != 0, res.stdout + res.stderr
        assert "curl" not in "\n".join(_calls(res.ctx)), "скачивание началось до проверки --output"
        assert (tmp_path / "scripts" / "build_bundled_runtime.command").exists()
        assert (tmp_path / "KrabEar").exists()

    def test_foreign_nonempty_dir_refused(self, tmp_path):
        target = tmp_path.parent / (tmp_path.name + "_foreign")
        target.mkdir()
        (target / "mine.txt").write_text("чужое")
        res = _run_build(tmp_path, str(target))
        assert res.returncode != 0
        assert (target / "mine.txt").read_text() == "чужое"
        assert "curl" not in "\n".join(_calls(res.ctx))

    def test_project_source_dir_refused_even_when_empty(self, tmp_path):
        res = _run_build(tmp_path, str(tmp_path / "KrabEar"))
        assert res.returncode != 0
        assert "curl" not in "\n".join(_calls(res.ctx))

    @pytest.mark.parametrize("kind", ["new", "empty", "marked"])
    def test_legitimate_outputs_pass_validation(self, tmp_path, kind):
        target = tmp_path.parent / (tmp_path.name + "_out_" + kind)
        if kind != "new":
            target.mkdir()
        if kind == "marked":
            (target / ".krab_runtime_output").write_text("")
            (target / "old.bin").write_text("x")
        res = _run_build(tmp_path, str(target))
        # curl-заглушка падает: сборка не завершится, но валидация --output пройдена
        assert "curl" in "\n".join(_calls(res.ctx)), res.stdout + res.stderr

    def test_default_build_vendor_still_allowed(self, tmp_path):
        vendor = tmp_path / "build" / "vendor"
        vendor.mkdir(parents=True)
        (vendor / "stale").write_text("прошлая сборка без маркера")
        res = _run_build(tmp_path, str(vendor))
        assert "curl" in "\n".join(_calls(res.ctx)), res.stdout + res.stderr

    def test_symlink_output_refused(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "keep").write_text("k")
        link = tmp_path / "link"
        link.symlink_to(real)
        res = _run_build(tmp_path, str(link))
        assert res.returncode != 0
        assert (real / "keep").exists()


# ── KEG2-19 migrate_to_canonical_launchagent ────────────────────────────────


def _migrate(tmp_path: Path, launchctl: str) -> tuple[subprocess.CompletedProcess, dict]:
    _tree(tmp_path, "migrate_to_canonical_launchagent.command")
    (tmp_path / "Krab Ear.app").mkdir()
    ctx = _stubs(tmp_path, launchctl)
    res = subprocess.run(
        [ZSH, str(tmp_path / "scripts" / "migrate_to_canonical_launchagent.command")],
        env=ctx["env"], capture_output=True, text=True, timeout=30,
    )
    return res, ctx


@needs_zsh
class TestMigrateCanonical:
    def test_bootstrap_failure_is_not_reported_as_complete(self, tmp_path):
        res, _ = _migrate(
            tmp_path,
            'case "$1" in print) exit 113;; bootstrap) echo "Bootstrap failed: 5" >&2; exit 5;; esac\nexit 0\n',
        )
        assert res.returncode != 0, res.stdout
        assert "Bootstrap complete" not in res.stdout

    def test_already_loaded_is_not_an_error(self, tmp_path):
        res, _ = _migrate(
            tmp_path,
            'case "$1" in print) exit 0;; bootstrap) exit 5;; esac\nexit 0\n',
        )
        assert res.returncode == 0, res.stdout + res.stderr
        assert "Migration done" in res.stdout

    def test_clean_bootstrap_succeeds(self, tmp_path):
        res, ctx = _migrate(tmp_path, "exit 0\n")
        assert res.returncode == 0, res.stdout + res.stderr
        assert "Bootstrap complete" in res.stdout
        assert any("bootstrap" in c for c in _calls(ctx))


# ═══ Пачка 2: KEG2-01 / 04 / 08+13 / 14 / 15 ══════════════════════════════════

import json  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import sqlite3  # noqa: E402
import tarfile  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402


def _function_source(script: str, name: str) -> str:
    text = (SCRIPTS / script).read_text()
    match = re.search(rf"^{name}\(\) \{{.*?\n\}}\n", text, re.S | re.M)
    assert match, f"{name}() не найдена в {script}"
    return match.group()


# ── KEG2-01 create_stable_backup ────────────────────────────────────────────

SECRET = "sk-LIVE-SECRET-DO-NOT-ARCHIVE"


def _backup_tree(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "create_stable_backup.command", root / "scripts")
    (root / "KrabEar").mkdir()
    (root / "KrabEar" / "app.py").write_text("print('tracked')\n")
    (root / "KrabEar" / ".env").write_text(f"KEY={SECRET}\n")
    (root / "KrabEar" / "data").mkdir()
    (root / "KrabEar" / "data" / "history.ndjson").write_text("приватная диктовка\n")
    (root / ".gitignore").write_text(".env\ndata/\n_STABLE_BACKUPS/\n")
    (root / "README.md").write_text("readme\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".gitignore", "README.md", "KrabEar/app.py", "scripts")
    _git(root, "commit", "-q", "-m", "init")
    (root / "KrabEar" / "new_untracked.py").write_text("print('new')\n")  # не в индексе, не ignored
    return root


def _run_backup(root: Path) -> Path:
    env = dict(os.environ, HOME=str(root.parent / "home"))
    res = subprocess.run([ZSH, str(root / "scripts" / "create_stable_backup.command")], env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    archives = sorted((root / "_STABLE_BACKUPS").glob("*.tar.gz"))
    assert len(archives) == 1, res.stdout
    return archives[0]


@needs_zsh
class TestStableBackup:
    def test_ignored_secrets_and_data_never_enter_archive(self, tmp_path):
        archive = _run_backup(_backup_tree(tmp_path))
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
        assert not any(n.endswith(".env") for n in names), names
        assert not any("history.ndjson" in n for n in names), names
        assert SECRET.encode() not in blob

    def test_tracked_and_untracked_sources_are_kept(self, tmp_path):
        archive = _run_backup(_backup_tree(tmp_path))
        with tarfile.open(archive) as tar:
            names = {n.lstrip("./") for n in tar.getnames()}
        assert "KrabEar/app.py" in names
        assert "KrabEar/new_untracked.py" in names

    def test_archive_and_dir_are_private(self, tmp_path):
        root = _backup_tree(tmp_path)
        archive = _run_backup(root)
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE((root / "_STABLE_BACKUPS").stat().st_mode) == 0o700

    def test_deleted_tracked_file_does_not_break_backup(self, tmp_path):
        root = _backup_tree(tmp_path)
        (root / "README.md").unlink()  # tracked, удалён с диска
        _run_backup(root)


# ── KEG2-04 migrate_to_inprocess_rest: fail-closed гейт ─────────────────────


class _IpcServer:
    """Unix-сокет, отвечающий на любой запрос заданной сырой строкой."""

    def __init__(self, path: str, raw: str | None) -> None:
        self.path, self.raw = path, raw
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        self.srv.settimeout(0.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(1)
                    conn.recv(4096)
                    if self.raw is not None:
                        conn.sendall((self.raw + "\n").encode())
                except OSError:
                    pass

    def close(self) -> None:
        self.stop.set()
        self.srv.close()
        self.thread.join(timeout=2)


def _run_migrate_rest(tmp_path: Path, raw: str | None, *args: str, socket_present: bool = True):
    with tempfile.TemporaryDirectory(prefix="ke-mig-", dir="/tmp") as short:
        home = Path(short)
        sock_dir = home / "Library" / "Application Support" / "KrabEar"
        sock_dir.mkdir(parents=True)
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        # print: «загружен», пока не было bootout (как настоящий launchd)
        ctx = _stubs(
            tmp_path,
            'case "$1" in print) [ ! -e "$STUB_LOG.bootout" ]; exit $?;; bootout) touch "$STUB_LOG.bootout";; esac\nexit 0\n',
        )
        env = dict(ctx["env"], HOME=str(home))
        server = _IpcServer(str(sock_dir / "krabear.sock"), raw) if socket_present else None
        try:
            res = subprocess.run(["/bin/bash", str(SCRIPTS / "migrate_to_inprocess_rest.command"), *args],
                                 env=env, capture_output=True, text=True, timeout=30)
        finally:
            if server:
                server.close()
        return res, ctx


_OK_REC = json.dumps({"id": "1", "ok": True, "result": {"is_recording": False}})


class TestMigrateInprocessRestFailClosed:
    def test_missing_socket_refuses_instead_of_skipping_gate(self, tmp_path):
        res, ctx = _run_migrate_rest(tmp_path, None, socket_present=False)
        assert res.returncode == 1, res.stdout + res.stderr
        assert not any("bootout" in c for c in _calls(ctx)), _calls(ctx)

    @pytest.mark.parametrize("raw", ["", "not json at all", '{"id":"1","ok":true,"result":{}}'])
    def test_unconfirmed_state_refuses(self, tmp_path, raw):
        res, ctx = _run_migrate_rest(tmp_path, raw)
        assert res.returncode == 1, res.stdout + res.stderr
        assert not any("bootout" in c or "bootstrap" in c for c in _calls(ctx)), _calls(ctx)

    def test_force_allows_dead_backend(self, tmp_path):
        res, ctx = _run_migrate_rest(tmp_path, None, "--force", socket_present=False)
        assert res.returncode == 0, res.stdout + res.stderr
        assert any("bootout" in c for c in _calls(ctx)), _calls(ctx)


# ── KEG2-08/13 audit_tcc_permissions ────────────────────────────────────────


def _tcc_home(tmp_path: Path, client: str) -> Path:
    home = tmp_path / "home"
    db_dir = home / "Library" / "Application Support" / "com.apple.TCC"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(db_dir / "TCC.db")
    con.execute("CREATE TABLE access (service TEXT, client TEXT, client_type INT, auth_value INT, last_modified INT)")
    con.execute("INSERT INTO access VALUES ('kTCCServiceMicrophone', ?, 0, 2, 1700000000)", (client,))
    con.commit()
    con.close()
    return home


def _run_tcc(tmp_path: Path, client: str, out: Path):
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "audit_tcc_permissions.command", root / "scripts")
    home = _tcc_home(tmp_path, client)
    env = dict(os.environ, HOME=str(home), KRAB_TCC_AUDIT_OUT=str(out))
    return subprocess.run(["/bin/bash", str(root / "scripts" / "audit_tcc_permissions.command")], env=env,
                          capture_output=True, text=True, timeout=60)


class TestTccAudit:
    def test_client_name_is_data_not_code(self, tmp_path):
        marker = tmp_path / "PWNED"
        payload = f'com.antigravity.krab-ear""" + str(open("{marker}", "w").write("x")) + """'
        out = tmp_path / "out" / "audit.json"
        out.parent.mkdir()
        res = _run_tcc(tmp_path, payload, out)
        assert not marker.exists(), "значение из TCC.db выполнилось как Python"
        assert res.returncode == 0, res.stdout + res.stderr
        data = json.loads(out.read_text())
        assert any(e["client"] == payload for e in data["tcc_entries"])

    def test_symlink_at_output_path_is_not_followed(self, tmp_path):
        victim = tmp_path / "victim.txt"
        victim.write_text("НЕ ТРОГАТЬ")
        out = tmp_path / "out" / "audit.json"
        out.parent.mkdir()
        out.symlink_to(victim)
        res = _run_tcc(tmp_path, "com.antigravity.krab-ear", out)
        assert victim.read_text() == "НЕ ТРОГАТЬ"
        assert res.returncode == 0, res.stdout + res.stderr
        assert not out.is_symlink()
        assert json.loads(out.read_text())["tcc_entries"]

    def test_output_is_private(self, tmp_path):
        out = tmp_path / "out" / "audit.json"
        out.parent.mkdir()
        _run_tcc(tmp_path, "com.antigravity.krab-ear", out)
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_default_output_is_not_a_fixed_world_writable_tmp_name(self):
        text = (SCRIPTS / "audit_tcc_permissions.command").read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert '"/tmp/krab-tcc-audit.json"' not in code


# ── KEG2-14 check_port без GNU timeout ──────────────────────────────────────


def _bash_only_path(tmp_path: Path) -> str:
    only = tmp_path / "onlybash"
    only.mkdir(exist_ok=True)
    (only / "bash").symlink_to(shutil.which("bash"))
    return str(only)


@pytest.mark.parametrize(
    "script,func",
    [("healthcheck_voice_assistant.command", "check_port"), ("observe_production.command", "_port_up")],
)
class TestPortCheckWithoutGnuTimeout:
    def _run(self, tmp_path, script, func, port):
        body = _function_source(script, func)
        env = dict(os.environ, PATH=_bash_only_path(tmp_path))
        return subprocess.run([shutil.which("bash"), "-c", f"{body}\n{func} {port}"], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_listening_port_is_reported_up(self, tmp_path, script, func):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            res = self._run(tmp_path, script, func, srv.getsockname()[1])
        finally:
            srv.close()
        assert res.returncode == 0, res.stderr

    def test_closed_port_is_reported_down(self, tmp_path, script, func):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        assert self._run(tmp_path, script, func, port).returncode != 0


# ── KEG2-15 prefetch_whisper_models: имя кэша HF и полнота ──────────────────


def _is_cached(tmp_path: Path, repo_id: str) -> str:
    body = _function_source("prefetch_whisper_models.command", "is_cached")
    cache = tmp_path / "hub"
    cache.mkdir(exist_ok=True)
    res = subprocess.run(["/bin/bash", "-c", f'HF_CACHE_DIR="{cache}"\n{body}\nis_cached "{repo_id}"'],
                         capture_output=True, text=True, timeout=10)
    return res.stdout.strip()


class TestPrefetchCacheDetection:
    def test_double_dash_cache_name_is_a_hit(self, tmp_path):
        snap = tmp_path / "hub" / "models--mlx-community--whisper-large" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        assert _is_cached(tmp_path, "mlx-community/whisper-large") == "HIT"

    def test_single_dash_lookalike_is_not_a_hit(self, tmp_path):
        snap = tmp_path / "hub" / "models--mlx-community-whisper-large" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        assert _is_cached(tmp_path, "mlx-community/whisper-large") == "MISS"

    def test_incomplete_download_is_a_miss(self, tmp_path):
        base = tmp_path / "hub" / "models--mlx-community--whisper-large"
        (base / "snapshots" / "abc123").mkdir(parents=True)
        (base / "blobs").mkdir()
        (base / "blobs" / "deadbeef.incomplete").write_text("partial")
        assert _is_cached(tmp_path, "mlx-community/whisper-large") == "MISS"

    def test_empty_snapshot_dir_is_a_miss(self, tmp_path):
        (tmp_path / "hub" / "models--mlx-community--whisper-large" / "snapshots" / "abc123").mkdir(parents=True)
        assert _is_cached(tmp_path, "mlx-community/whisper-large") == "MISS"


# ═══ Пачка 3 ══════════════════════════════════════════════════════════════════
# ── KEG2-02 observe_production: содержимое диктовок и логов — только по явному флагу ──

TRANSCRIPT_MARK = "СЕКРЕТНАЯ-ДИКТОВКА-777"
LOG_MARK = "SECRET-LOG-LINE-888"
BROKEN_MARK = "BROKEN-LINE-PAYLOAD-999"


def _observe(tmp_path: Path, *args: str, settings: str | None = '{"privacy_mode_enabled": false}') -> str:
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "krab_ear_backend.log").write_text(f"INFO start\nINFO {LOG_MARK}\n")
    (data / "history.ndjson").write_text(
        json.dumps({"ts": "2026-11-22T10:00:00", "language": "ru", "duration_s": 3, "text": TRANSCRIPT_MARK}) + "\n"
        + f"{{not json {BROKEN_MARK}\n"
    )
    if settings is not None:
        (data / "settings.json").write_text(settings)
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "observe_production.command", root / "scripts")
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), KRAB_EAR_DATA_DIR=str(data))
    res = subprocess.run([ZSH, str(root / "scripts" / "observe_production.command"), *args], env=env,
                         capture_output=True, text=True, timeout=120)
    return res.stdout + res.stderr


@needs_zsh
class TestObserveProductionPrivacy:
    def test_default_run_prints_no_transcript_or_log_content(self, tmp_path):
        out = _observe(tmp_path)
        for mark in (TRANSCRIPT_MARK, LOG_MARK, BROKEN_MARK):
            assert mark not in out, f"{mark} просочился в вывод по умолчанию"
        assert "history.ndjson" in out  # метаданные по-прежнему показываются

    def test_flag_with_privacy_off_shows_content(self, tmp_path):
        out = _observe(tmp_path, "--show-content")
        assert TRANSCRIPT_MARK in out
        assert LOG_MARK in out
        assert BROKEN_MARK not in out, "содержимое битой строки не должно печататься даже с флагом"

    @pytest.mark.parametrize("settings", ['{"privacy_mode_enabled": true}', "{broken json", "[]", "{}", None])
    def test_flag_never_overrides_privacy_on_or_unknown(self, tmp_path, settings):
        out = _observe(tmp_path, "--show-content", settings=settings)
        assert TRANSCRIPT_MARK not in out
        assert LOG_MARK not in out


# ── KEG2-10 install_rest_launchagent: busy-гейт и подтверждение запуска ────


def _rest_installer(tmp_path: Path, gate_rc: int, launchctl_body: str, *args: str):
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    for name in ("install_rest_launchagent.command", "render_launchagent_plist.py"):
        shutil.copy2(SCRIPTS / name, root / "scripts")
    (root / "KrabEar" / "launchagents").mkdir(parents=True)
    shutil.copy2(ROOT / "KrabEar" / "launchagents" / "ai.krab.ear.rest.plist.template",
                 root / "KrabEar" / "launchagents")
    (root / ".venv_krab_ear").mkdir()
    _exe(root / "scripts" / "safe_backend_restart.command", f'echo "$*" >> "$STUB_LOG.gate"\nexit {gate_rc}\n')
    ctx = _stubs(tmp_path, launchctl_body)
    env = dict(ctx["env"], HF_TOKEN="hf_test_token_not_real")
    res = subprocess.run(["/bin/bash", str(root / "scripts" / "install_rest_launchagent.command"), *args],
                         env=env, capture_output=True, text=True, timeout=60)
    return res, ctx


# print: «загружен с pid» после bootstrap (как живой launchd)
_LOADED_LAUNCHCTL = (
    'case "$1" in\n'
    '  print) if [ -e "$STUB_LOG.boot" ]; then echo "pid = 4242"; exit 0; fi; exit 1;;\n'
    '  bootstrap) touch "$STUB_LOG.boot";;\n'
    'esac\nexit 0\n'
)


class TestInstallRestLaunchagent:
    def test_busy_backend_refuses_before_any_launchctl_call(self, tmp_path):
        res, ctx = _rest_installer(tmp_path, 1, _LOADED_LAUNCHCTL)
        assert res.returncode != 0, res.stdout + res.stderr
        assert not any("bootout" in c or "bootstrap" in c for c in _calls(ctx)), _calls(ctx)
        assert not (ctx["home"] / "Library" / "LaunchAgents" / "ai.krab.ear.rest.plist").exists()

    def test_idle_installs_and_confirms_running(self, tmp_path):
        res, ctx = _rest_installer(tmp_path, 0, _LOADED_LAUNCHCTL)
        assert res.returncode == 0, res.stdout + res.stderr
        assert any("bootstrap" in c for c in _calls(ctx))
        assert Path(str(ctx["log"]) + ".gate").read_text().strip() == "--check-only"

    def test_force_skips_the_gate(self, tmp_path):
        res, ctx = _rest_installer(tmp_path, 1, _LOADED_LAUNCHCTL, "--force")
        assert res.returncode == 0, res.stdout + res.stderr

    def test_bootstrap_that_never_starts_is_not_reported_as_running(self, tmp_path):
        # bootstrap «успешен», но юнит так и не загрузился/не получил pid
        res, _ = _rest_installer(tmp_path, 0, 'case "$1" in print) exit 1;; esac\nexit 0\n')
        assert res.returncode != 0, res.stdout
        assert "установлен и запущен" not in res.stdout


# ── G2-KE02a-F03/F04: validate_backup и restore_backup_preview ────────────


def _backup_dir_tree(tmp_path: Path, *names: str) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    for name in names:
        shutil.copy2(SCRIPTS / name, root / "scripts")
    backups = root / "_STABLE_BACKUPS"
    backups.mkdir()
    return root, backups


def _make_archive(path: Path, payload: str) -> None:
    src = path.parent / ("src_" + path.stem)
    src.mkdir()
    (src / "f.txt").write_text(payload)
    subprocess.run(["tar", "-czf", str(path), "-C", str(src), "."], check=True)


def _sha(path: Path) -> str:
    return subprocess.run(["shasum", "-a", "256", str(path)], capture_output=True, text=True,
                          check=True).stdout.split()[0]


@needs_zsh
class TestValidateBackup:
    def _run(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run([ZSH, str(root / "scripts" / "validate_backup.command")], capture_output=True,
                              text=True, timeout=60)

    def test_sidecar_naming_a_different_file_does_not_validate_the_archive(self, tmp_path):
        root, backups = _backup_dir_tree(tmp_path, "validate_backup.command")
        archive = backups / "krabear_stable_20261122_010101.tar.gz"
        _make_archive(archive, "real archive payload")
        decoy = backups / "decoy.bin"
        decoy.write_text("decoy")
        (backups / "krabear_stable_20261122_010101.sha256").write_text(f"{_sha(decoy)}  decoy.bin\n")
        res = self._run(root)
        assert res.returncode != 0, "checksum подложного файла принят за checksum архива"
        assert "✅" not in res.stdout

    def test_matching_checksum_with_absolute_old_path_validates(self, tmp_path):
        root, backups = _backup_dir_tree(tmp_path, "validate_backup.command")
        archive = backups / "krabear_stable_20261122_010101.tar.gz"
        _make_archive(archive, "payload")
        # формат, который пишет create_stable_backup: абсолютный путь прежнего места
        (backups / "krabear_stable_20261122_010101.sha256").write_text(f"{_sha(archive)}  /old/place/{archive.name}\n")
        res = self._run(root)
        assert res.returncode == 0, res.stdout + res.stderr

    def test_corrupted_archive_fails(self, tmp_path):
        root, backups = _backup_dir_tree(tmp_path, "validate_backup.command")
        archive = backups / "krabear_stable_20261122_010101.tar.gz"
        _make_archive(archive, "payload")
        (backups / "krabear_stable_20261122_010101.sha256").write_text(f"{_sha(archive)}  {archive.name}\n")
        archive.write_bytes(archive.read_bytes() + b"tamper")
        assert self._run(root).returncode != 0


@needs_zsh
class TestRestorePreview:
    def test_same_second_runs_get_distinct_dirs(self, tmp_path):
        root, backups = _backup_dir_tree(tmp_path, "restore_backup_preview.command")
        _make_archive(backups / "krabear_stable_20261122_010101.tar.gz", "payload")
        stub = tmp_path / "stubbin"
        _exe(stub / "date", 'echo 20261122_010101\n')
        env = dict(os.environ, PATH=f"{stub}{os.pathsep}{os.environ['PATH']}")
        dirs = []
        for _ in range(2):
            res = subprocess.run([ZSH, str(root / "scripts" / "restore_backup_preview.command")], env=env,
                                 capture_output=True, text=True, timeout=60)
            assert res.returncode == 0, res.stdout + res.stderr
            dirs.append(next(line.split(": ", 1)[1] for line in res.stdout.splitlines()
                             if line.startswith("Preview dir:")))
        assert dirs[0] != dirs[1], "повторный preview смешался с предыдущим каталогом"
        assert all(Path(d).is_dir() for d in dirs)


# ── W6-B V03: разбор worktree через --porcelain (пути с пробелами), main пропускается ──


def _dry_and_exec(repo: Path, execute: bool) -> subprocess.CompletedProcess:
    args = ["--execute"] if execute else []
    return subprocess.run(["/bin/bash", str(repo / "scripts" / "cleanup_merged_worktrees.command"), *args],
                          cwd=repo, capture_output=True, text=True, timeout=60)


def _space_worktree(repo: Path, branch: str) -> Path:
    path = repo.parent / "dir with spaces" / branch
    path.parent.mkdir(exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(path), "codex/krab-ear-v2")
    return path


class TestCleanupWorktreesPathsWithSpaces:
    def test_clean_merged_worktree_with_spaces_in_path_is_removed(self, repo):
        wt = _space_worktree(repo, "spaced-clean")
        res = _dry_and_exec(repo, True)
        assert not wt.exists(), res.stdout + res.stderr

    def test_dirty_worktree_with_spaces_in_path_survives(self, repo):
        wt = _space_worktree(repo, "spaced-dirty")
        (wt / "wip.txt").write_text("работа")
        _dry_and_exec(repo, True)
        assert (wt / "wip.txt").read_text() == "работа"

    def test_dry_run_marks_dirty_as_kept_not_candidate(self, repo):
        wt = _space_worktree(repo, "spaced-dry")
        (wt / "wip.txt").write_text("x")
        res = _dry_and_exec(repo, False)
        assert wt.exists()
        assert "KEEP (dirty)" in res.stdout, res.stdout
        assert "Candidates for removal  : 0" in res.stdout, res.stdout

    def test_fresh_branch_without_commits_but_dirty_is_preserved_with_every_flag(self, repo):
        wt = _add_merged_worktree(repo, "fresh-agent")
        (wt / "agent_output.md").write_text("несохранённый результат агента")
        for execute in (False, True):
            _dry_and_exec(repo, execute)
        assert (wt / "agent_output.md").exists()
        assert "fresh-agent" in _git(repo, "branch", "--list", "fresh-agent")

    def test_main_worktree_is_never_touched(self, repo):
        _dry_and_exec(repo, True)
        assert (repo / "a.txt").exists()
        assert "codex/krab-ear-v2" in _git(repo, "branch", "--list", "codex/krab-ear-v2")


# ── W6-B V34/V35 e2e_privacy_gates.py ───────────────────────────────────────

GATES = SCRIPTS / "e2e_privacy_gates.py"


class _FakeBackend:
    """Фейковый IPC-backend: настройки, история, подсчёт обращений."""

    def __init__(self, path: str, *, privacy_sticks: bool = True, drop_second_get_settings: bool = False,
                 leak_when_private: bool = False, canary_visible: bool = True,
                 stale_settings_read: bool = False) -> None:
        self.privacy = False
        self.requests: list[str] = []
        self.items: dict[str, str] = {}
        self.privacy_sticks = privacy_sticks
        self.drop_second_get_settings = drop_second_get_settings
        self.leak_when_private = leak_when_private
        self.canary_visible = canary_visible
        self.stale_settings_read = stale_settings_read
        self._get_settings_calls = 0
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(16)
        self.srv.settimeout(0.1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _handle(self, req: dict) -> dict | None:
        method, params = req["method"], req.get("params") or {}
        self.requests.append(method)
        if method == "add_history_item":
            item_id = f"id{len(self.items)}"
            self.items[item_id] = params.get("text", "")
            return {"ok": True, "result": {"id": item_id}}
        if method == "get_settings":
            self._get_settings_calls += 1
            if self.drop_second_get_settings and self._get_settings_calls == 2:
                return None
            reported = False if self.stale_settings_read else self.privacy
            return {"ok": True, "result": {"privacy_mode_enabled": reported}}
        if method == "set_settings":
            if self.privacy_sticks and "privacy_mode_enabled" in params:
                self.privacy = bool(params["privacy_mode_enabled"])
            return {"ok": True, "result": {}}
        if method == "delete_history_item":
            self.items.pop(params.get("id"), None)
            return {"ok": True, "result": {}}
        if method == "search_history":
            if self.privacy and not self.leak_when_private:
                return {"ok": True, "result": {"items": []}}
            if not self.privacy and not self.canary_visible:
                return {"ok": True, "result": {"items": []}}
            return {"ok": True, "result": {"items": list(self.items.values())}}
        return {"ok": True, "result": {}}

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2)
                    data = b""
                    while b"\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    resp = self._handle(json.loads(data))
                    if resp is not None:
                        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        self.stop.set()
        self.srv.close()
        self.thread.join(timeout=2)


def _run_gates(*args: str, home: str | None = None, **backend_kwargs):
    """Запуск скрипта против фейкового backend. Возвращает (результат, backend)."""
    with tempfile.TemporaryDirectory(prefix="ke-pg-", dir="/tmp") as short:
        sock_path = str(Path(short) / "t.sock")
        backend = _FakeBackend(sock_path, **backend_kwargs)
        try:
            argv = [sys.executable, str(GATES), *(a.replace("{SOCK}", sock_path) for a in args)]
            env = dict(os.environ, HOME=home or short)
            res = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        finally:
            backend.close()
        return res, backend


class TestE2ePrivacyGates:
    def test_healthy_backend_passes_restores_privacy_and_cleans_seed(self):
        res, backend = _run_gates("{SOCK}")
        assert res.returncode == 0, res.stdout + res.stderr
        assert backend.privacy is False
        assert backend.items == {}, "канареечные записи остались в истории"

    def test_no_socket_argument_refuses_without_touching_anything(self, tmp_path):
        res = subprocess.run([sys.executable, str(GATES)], env=dict(os.environ, HOME=str(tmp_path)),
                             capture_output=True, text=True, timeout=30)
        assert res.returncode == 2, res.stdout + res.stderr
        assert "socket" in (res.stdout + res.stderr).lower()

    def test_production_default_socket_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="ke-pg-", dir="/tmp") as short:
            prod = Path(short) / "Library" / "Application Support" / "KrabEar"
            prod.mkdir(parents=True)
            backend = _FakeBackend(str(prod / "krabear.sock"))
            try:
                res = subprocess.run([sys.executable, str(GATES), str(prod / "krabear.sock")],
                                     env=dict(os.environ, HOME=short), capture_output=True, text=True, timeout=30)
            finally:
                backend.close()
        assert res.returncode == 2, res.stdout + res.stderr
        assert backend.requests == [], "скрипт обратился к боевому сокету"

    def test_privacy_restored_when_script_crashes_midway(self):
        # второй get_settings (проверка, что privacy включилась) рвёт соединение → исключение вне try/except
        res, backend = _run_gates("{SOCK}", drop_second_get_settings=True)
        assert res.returncode != 0
        assert backend.privacy is False, "privacy осталась включённой после сбоя скрипта"

    def test_unconfirmed_privacy_switch_is_a_failure_not_a_pass(self):
        # backend не подтверждает включение privacy (устаревшее чтение), утечек нет — но проверка
        # не вправе выдавать PASS: need() раньше не попадал в results.
        res, _ = _run_gates("{SOCK}", stale_settings_read=True)
        assert res.returncode != 0, "включение privacy не подтверждено, а проверка прошла ложно"

    def test_leak_under_privacy_fails(self):
        res, _ = _run_gates("{SOCK}", leak_when_private=True)
        assert res.returncode != 0

    def test_invisible_canary_fails(self):
        res, _ = _run_gates("{SOCK}", canary_visible=False)
        assert res.returncode != 0
