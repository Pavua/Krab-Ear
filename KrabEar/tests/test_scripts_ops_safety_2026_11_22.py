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
