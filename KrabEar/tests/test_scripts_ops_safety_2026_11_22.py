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
# KE_SIMULATE_CI=1 имитирует Linux-раннер (нет zsh/plutil): так локально ловятся платформенные падения.
ZSH = None if os.environ.get("KE_SIMULATE_CI") else shutil.which("zsh")
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
        gate_calls = Path(str(ctx["log"]) + ".gate").read_text().splitlines()
        # сначала проверка занятости, затем сам безопасный рестарт (без флагов)
        assert gate_calls == ["--check-only", ""], gate_calls


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
        assert res.returncode == 0, res.stdout + res.stderr
        # worktree чист и ветка слита → удалён штатно; ветка же снимается только безопасным -d,
        # который отказывает, пока локальный HEAD не содержит её коммитов (BRANCH -D не применяется).
        assert not wt.exists()
        assert "has-extra" in _git(repo, "branch", "--list", "has-extra")

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
    # скрипт требует arm64: тесты не должны зависеть от архитектуры/ОС хоста
    _exe(tmp_path / "stubbin" / "uname", 'echo arm64\n')
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

    @pytest.mark.parametrize("suffix", ["", "/", "/."])
    def test_symlink_output_refused_even_when_target_is_allowed(self, tmp_path, suffix):
        # цель внутри build/ разрешена сама по себе: отказ обязан дать именно проверка симлинка
        real = tmp_path / "build" / "vendor_real"
        real.mkdir(parents=True)
        (real / "keep").write_text("k")
        link = tmp_path / "link"
        link.symlink_to(real)
        res = _run_build(tmp_path, str(link) + suffix)
        assert res.returncode != 0
        assert (real / "keep").exists(), "rm -rf прошёл через симлинк"
        assert "curl" not in "\n".join(_calls(res.ctx)), "валидация пропустила симлинк"
        assert "симлинк" in (res.stdout + res.stderr)


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

    def test_tracked_sources_kept_untracked_not_included(self, tmp_path):
        archive = _run_backup(_backup_tree(tmp_path))
        with tarfile.open(archive) as tar:
            names = {n.lstrip("./") for n in tar.getnames()}
        assert "KrabEar/app.py" in names
        assert "KrabEar/new_untracked.py" not in names  # неотслеживаемое — только через git add

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
        (snap / "weights.npz").write_text("w")
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
    # plutil есть только на macOS; здесь важна логика гейта, а не валидатор plist (его проверяет
    # TestInstallAgentLaunchagent на macOS), поэтому заглушка делает тест платформонезависимым.
    _exe(tmp_path / "stubbin" / "plutil", "exit 0\n")
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


# ── W6-B V06 pre_merge_py312_check: HARNESS_VENV не удаляется вслепую ─────
# 🔴 Скрипт запускается ТОЛЬКО как копия в tmp_path (REPO_ROOT = tmp-дерево): красная фаза
# исполняет баг по-настоящему, и опасные значения не должны указывать на реальный репозиторий.


def _harness_tree(tmp_path: Path) -> Path:
    root = tmp_path / "harness_proj"
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPTS / "pre_merge_py312_check.sh", root / "scripts")
    (root / "KrabEar").mkdir(exist_ok=True)
    (root / "KrabEar" / "requirements.txt").write_text("")
    stub = tmp_path / "stubbin"
    _exe(stub / "python3.12", 'echo "Python 3.12.0"\nexit 1\n')  # создание venv всё равно не удастся
    return root


def _harness(tmp_path: Path, harness_venv: Path, **extra_env: str) -> subprocess.CompletedProcess:
    root = _harness_tree(tmp_path)
    stub = tmp_path / "stubbin"
    env = dict(os.environ, PATH=f"{stub}{os.pathsep}{os.environ['PATH']}", PY312=str(stub / "python3.12"),
               HARNESS_VENV=str(harness_venv), HOME=str(tmp_path / "fakehome"), **extra_env)
    return subprocess.run(["/bin/bash", str(root / "scripts" / "pre_merge_py312_check.sh")], env=env,
                          capture_output=True, text=True, timeout=60, cwd=root)


class TestHarnessVenvGuard:
    def test_foreign_nonempty_dir_without_bin_python_is_not_deleted(self, tmp_path):
        target = tmp_path / "not_a_venv"
        target.mkdir()
        (target / "precious.txt").write_text("мои файлы")
        res = _harness(tmp_path, target)
        assert (target / "precious.txt").read_text() == "мои файлы", res.stdout + res.stderr
        assert res.returncode == 2

    def test_rebuild_does_not_delete_foreign_dir_either(self, tmp_path):
        target = tmp_path / "not_a_venv2"
        target.mkdir()
        (target / "precious.txt").write_text("x")
        _harness(tmp_path, target, REBUILD="1")
        assert (target / "precious.txt").exists()

    def test_fake_home_is_refused(self, tmp_path):
        home = tmp_path / "fakehome"
        home.mkdir()
        (home / "keep.txt").write_text("k")
        res = _harness(tmp_path, home, REBUILD="1")
        assert (home / "keep.txt").exists(), res.stdout + res.stderr
        assert res.returncode == 2

    def test_project_root_is_refused(self, tmp_path):
        root = _harness_tree(tmp_path)
        res = _harness(tmp_path, root, REBUILD="1")
        assert (root / "scripts" / "pre_merge_py312_check.sh").exists(), res.stdout + res.stderr
        assert res.returncode == 2

    def test_symlink_harness_dir_is_refused(self, tmp_path):
        real = tmp_path / "real_dir"
        real.mkdir()
        (real / "keep.txt").write_text("k")
        link = tmp_path / "link_venv"
        link.symlink_to(real)
        _harness(tmp_path, link, REBUILD="1")
        assert (real / "keep.txt").exists()

    def test_real_venv_dir_is_still_rebuilt(self, tmp_path):
        venv = tmp_path / "old_venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (venv / "stale.txt").write_text("старое содержимое")
        _harness(tmp_path, venv)
        assert not (venv / "stale.txt").exists(), "настоящий venv должен пересоздаваться"

    def test_missing_dir_is_created_path(self, tmp_path):
        res = _harness(tmp_path, tmp_path / "does_not_exist_yet")
        # venv-сборка заглушкой падает (код 2), но отказ по валидации здесь неуместен
        assert "refus" not in res.stderr.lower()


# ── W6-B V13 update_agent: агент не останавливается до успешной сборки ────


def _update_agent_tree(tmp_path: Path, swift_ok: bool):
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "update_agent.command", root / "scripts")
    (root / "scripts" / "agent_signing_identity.sh").write_text(
        'resolve_agent_signing_identity() { echo "TEST-IDENTITY"; }\n')
    _exe(root / ".venv_krab_ear" / "bin" / "python", "exit 0\n")
    (root / "native" / "runtime").mkdir(parents=True)
    (root / "native" / "runtime" / "KrabEarAgent").write_text("OLD-RUNTIME")
    macos = root / "Krab Ear.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "KrabEarAgent").write_text("OLD-BUNDLE")
    (root / "native" / "KrabEarAgent").mkdir(parents=True)
    ctx = _stubs(tmp_path)
    stub = tmp_path / "stubbin"
    build_out = f"{root}/native/KrabEarAgent/.build/release"
    build_cmd = (f'mkdir -p "{build_out}"; echo NEW-BUILD > "{build_out}/KrabEarAgent"; '
                 f'chmod +x "{build_out}/KrabEarAgent"; exit 0') if swift_ok else "exit 1"
    _exe(stub / "swift", 'echo "swift $*" >> "$STUB_LOG"\n' + build_cmd + "\n")
    _exe(stub / "pgrep", "exit 1\n")
    _exe(stub / "codesign", "exit 0\n")
    _exe(stub / "open", 'echo "open $*" >> "$STUB_LOG"\nexit 0\n')
    return root, ctx


@needs_zsh
class TestUpdateAgentOrdering:
    def _run(self, root, ctx):
        return subprocess.run([ZSH, str(root / "scripts" / "update_agent.command")], env=ctx["env"],
                              capture_output=True, text=True, timeout=60)

    def test_failed_build_leaves_running_agent_and_binaries_untouched(self, tmp_path):
        root, ctx = _update_agent_tree(tmp_path, swift_ok=False)
        res = self._run(root, ctx)
        assert res.returncode != 0
        assert not any(c.startswith("pkill") for c in _calls(ctx)), _calls(ctx)
        assert (root / "native" / "runtime" / "KrabEarAgent").read_text() == "OLD-RUNTIME"
        assert (root / "Krab Ear.app" / "Contents" / "MacOS" / "KrabEarAgent").read_text() == "OLD-BUNDLE"

    def test_successful_build_precedes_stopping_the_agent(self, tmp_path):
        root, ctx = _update_agent_tree(tmp_path, swift_ok=True)
        res = self._run(root, ctx)
        assert res.returncode == 0, res.stdout + res.stderr
        calls = _calls(ctx)
        build_at = next(i for i, c in enumerate(calls) if c.startswith("swift"))
        stop_at = next(i for i, c in enumerate(calls) if c.startswith("pkill"))
        assert build_at < stop_at, calls
        assert (root / "Krab Ear.app" / "Contents" / "MacOS" / "KrabEarAgent").read_text().strip() == "NEW-BUILD"


# ═══ Ревью ChatGPT 22.11 (F1–F11) ═════════════════════════════════════════════


# F1: ignored-файлы не теряются при удалении worktree
class TestCleanupKeepsIgnoredWork:
    def _ignored_worktree(self, repo: Path, name: str, ignored_name: str, content: str) -> Path:
        (repo / ".gitignore").write_text("secret-data/\n__pycache__/\n.pytest_cache/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore rules")
        _git(repo, "push", "origin", "codex/krab-ear-v2")
        wt = _add_merged_worktree(repo, name)
        target = wt / ignored_name
        target.mkdir(parents=True)
        (target / "recording.wav").write_text(content)
        return wt

    def test_ignored_valuable_files_keep_worktree(self, repo):
        wt = self._ignored_worktree(repo, "has-ignored", "secret-data", "ценная запись")
        res = _cleanup(repo)
        assert (wt / "secret-data" / "recording.wav").read_text() == "ценная запись", res.stdout
        assert "KEEP (ignored" in res.stdout

    def test_only_regenerable_caches_do_not_block_cleanup(self, repo):
        wt = self._ignored_worktree(repo, "caches-only", "__pycache__", "pyc")
        res = _cleanup(repo)
        assert not wt.exists(), res.stdout + res.stderr


# F2/F8/F9: границы файлового отбора бэкапа
@needs_zsh
class TestBackupSelection:
    def _tree(self, tmp_path: Path) -> Path:
        root = _backup_tree(tmp_path)
        return root

    def test_untracked_secret_like_file_never_archived(self, tmp_path):
        root = self._tree(tmp_path)
        (root / "KrabEar" / "api-credentials.json").write_text(f"token={SECRET}")
        (root / "KrabEar" / "service.pem").write_text(SECRET)
        archive = _run_backup(root)
        with tarfile.open(archive) as tar:
            blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
        assert SECRET.encode() not in blob

    def test_tracked_secret_like_file_is_filtered(self, tmp_path):
        root = self._tree(tmp_path)
        (root / "KrabEar" / "prod-secret.key").write_text(SECRET)
        _git(root, "add", "-f", "KrabEar/prod-secret.key")
        _git(root, "commit", "-q", "-m", "oops tracked key")
        archive = _run_backup(root)
        with tarfile.open(archive) as tar:
            assert not any("prod-secret.key" in n for n in tar.getnames())

    def test_submodule_directory_contents_are_not_archived(self, tmp_path):
        root = self._tree(tmp_path)
        sub_src = tmp_path / "subsrc"
        sub_src.mkdir()
        _git(sub_src, "init", "-q", "-b", "main")
        (sub_src / "lib.py").write_text("x = 1\n")
        (sub_src / ".env").write_text(f"KEY={SECRET}")
        (sub_src / ".gitignore").write_text(".env\n")
        _git(sub_src, "add", "lib.py", ".gitignore")
        _git(sub_src, "commit", "-q", "-m", "sub")
        env = dict(os.environ, GIT_ALLOW_PROTOCOL="file")
        subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", str(sub_src), "native/vendored"],
                       cwd=root, env=env, check=True, capture_output=True)
        archive = _run_backup(root)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())
        assert SECRET.encode() not in blob
        assert not any("native/vendored" in n for n in names), "содержимое подмодуля попало в архив"

    def test_tracked_dangling_symlink_survives(self, tmp_path):
        root = self._tree(tmp_path)
        (root / "KrabEar" / "link_to_missing").symlink_to("does-not-exist.txt")
        _git(root, "add", "KrabEar/link_to_missing")
        _git(root, "commit", "-q", "-m", "dangling")
        archive = _run_backup(root)
        with tarfile.open(archive) as tar:
            members = {m.name.lstrip("./"): m for m in tar.getmembers()}
        assert "KrabEar/link_to_missing" in members
        assert members["KrabEar/link_to_missing"].issym()


# F3: гейт REST-миграции валидирует JSON, а не подстроки
@pytest.mark.parametrize(
    "rec,meet",
    [
        ('not-json "is_recording": false', '{"ok": true, "result": {"active": false}}'),
        ('{"id":"1","ok":true,"result":{"is_recording":false}}', 'not-json "active": false'),
        ('{"id":"1","ok":false,"result":{"is_recording":false}}', '{"id":"1","ok":true,"result":{"active":false}}'),
        ('{"id":"1","ok":true,"result":{"is_recording":false}}', '{"id":"1","ok":false,"result":{"active":false}}'),
        ('{"id":"1","ok":true,"result":{"is_recording":"false"}}', '{"id":"1","ok":true,"result":{"active":false}}'),
    ],
)
def test_migrate_rest_gate_rejects_malformed_idle_proofs(tmp_path, rec, meet):
    with tempfile.TemporaryDirectory(prefix="ke-mig2-", dir="/tmp") as short:
        home = Path(short)
        sock_dir = home / "Library" / "Application Support" / "KrabEar"
        sock_dir.mkdir(parents=True)
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        ctx = _stubs(tmp_path, "exit 0\n")
        env = dict(ctx["env"], HOME=str(home))
        server = _MethodServer(str(sock_dir / "krabear.sock"), {"get_recording_state": rec, "get_meeting_live_state": meet})
        try:
            res = subprocess.run(["/bin/bash", str(SCRIPTS / "migrate_to_inprocess_rest.command")], env=env,
                                 capture_output=True, text=True, timeout=30)
        finally:
            server.close()
    assert res.returncode == 1, res.stdout + res.stderr
    assert not any("bootout" in c or "bootstrap" in c for c in _calls(ctx)), _calls(ctx)


class _MethodServer(_IpcServer):
    """Unix-сокет, отвечающий по методу запроса заданными сырыми строками."""

    def __init__(self, path: str, replies: dict) -> None:
        self.replies = replies
        super().__init__(path, None)

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
                    data = conn.recv(4096)
                    method = json.loads(data).get("method")
                    raw = self.replies.get(method)
                    if raw is not None:
                        conn.sendall((raw + "\n").encode())
                except (OSError, ValueError):
                    pass


# F7: «кэш» без весов не считается HIT
class TestPrefetchWeights:
    def test_config_only_snapshot_is_a_miss(self, tmp_path):
        snap = tmp_path / "hub" / "models--mlx-community--whisper-large" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        assert _is_cached(tmp_path, "mlx-community/whisper-large") == "MISS"

    @pytest.mark.parametrize("weights", ["weights.npz", "model.safetensors"])
    def test_snapshot_with_weights_is_a_hit(self, tmp_path, weights):
        snap = tmp_path / "hub" / "models--mlx-community--whisper-large" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / weights).write_text("w")
        assert _is_cached(tmp_path, "mlx-community/whisper-large") == "HIT"

    def test_dangling_weights_symlink_is_a_miss(self, tmp_path):
        snap = tmp_path / "hub" / "models--mlx-community--whisper-large" / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "weights.npz").symlink_to("../../blobs/missing")
        assert _is_cached(tmp_path, "mlx-community/whisper-large") == "MISS"


# F10: неудачное профилирование возвращает прежнее значение launchctl env
@needs_zsh
class TestProfileRestoresEnv:
    def _ctx(self, tmp_path: Path, previous: str | None):
        ctx = _profile_ctx(tmp_path, gate_rc=0)
        # второй вызов safe_backend_restart (без флагов) падает; первый (--check-only) проходит
        _exe(tmp_path / "scripts" / "safe_backend_restart.command",
             'echo "$*" >> "$STUB_LOG.gate"\n[ "$1" = "--check-only" ] && exit 0\nexit 1\n')
        getenv = f'echo "{previous}"' if previous is not None else "true"
        _exe(tmp_path / "stubbin" / "launchctl",
             'printf "launchctl %s\\n" "$*" >> "$STUB_LOG"\n'
             f'case "$1" in getenv) {getenv};; esac\nexit 0\n')
        return ctx

    @pytest.mark.parametrize("previous", ["0", "1", "custom"])
    def test_previous_value_is_restored(self, tmp_path, previous):
        ctx = self._ctx(tmp_path, previous)
        res = _run_profile(tmp_path, ctx)
        assert res.returncode != 0
        calls = _calls(ctx)
        assert f"launchctl setenv KRAB_EAR_TRACE_GIGAAM_MEM {previous}" == calls[-1], calls

    def test_previously_unset_is_unset_again(self, tmp_path):
        ctx = self._ctx(tmp_path, None)
        res = _run_profile(tmp_path, ctx)
        assert res.returncode != 0
        assert _calls(ctx)[-1] == "launchctl unsetenv KRAB_EAR_TRACE_GIGAAM_MEM", _calls(ctx)


# F11: TCC-аудит не меняет права существующего каталога
def test_tcc_audit_does_not_chmod_existing_output_dir(tmp_path):
    out_dir = tmp_path / "shared"
    out_dir.mkdir()
    out_dir.chmod(0o755)
    _run_tcc(tmp_path, "com.antigravity.krab-ear", out_dir / "audit.json")
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o755
    assert (out_dir / "audit.json").exists()


# ═══ Пачка 4: G2-KE02a-F02/F05/F10, KEG2-12 ═══════════════════════════════════

import time  # noqa: E402

_STUB_PY = '''#!/usr/bin/env python3
import os, socket, sys, time
args = sys.argv[1:]
log = os.environ["STUB_LOG"]
def note(text):
    with open(log, "a") as fh:
        fh.write(text + "\\n")
if args and args[0].endswith("main.py"):
    data_dir = args[args.index("--data-dir") + 1]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(os.path.join(data_dir, "krabear.sock"))
    s.listen(1)
    time.sleep(60)
elif args and args[0].endswith("e2e_ipc_smoke.py"):
    note("phase:ipc")
    time.sleep(float(os.environ.get("FAKE_IPC_SLEEP", "0")))
    sys.exit(int(os.environ.get("FAKE_IPC_RC", "0")))
elif args and args[0].endswith("e2e_privacy_gates.py"):
    note("phase:gates")
    sys.exit(int(os.environ.get("FAKE_PG_RC", "0")))
'''


def _smoke_tree(tmp_path: Path) -> Path:
    root = tmp_path / "smoke_proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "run_e2e_smokes.command", root / "scripts")
    (root / "KrabEar").mkdir()
    (root / "KrabEar" / "main.py").write_text("")
    py = root / ".venv_krab_ear" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(_STUB_PY)
    py.chmod(0o755)
    return root


def _run_smokes(tmp_path: Path, **env_extra: str) -> subprocess.CompletedProcess:
    root = _smoke_tree(tmp_path)
    ctx = _stubs(tmp_path)
    env = dict(ctx["env"], **env_extra)
    return subprocess.run(["/bin/bash", str(root / "scripts" / "run_e2e_smokes.command")], env=env,
                          capture_output=True, text=True, timeout=120)


class TestE2eSmokesRunner:
    def test_failure_log_goes_to_a_unique_private_file_not_a_fixed_tmp_name(self, tmp_path):
        paths = []
        for i in range(2):
            run_dir = tmp_path / f"run{i}"
            run_dir.mkdir()
            res = _run_smokes(run_dir, FAKE_IPC_RC="1")
            assert res.returncode != 0
            line = next((ln for ln in res.stdout.splitlines() if "backend log copied to" in ln), None)
            assert line, res.stdout + res.stderr
            paths.append(line.split("copied to ", 1)[1].rstrip(")").strip())
        assert paths[0] != paths[1], "оба сбоя пишут в один фиксированный путь в /tmp"
        for p in paths:
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
            os.unlink(p)

    def test_sigint_stops_the_run_instead_of_continuing_to_next_phase(self, tmp_path):
        root = _smoke_tree(tmp_path)
        ctx = _stubs(tmp_path)
        env = dict(ctx["env"], FAKE_IPC_SLEEP="6")
        proc = subprocess.Popen(["/bin/bash", str(root / "scripts" / "run_e2e_smokes.command")], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline and "phase:ipc" not in (ctx["log"].read_text() if ctx["log"].exists() else ""):
            time.sleep(0.2)
        assert "phase:ipc" in ctx["log"].read_text(), "первая фаза не стартовала"
        proc.send_signal(__import__("signal").SIGINT)
        out, _ = proc.communicate(timeout=60)
        assert "phase:gates" not in ctx["log"].read_text(), "после SIGINT скрипт пошёл в следующую фазу: " + out
        assert proc.returncode != 0


# ── F05 run_soak_backend: «latest» публикуется атомарно и согласованно ──────


def _soak_tree(tmp_path: Path, write_md: bool) -> Path:
    root = tmp_path / "soak_proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "run_soak_backend.command", root / "scripts")
    (root / "KrabEar" / "tests").mkdir(parents=True)
    (root / "KrabEar" / "tests" / "soak_backend.py").write_text("")
    venv_bin = root / ".venv_krab_ear" / "bin"
    venv_bin.mkdir(parents=True)
    stub = root / "stubpy"
    stub.mkdir()
    md_line = 'open(report[:-5] + ".md", "w").write("NEW-MD")' if write_md else "pass"
    body = (
        '#!/usr/bin/env python3\n'
        'import sys\n'
        'report = sys.argv[sys.argv.index("--report") + 1]\n'
        'open(report, "w").write("NEW-JSON")\n'
        f'{md_line}\n'
    )
    (stub / "python").write_text(body)
    (stub / "python").chmod(0o755)
    (venv_bin / "activate").write_text(f'export PATH="{stub}:$PATH"\n')
    (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
    (venv_bin / "python").chmod(0o755)
    reports = root / "docs" / "reports"
    reports.mkdir(parents=True)
    (reports / "soak_backend_latest.json").write_text("OLD-JSON")
    (reports / "soak_backend_latest.md").write_text("OLD-MD")
    return root


@needs_zsh
class TestSoakPublish:
    def _run(self, root):
        return subprocess.run([ZSH, str(root / "scripts" / "run_soak_backend.command"), "3"], capture_output=True,
                              text=True, timeout=60)

    def test_missing_markdown_leaves_previous_latest_pair_untouched(self, tmp_path):
        root = _soak_tree(tmp_path, write_md=False)
        res = self._run(root)
        assert res.returncode != 0
        reports = root / "docs" / "reports"
        assert (reports / "soak_backend_latest.json").read_text() == "OLD-JSON", "latest.json обновлён без пары .md"
        assert (reports / "soak_backend_latest.md").read_text() == "OLD-MD"

    def test_success_publishes_both(self, tmp_path):
        root = _soak_tree(tmp_path, write_md=True)
        res = self._run(root)
        assert res.returncode == 0, res.stdout + res.stderr
        reports = root / "docs" / "reports"
        assert (reports / "soak_backend_latest.json").read_text() == "NEW-JSON"
        assert (reports / "soak_backend_latest.md").read_text() == "NEW-MD"
        assert not list(reports.glob(".latest*")), "временные файлы публикации остались"


# ── KEG2-12 audit_tcc_permissions: статус текущего приложения, а не максимум по всем клиентам ──


def _tcc_rows(tmp_path: Path, rows: list[tuple[str, str, int]]) -> Path:
    home = tmp_path / "home"
    db_dir = home / "Library" / "Application Support" / "com.apple.TCC"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(db_dir / "TCC.db")
    con.execute("CREATE TABLE access (service TEXT, client TEXT, client_type INT, auth_value INT, last_modified INT)")
    for service, client, auth in rows:
        con.execute("INSERT INTO access VALUES (?, ?, 0, ?, 1700000000)", (service, client, auth))
    con.commit()
    con.close()
    return home


def _tcc_summary(tmp_path: Path, rows) -> dict:
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "audit_tcc_permissions.command", root / "scripts")
    home = _tcc_rows(tmp_path, rows)
    out = tmp_path / "audit.json"
    env = dict(os.environ, HOME=str(home), KRAB_TCC_AUDIT_OUT=str(out))
    subprocess.run(["/bin/bash", str(root / "scripts" / "audit_tcc_permissions.command")], env=env,
                   capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.read_text())["key_services"]


class TestTccCurrentClientStatus:
    def test_current_bundle_denial_is_not_hidden_by_old_path_grant(self, tmp_path):
        summary = _tcc_summary(tmp_path, [
            ("kTCCServiceMicrophone", "com.antigravity.krab-ear", 0),
            ("kTCCServiceMicrophone", "/old/build/KrabEarAgent", 2),
        ])
        assert summary["Microphone"] == "DENIED", summary

    def test_current_bundle_grant_wins_over_old_denial(self, tmp_path):
        summary = _tcc_summary(tmp_path, [
            ("kTCCServiceMicrophone", "com.antigravity.krab-ear", 2),
            ("kTCCServiceMicrophone", "/old/build/KrabEarAgent", 0),
        ])
        assert summary["Microphone"] == "GRANTED", summary

    def test_only_path_based_grant_is_reported_as_such(self, tmp_path):
        summary = _tcc_summary(tmp_path, [("kTCCServiceMicrophone", "/old/build/KrabEarAgent", 2)])
        assert "GRANTED" in summary["Microphone"] and "path" in summary["Microphone"].lower(), summary


# ═══ Пачка 5: KEG2-11 install_agent_launchagent ═══════════════════════════════

PLUTIL = shutil.which("plutil")


def _agent_installer(tmp_path: Path, *, broken_template: bool = False, job_gets_pid: bool = True):
    root = tmp_path / "agent_proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "install_agent_launchagent.command", root / "scripts")
    tpl_dir = root / "KrabEar" / "launchagents"
    tpl_dir.mkdir(parents=True)
    template = (ROOT / "KrabEar" / "launchagents" / "ai.krab.ear.agent.plist.template").read_text()
    (tpl_dir / "ai.krab.ear.agent.plist.template").write_text(template[: len(template) // 2] if broken_template
                                                              else template)
    binary = root / "Krab Ear.app" / "Contents" / "MacOS" / "KrabEarAgent"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    pid_line = 'echo "pid = 4242"' if job_gets_pid else 'echo "state = spawn scheduled"'
    ctx = _stubs(
        tmp_path,
        'case "$1" in\n'
        f'  print) if [ -e "$STUB_LOG.boot" ]; then {pid_line}; fi; exit 0;;\n'
        '  bootout) rm -f "$STUB_LOG.boot";;\n'
        '  bootstrap) touch "$STUB_LOG.boot";;\n'
        'esac\nexit 0\n',
    )
    # любой подходящий процесс «найден» — старая проверка через pgrep это принимала за запуск
    _exe(tmp_path / "stubbin" / "pgrep", "exit 0\n")
    target = ctx["home"] / "Library" / "LaunchAgents" / "ai.krab.ear.agent.plist"
    target.write_text("OLD-PLIST")
    res = subprocess.run(["/bin/bash", str(root / "scripts" / "install_agent_launchagent.command")],
                         env=ctx["env"], capture_output=True, text=True, timeout=60)
    return res, ctx, target


@pytest.mark.skipif(PLUTIL is None, reason="plutil доступен только на macOS")
class TestInstallAgentLaunchagent:
    def test_invalid_plist_leaves_loaded_job_and_old_file_untouched(self, tmp_path):
        res, ctx, target = _agent_installer(tmp_path, broken_template=True)
        assert res.returncode != 0, res.stdout + res.stderr
        assert target.read_text() == "OLD-PLIST", "рабочий plist затёрт невалидным"
        assert not any("bootout" in c for c in _calls(ctx)), "юнит выгружен до проверки нового plist"

    def test_any_matching_process_is_not_proof_of_launchd_start(self, tmp_path):
        res, _, _ = _agent_installer(tmp_path, job_gets_pid=False)
        assert res.returncode != 0, "pgrep по любому процессу выдан за запуск launchd-юнита"
        assert "agent запущен через launchd" not in res.stdout

    def test_job_with_pid_succeeds_and_installs_new_plist(self, tmp_path):
        res, ctx, target = _agent_installer(tmp_path)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "OLD-PLIST" not in target.read_text()
        assert str(tmp_path) in target.read_text()
        assert not list(target.parent.glob(".ai.krab.ear.agent*")), "остались временные файлы"


# ── G2-KE02a-F08 run_release_checklist: git-состояние и в linked worktree ─


def _git_state(root: Path) -> str:
    body = _function_source("run_release_checklist.command", "git_worktree_state")
    res = subprocess.run([ZSH, "-c", f'ROOT_DIR="{root}"\n{body}\ngit_worktree_state'], capture_output=True,
                         text=True, timeout=30)
    return res.stdout.strip()


@needs_zsh
class TestReleaseChecklistGitState:
    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "main"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("a")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        return repo

    def test_clean_clone(self, tmp_path):
        assert _git_state(self._repo(tmp_path)) == "clean"

    def test_dirty_tracked_change(self, tmp_path):
        repo = self._repo(tmp_path)
        (repo / "a.txt").write_text("changed")
        assert _git_state(repo) == "dirty"

    def test_linked_worktree_is_checked_not_skipped(self, tmp_path):
        repo = self._repo(tmp_path)
        wt = tmp_path / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
        assert (wt / ".git").is_file(), "предусловие: .git в linked worktree — файл"
        assert _git_state(wt) == "clean"
        (wt / "a.txt").write_text("changed in worktree")
        assert _git_state(wt) == "dirty", "изменения в linked worktree не замечены"

    def test_non_git_directory_is_unknown_not_clean(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _git_state(plain) == "unknown"


# ── G2-KE02a-F06/F09 validate_c1_mps_fix ────────────────────────────────────

ENV_NAME = "KRAB_EAR_DISABLE_MPS_POOL_FREE"


def _c1_run(tmp_path: Path, *, previous: str | None, restart_rc: int):
    root = tmp_path / "c1_proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "validate_c1_mps_fix.command", root / "scripts")
    _exe(root / "scripts" / "safe_backend_restart.command", f'echo "$*" >> "$STUB_LOG.restart"\nexit {restart_rc}\n')
    getenv = f'echo "{previous}"' if previous is not None else "true"
    ctx = _stubs(tmp_path, f'case "$1" in getenv) {getenv};; esac\nexit 0\n')
    _exe(tmp_path / "stubbin" / "ps", "exit 0\n")  # gigaam_worker нет → RSS 0 во всех замерах
    env = dict(ctx["env"], CYCLES="1", SLEEP_BETWEEN="0", SOCK=str(tmp_path / "no.sock"))
    res = subprocess.run([ZSH, str(root / "scripts" / "validate_c1_mps_fix.command")], env=env,
                         capture_output=True, text=True, timeout=120)
    return res, ctx, root


@needs_zsh
@pytest.mark.skipif(shutil.which("bc") is None, reason="нужен bc")
class TestValidateC1:
    def test_env_is_restored_when_restart_refused(self, tmp_path):
        res, ctx, _ = _c1_run(tmp_path, previous="keepme", restart_rc=1)
        assert res.returncode != 0
        assert _calls(ctx)[-1] == f"launchctl setenv {ENV_NAME} keepme", _calls(ctx)

    def test_previously_unset_env_is_unset_again(self, tmp_path):
        res, ctx, _ = _c1_run(tmp_path, previous=None, restart_rc=1)
        assert res.returncode != 0
        assert _calls(ctx)[-1] == f"launchctl unsetenv {ENV_NAME}", _calls(ctx)

    def test_restart_goes_through_the_safe_script_not_blind_kickstart(self, tmp_path):
        res, ctx, _ = _c1_run(tmp_path, previous=None, restart_rc=0)
        assert Path(str(ctx["log"]) + ".restart").exists(), "рестарт не через safe_backend_restart"
        assert not any("kickstart" in c for c in _calls(ctx)), _calls(ctx)

    def test_zero_rss_and_failed_ipc_give_inconclusive_verdict_not_a_causal_claim(self, tmp_path):
        res, ctx, root = _c1_run(tmp_path, previous=None, restart_rc=0)
        assert res.returncode == 0, res.stdout + res.stderr
        report = next((root / "docs" / "measurements").glob("c1-validation-*.md")).read_text()
        assert "INCONCLUSIVE" in report, report[-600:]
        assert "H1 hypothesis confirmed" not in report
        assert "H1 not primary" not in report


# ── Codex review #2224: нестандартный BACKEND_LABEL — отказ до любых изменений ──


@needs_zsh
@pytest.mark.skipif(shutil.which("bc") is None, reason="нужен bc")
def test_validate_c1_rejects_nondefault_backend_label_before_touching_anything(tmp_path):
    root = tmp_path / "c1_label_proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "validate_c1_mps_fix.command", root / "scripts")
    _exe(root / "scripts" / "safe_backend_restart.command", 'echo "$*" >> "$STUB_LOG.restart"\nexit 0\n')
    ctx = _stubs(tmp_path)
    env = dict(ctx["env"], BACKEND_LABEL="com.example.other-backend", CYCLES="1", SLEEP_BETWEEN="0")
    res = subprocess.run([ZSH, str(root / "scripts" / "validate_c1_mps_fix.command")], env=env, capture_output=True,
                         text=True, timeout=60)
    assert res.returncode != 0, res.stdout + res.stderr
    assert "BACKEND_LABEL" in (res.stdout + res.stderr)
    assert not Path(str(ctx["log"]) + ".restart").exists(), "рестарт запущен для чужого label"
    assert not any("setenv" in c for c in _calls(ctx)), "launchctl env изменён до отказа"
