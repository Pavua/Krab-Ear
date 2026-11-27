"""KEG2-18 / KEG2-20: остатки G2-Krab-Ear-01 (скрипты).

* KEG2-18 ``cleanup_worktree_builds``: после канонической проверки ``rm -rf`` снова
  шёл по АБСОЛЮТНОМУ пути — подмена предка симлинком между проверкой и удалением
  уводила удаление наружу. Теперь удаление идёт из закреплённого ``cd`` и только
  для того же inode. Плюс ``stat`` берётся абсолютным ``/usr/bin/stat``: GNU stat в
  PATH молча давал ``mtime=0`` → свежий ``.build`` считался старым и удалялся.
* KEG2-20 ``bootstrap_backend``: ставил плавающую ветку без проверки. Теперь —
  ровно закреплённый коммит, сверка git HEAD / pax-комментария tarball до установки.

Все запуски — на копиях/функциях в ``tmp_path``; реальные worktree, launchd,
сеть и ``~/KrabEar`` не затрагиваются.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
# KE_SIMULATE_CI=1 имитирует Linux-раннер без zsh.
ZSH = None if os.environ.get("KE_SIMULATE_CI") else shutil.which("zsh")
needs_zsh = pytest.mark.skipif(ZSH is None, reason="zsh не установлен (shebang #!/bin/zsh)")
needs_bsd_stat = pytest.mark.skipif(sys.platform != "darwin", reason="скрипт использует BSD /usr/bin/stat")
GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="нет git")
BASH = "/bin/bash"


def _function_source(script: str, name: str) -> str:
    text = (SCRIPTS / script).read_text()
    match = re.search(rf"^{name}\(\) \{{.*?\n\}}\n", text, re.S | re.M)
    assert match, f"{name}() не найдена в {script}"
    return match.group()


# ── KEG2-18: удаление из закреплённого cwd и только того же inode ──────────────


def _build_tree(tmp_path: Path) -> Path:
    build = tmp_path / "worktrees" / "wt1" / "native" / "KrabEarAgent" / ".build"
    (build / "release").mkdir(parents=True)
    (build / "release" / "artifact").write_text("a")
    return build


def _identity(path: Path) -> str:
    st = os.lstat(path)
    return f"{st.st_dev}:{st.st_ino}"


def _remove_pinned(real: Path, want_id: str) -> subprocess.CompletedProcess:
    body = (_function_source("cleanup_worktree_builds.command", "build_dir_identity")
            + _function_source("cleanup_worktree_builds.command", "remove_build_dir_pinned"))
    code = f'{body}\nremove_build_dir_pinned "{real}" "{want_id}"'
    return subprocess.run([ZSH, "-c", code], capture_output=True, text=True, timeout=30)


@needs_zsh
@needs_bsd_stat
class TestRemoveBuildDirPinned:
    def test_same_inode_is_removed(self, tmp_path):
        build = _build_tree(tmp_path)
        res = _remove_pinned(build.resolve(), _identity(build))
        assert res.returncode == 0, res.stderr
        assert not build.exists()

    def test_directory_replaced_after_discovery_is_refused(self, tmp_path):
        build = _build_tree(tmp_path)
        old_id = _identity(build)
        # держим старый каталог живым, иначе ФС может отдать новому тот же inode
        parked = tmp_path / "parked"
        build.rename(parked)
        build.mkdir()
        (build / "fresh").write_text("new build of someone else")
        res = _remove_pinned(build.resolve(), old_id)
        assert res.returncode == 3, res.stdout + res.stderr
        assert (build / "fresh").exists()

    def test_build_swapped_to_symlink_is_refused(self, tmp_path):
        build = _build_tree(tmp_path)
        real = build.resolve()
        old_id = _identity(build)
        target = tmp_path / "precious"
        target.mkdir()
        (target / "keep").write_text("k")
        build.rename(tmp_path / "parked")
        build.symlink_to(target)
        res = _remove_pinned(real, old_id)
        assert res.returncode == 3, res.stdout + res.stderr
        assert (target / "keep").exists()
        assert build.is_symlink()

    def test_ancestor_swapped_to_symlink_is_refused(self, tmp_path):
        build = _build_tree(tmp_path)
        real = build.resolve()
        old_id = _identity(build)
        outside = tmp_path / "outside" / "KrabEarAgent" / ".build"
        outside.mkdir(parents=True)
        (outside / "precious").write_text("p")
        native = tmp_path / "worktrees" / "wt1" / "native"
        native.rename(tmp_path / "parked_native")
        native.symlink_to(tmp_path / "outside")
        res = _remove_pinned(real, old_id)
        assert res.returncode == 3, res.stdout + res.stderr
        assert (outside / "precious").exists()

    def test_ancestor_swapped_between_check_and_rm_does_not_redirect(self, tmp_path):
        """Сама гонка: предок подменяется ПОСЛЕ всех проверок, прямо перед rm."""
        build = _build_tree(tmp_path)
        real = build.resolve()
        old_id = _identity(build)
        outside_root = tmp_path / "outside"
        outside = outside_root / "KrabEarAgent" / ".build"
        outside.mkdir(parents=True)
        (outside / "precious").write_text("p")
        native = tmp_path / "worktrees" / "wt1" / "native"
        parked = tmp_path / "parked_native"
        # Последняя проверка перед rm — сверка inode; подмена — побочным эффектом этой сверки.
        racer = ('build_dir_identity() {\n  local id\n  id="$(/usr/bin/stat -f \'%d:%i\' "$1")"\n'
                 f'  mv "{native}" "{parked}" && ln -s "{outside_root}" "{native}"\n'
                 '  printf \'%s\\n\' "$id"\n}\n')
        code = (_function_source("cleanup_worktree_builds.command", "remove_build_dir_pinned") + racer
                + f'remove_build_dir_pinned "{real}" "{old_id}"')
        res = subprocess.run([ZSH, "-c", code], capture_output=True, text=True, timeout=30)
        assert native.is_symlink(), "гонка не воспроизведена"
        assert (outside / "precious").exists(), "rm ушёл по подменённому предку наружу"
        assert res.returncode == 0, res.stderr
        assert not (parked / "KrabEarAgent" / ".build").exists(), "удалён должен быть проверенный каталог"

    def test_unknown_identity_never_deletes(self, tmp_path):
        build = _build_tree(tmp_path)
        res = _remove_pinned(build.resolve(), "")
        assert res.returncode == 3
        assert build.exists()


def _cleanup_project(tmp_path: Path, age_days: int) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPTS / "cleanup_worktree_builds.command", proj / "scripts")
    build = proj / ".claude" / "worktrees" / "wt1" / "native" / "KrabEarAgent" / ".build"
    build.mkdir(parents=True)
    (build / "obj").write_text("x")
    stamp = time.time() - age_days * 86400
    os.utime(build, (stamp, stamp))
    return proj, build


def _run_cleanup(proj: Path, tmp_path: Path, *args: str, fake_stat: bool = False) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path), "TERM": "dumb"}
    if fake_stat:
        # GNU stat из Homebrew: `stat -f "%m"` для него — другой режим, разбор ломается.
        fake = tmp_path / "gnubin"
        fake.mkdir(exist_ok=True)
        (fake / "stat").write_text("#!/bin/sh\necho 'stat: cannot read file system information' >&2\nexit 1\n")
        (fake / "stat").chmod(0o755)
        env["PATH"] = f"{fake}:{env['PATH']}"
    # cwd вне любого git-репозитория: скрипт берёт worktrees из своего каталога.
    return subprocess.run([ZSH, str(proj / "scripts" / "cleanup_worktree_builds.command"), *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


@needs_zsh
@needs_bsd_stat
class TestCleanupWorktreeBuildsScript:
    def test_old_build_is_removed_through_pinned_path(self, tmp_path):
        proj, build = _cleanup_project(tmp_path, age_days=90)
        res = _run_cleanup(proj, tmp_path, "--apply", "--force", "--days", "30")
        assert res.returncode == 0, res.stdout + res.stderr
        assert not build.exists(), res.stdout

    def test_gnu_stat_in_path_does_not_make_fresh_build_look_old(self, tmp_path):
        proj, build = _cleanup_project(tmp_path, age_days=0)
        res = _run_cleanup(proj, tmp_path, "--apply", "--force", "--days", "30", fake_stat=True)
        assert build.exists(), "свежий .build удалён: mtime прочитан через GNU stat как 0\n" + res.stdout
        assert res.returncode == 0, res.stdout + res.stderr


def test_cleanup_source_has_no_absolute_rm_and_no_path_stat():
    """Статическая страховка — работает и на Linux-раннере без zsh."""
    text = (SCRIPTS / "cleanup_worktree_builds.command").read_text()
    assert 'rm -rf "$real_build_dir"' not in text
    assert re.search(r"^\s*mtime=\$\(stat ", text, re.M) is None, "stat без абсолютного пути"

# ── KEG2-20: bootstrap ставит ровно закреплённый коммит ─────────────────────────


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run([GIT, *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


@pytest.fixture()
def src_repo(tmp_path):
    repo = tmp_path / "src"
    (repo / "KrabEar" / "backend").mkdir(parents=True)
    (repo / "KrabEar" / "backend" / "service.py").write_text("VERSION = 'A'\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "A")
    sha_a = _git(repo, "rev-parse", "HEAD")
    (repo / "KrabEar" / "backend" / "service.py").write_text("VERSION = 'B'\n")
    _git(repo, "commit", "-qam", "B")
    sha_b = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    return repo, sha_a, sha_b


_HARNESS_FUNCS = ("resolve_expected_commit", "fetch_pinned_git", "fetch_pinned_tarball", "install_pinned_code")


# Скрипт пускает curl только по https. Тестовые архивы лежат по file:// — обёртка
# дописывает разрешение file ПОСЛЕ аргументов скрипта (у curl последний --proto побеждает).
# Сам запрет http/file проверяет TestCurlHttpsOnly без обёртки.
_CURL_ALLOW_FILE = "curl() { command curl \"$@\" --proto '=file' --proto-redir '=file'; }\n"


def _bootstrap(tmp_path: Path, snippet: str, prelude_extra: str = "", **env_extra: str) -> subprocess.CompletedProcess:
    funcs = "".join(_function_source("bootstrap_backend.command", f) for f in _HARNESS_FUNCS)
    prelude = ("log() { echo \"LOG $*\"; }\nwarn() { echo \"WARN $*\"; }\n"
               f'PY="{sys.executable}"\nPINNED_COMMIT="${{TEST_STAMP:-}}"\n' + prelude_extra)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}
    env.update(env_extra)
    return subprocess.run([BASH, "-c", prelude + funcs + snippet], env=env, capture_output=True,
                          text=True, timeout=120)


def _install(tmp_path: Path, repo_url: str, sha: str) -> tuple[subprocess.CompletedProcess, Path]:
    dest = tmp_path / "home" / "KrabEar"
    res = _bootstrap(tmp_path, 'install_pinned_code', prelude_extra=_CURL_ALLOW_FILE,
                     REPO_URL=repo_url, EXPECTED_COMMIT=sha, INSTALL_DIR=str(dest))
    return res, dest


def _leftovers(tmp_path: Path) -> list[str]:
    parent = tmp_path / "home"
    return [p.name for p in parent.iterdir() if p.name.startswith(".krab_ear_bootstrap")] if parent.exists() else []


def _publish_tarball(tmp_path: Path, repo: Path, url_sha: str, content_sha: str) -> str:
    """Кладёт архив коммита content_sha по адресу коммита url_sha; возвращает REPO_URL без git."""
    archive_dir = tmp_path / "web" / "repo" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    out = archive_dir / f"{url_sha}.tar.gz"
    subprocess.run([GIT, "archive", "--format=tar.gz", f"--prefix=Krab-Ear-{content_sha}/", "-o", str(out),
                    content_sha], cwd=repo, check=True, timeout=60)
    return f"file://{tmp_path / 'web' / 'repo'}.git"


class TestResolveExpectedCommit:
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def test_env_commit_wins(self, tmp_path):
        res = _bootstrap(tmp_path, "resolve_expected_commit", KRAB_EAR_COMMIT=self.SHA,
                         TEST_STAMP="f" * 40, SCRIPT_DIR=str(tmp_path))
        assert res.returncode == 0 and res.stdout.strip() == self.SHA

    def test_stamp_used_when_env_empty(self, tmp_path):
        res = _bootstrap(tmp_path, "resolve_expected_commit", TEST_STAMP=self.SHA, SCRIPT_DIR=str(tmp_path))
        assert res.returncode == 0 and res.stdout.strip() == self.SHA

    @pytest.mark.parametrize("bad", ["", "abc123", "0123456789ABCDEF0123456789ABCDEF01234567",
                                     "codex/krab-ear-v2", "0123456789abcdef0123456789abcdef0123456",
                                     "0123456789abcdef0123456789abcdef01234567; rm -rf ~"])
    def test_no_valid_commit_refuses(self, tmp_path, bad):
        res = _bootstrap(tmp_path, "resolve_expected_commit", KRAB_EAR_COMMIT=bad, SCRIPT_DIR=str(tmp_path))
        assert res.returncode != 0, res.stdout

    @needs_git
    def test_checkout_head_used_when_run_from_repo(self, tmp_path, src_repo):
        repo, _sha_a, sha_b = src_repo
        (repo / "scripts").mkdir()
        res = _bootstrap(tmp_path, "resolve_expected_commit", SCRIPT_DIR=str(repo / "scripts"))
        assert res.returncode == 0 and res.stdout.strip() == sha_b


@needs_git
class TestInstallPinnedCode:
    def test_git_installs_exactly_the_pinned_commit(self, tmp_path, src_repo):
        repo, sha_a, _sha_b = src_repo
        res, dest = _install(tmp_path, f"file://{repo}", sha_a)
        assert res.returncode == 0, res.stdout + res.stderr
        assert (dest / "KrabEar" / "backend" / "service.py").read_text() == "VERSION = 'A'\n"
        assert _git(dest, "rev-parse", "HEAD") == sha_a
        assert _leftovers(tmp_path) == []

    def test_unknown_commit_fails_without_partial_install(self, tmp_path, src_repo):
        repo, _a, _b = src_repo
        res, dest = _install(tmp_path, f"file://{repo}", "f" * 40)
        assert res.returncode != 0, res.stdout
        assert not dest.exists()
        assert _leftovers(tmp_path) == []

    def test_tarball_of_pinned_commit_is_installed(self, tmp_path, src_repo):
        repo, sha_a, _b = src_repo
        url = _publish_tarball(tmp_path, repo, sha_a, sha_a)
        res, dest = _install(tmp_path, url, sha_a)
        assert res.returncode == 0, res.stdout + res.stderr
        assert (dest / "KrabEar" / "backend" / "service.py").read_text() == "VERSION = 'A'\n"
        assert _leftovers(tmp_path) == []

    def test_tarball_of_another_commit_is_refused(self, tmp_path, src_repo):
        repo, sha_a, sha_b = src_repo
        url = _publish_tarball(tmp_path, repo, sha_a, sha_b)  # по адресу A лежит B
        res, dest = _install(tmp_path, url, sha_a)
        assert res.returncode != 0, res.stdout
        assert "вместо" in res.stdout + res.stderr
        assert not dest.exists()
        assert _leftovers(tmp_path) == []

    def test_tarball_with_escaping_member_is_refused(self, tmp_path, src_repo):
        _repo, sha_a, _b = src_repo
        archive_dir = tmp_path / "web" / "repo" / "archive"
        archive_dir.mkdir(parents=True)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                          pax_headers={"comment": sha_a}) as tf:
            for name, data in ((f"Krab-Ear-{sha_a}/KrabEar/backend/service.py", b"x"),
                               (f"Krab-Ear-{sha_a}/../../escaped", b"evil")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        (archive_dir / f"{sha_a}.tar.gz").write_bytes(buf.getvalue())
        res, dest = _install(tmp_path, f"file://{tmp_path / 'web' / 'repo'}.git", sha_a)
        assert res.returncode != 0, res.stdout
        assert not dest.exists()
        assert list(tmp_path.rglob("escaped")) == []


def _publish_crafted(tmp_path: Path, sha: str, members: list[tarfile.TarInfo | tuple[str, bytes]]) -> str:
    """Собирает архив с pax comment = sha из произвольных членов; возвращает REPO_URL."""
    archive_dir = tmp_path / "web" / "repo" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": sha}) as tf:
        for item in members:
            if isinstance(item, tarfile.TarInfo):
                tf.addfile(item)
            else:
                name, data = item
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    (archive_dir / f"{sha}.tar.gz").write_bytes(buf.getvalue())
    return f"file://{tmp_path / 'web' / 'repo'}.git"


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


class TestTarballMemberTypes:
    SHA = "0123456789abcdef0123456789abcdef01234567"
    ROOT_DIR = f"Krab-Ear-{SHA}"
    SERVICE = (f"{ROOT_DIR}/KrabEar/backend/service.py", b"VERSION = 'T'\n")

    def _install_crafted(self, tmp_path: Path, *extra) -> tuple[subprocess.CompletedProcess, Path]:
        url = _publish_crafted(tmp_path, self.SHA, [self.SERVICE, *extra])
        # git-путь по этому адресу не сработает (нет репозитория) — ставится tarball.
        return _install(tmp_path, url, self.SHA)

    def test_internal_relative_symlink_is_kept(self, tmp_path):
        """Как Sparkle.framework в репо: относительная ссылка внутри корня допустима."""
        fw = f"{self.ROOT_DIR}/Krab Ear.app/Contents/Frameworks/Sparkle.framework"
        res, dest = self._install_crafted(
            tmp_path, (f"{fw}/Versions/B/Sparkle", b"bin"), _link(f"{fw}/Versions/Current", "B"),
            _link(f"{fw}/Sparkle", "Versions/Current/Sparkle"))
        assert res.returncode == 0, res.stdout + res.stderr
        link = dest / "Krab Ear.app/Contents/Frameworks/Sparkle.framework/Sparkle"
        assert link.is_symlink() and link.read_bytes() == b"bin"
        assert _leftovers(tmp_path) == []

    @pytest.mark.parametrize("member", [
        pytest.param(lambda r: _link(f"{r}/KrabEar/hard.py", f"{r}/KrabEar/backend/service.py", tarfile.LNKTYPE),
                     id="hardlink"),
        pytest.param(lambda r: _link(f"{r}/out", "../../outside"), id="symlink-escapes-root"),
        pytest.param(lambda r: _link(f"{r}/abs", "/etc"), id="symlink-absolute"),
        pytest.param(lambda r: _link(f"{r}/dev", "", tarfile.CHRTYPE), id="char-device"),
        pytest.param(lambda r: _link(f"{r}/fifo", "", tarfile.FIFOTYPE), id="fifo"),
    ])
    def test_forbidden_member_refuses_whole_install(self, tmp_path, member):
        res, dest = self._install_crafted(tmp_path, member(self.ROOT_DIR))
        assert res.returncode != 0, res.stdout
        assert "tarball:" in res.stdout + res.stderr
        assert not os.path.lexists(dest)
        assert _leftovers(tmp_path) == []

    def test_member_written_through_internal_symlink_is_refused(self, tmp_path):
        res, dest = self._install_crafted(
            tmp_path, (f"{self.ROOT_DIR}/sub/keep.txt", b"k"), _link(f"{self.ROOT_DIR}/lnk", "sub"),
            (f"{self.ROOT_DIR}/lnk/injected.txt", b"x"))
        assert res.returncode != 0, res.stdout
        assert "внутри симлинка" in res.stdout + res.stderr
        assert not os.path.lexists(dest)


class TestCurlHttpsOnly:
    """Без тестовой обёртки: curl скрипта не ходит ни по http, ни по file."""

    @pytest.mark.parametrize("scheme_url", ["http://127.0.0.1:9/repo.git", "file:///nonexistent/repo.git"])
    def test_non_https_url_is_refused_by_curl(self, tmp_path, scheme_url):
        stage = tmp_path / "stage"
        stage.mkdir()
        res = _bootstrap(tmp_path, f'fetch_pinned_tarball "{stage}"', REPO_URL=scheme_url,
                         EXPECTED_COMMIT="0123456789abcdef0123456789abcdef01234567")
        assert res.returncode != 0
        assert "disabled" in res.stderr, res.stderr
        assert list(stage.iterdir()) == []

    def test_every_curl_call_ignores_curlrc_and_pins_https(self):
        text = (SCRIPTS / "bootstrap_backend.command").read_text()
        calls = [ln.strip() for ln in text.splitlines()
                 if re.search(r"(^|[\s;|&(])curl\s", ln) and not ln.lstrip().startswith("#")]
        assert calls, "curl не найден — проверка пустая"
        for call in calls:
            assert call.startswith("curl -q --proto '=https' --proto-redir '=https' "), call


_CODE_BLOCK_RE = re.compile(r'^if \[ -f "\$INSTALL_DIR/KrabEar/backend/service\.py" \]; then\n.*?^fi\n', re.S | re.M)


def _run_code_step(tmp_path: Path, install_dir: Path) -> subprocess.CompletedProcess:
    """Шаг «3. Код проекта» верхнего уровня; скачивание заменено заглушкой."""
    block = _CODE_BLOCK_RE.search((SCRIPTS / "bootstrap_backend.command").read_text())
    assert block, "блок «Код проекта» не найден"
    prelude = ('log() { echo "LOG $*"; }\nfail() { echo "FAIL $*"; exit 1; }\n'
               'install_pinned_code() { echo DOWNLOAD_CALLED; return 1; }\n')
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "INSTALL_DIR": str(install_dir), "EXPECTED_COMMIT": "0" * 40, "REPO_URL": "https://example.invalid/r.git"}
    return subprocess.run([BASH, "-c", prelude + block.group()], env=env, capture_output=True, text=True, timeout=60)


class TestInstallDirSymlinkRefused:
    def test_dangling_symlink_install_dir_fails_before_download(self, tmp_path):
        target = tmp_path / "nowhere"
        link = tmp_path / "KrabEar"
        link.symlink_to(target)
        res = _run_code_step(tmp_path, link)
        assert res.returncode != 0 and "FAIL" in res.stdout, res.stdout
        assert "DOWNLOAD_CALLED" not in res.stdout
        assert link.is_symlink() and not target.exists()

    def test_symlink_to_foreign_dir_fails_before_download(self, tmp_path):
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        link = tmp_path / "KrabEar"
        link.symlink_to(foreign, target_is_directory=True)
        res = _run_code_step(tmp_path, link)
        assert res.returncode != 0 and "DOWNLOAD_CALLED" not in res.stdout, res.stdout
        assert list(foreign.iterdir()) == []

    def test_absent_install_dir_proceeds_to_download(self, tmp_path):
        res = _run_code_step(tmp_path, tmp_path / "KrabEar")
        assert "DOWNLOAD_CALLED" in res.stdout, res.stdout + res.stderr

    @needs_git
    @pytest.mark.parametrize("kind", ["dangling", "to-empty-dir"])
    def test_symlink_appearing_during_download_is_not_replaced(self, tmp_path, src_repo, kind):
        """install_pinned_code тоже не делает mv в симлинк (появился после шага 3)."""
        repo, sha_a, _b = src_repo
        dest = tmp_path / "home" / "KrabEar"
        dest.parent.mkdir(parents=True)
        target = tmp_path / ("nowhere" if kind == "dangling" else "empty")
        if kind == "to-empty-dir":
            target.mkdir()
        dest.symlink_to(target, target_is_directory=True)
        res = _bootstrap(tmp_path, "install_pinned_code", REPO_URL=f"file://{repo}", EXPECTED_COMMIT=sha_a,
                         INSTALL_DIR=str(dest))
        assert res.returncode != 0, res.stdout
        assert dest.is_symlink() and os.readlink(dest) == str(target)
        assert (not target.exists()) if kind == "dangling" else list(target.iterdir()) == []
        assert _leftovers(tmp_path) == []


class TestAssembleStampsBootstrapCommit:
    def _awk_program(self) -> str:
        text = (SCRIPTS / "assemble_signed_app.sh").read_text()
        match = re.search(r"awk -v sha=\"\$BOOTSTRAP_COMMIT\" '(.*?)' \"\$ROOT_DIR/scripts/bootstrap_backend",
                          text, re.S)
        assert match, "штамп коммита не найден в assemble_signed_app.sh"
        return match.group(1)

    def test_stamp_lands_exactly_once_and_is_resolved(self, tmp_path):
        sha = "89abcdef0123456789abcdef0123456789abcdef"
        out = tmp_path / "bootstrap_backend.command"
        res = subprocess.run(["awk", "-v", f"sha={sha}", self._awk_program(),
                              str(SCRIPTS / "bootstrap_backend.command")],
                             capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        out.write_text(res.stdout)
        assert res.stdout.count(f'PINNED_COMMIT="{sha}"') == 1
        assert subprocess.run([BASH, "-n", str(out)], timeout=30).returncode == 0
        resolve = _function_source("bootstrap_backend.command", "resolve_expected_commit")
        stamped = re.search(r"^PINNED_COMMIT=.*$", res.stdout, re.M).group()
        got = subprocess.run([BASH, "-c", f"{stamped}\nSCRIPT_DIR=/nonexistent\n{resolve}resolve_expected_commit"],
                             env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=30)
        assert got.stdout.strip() == sha

    def test_missing_stamp_line_fails_the_assembly(self, tmp_path):
        broken = tmp_path / "b.command"
        broken.write_text("#!/bin/bash\necho no stamp\n")
        res = subprocess.run(["awk", "-v", "sha=" + "a" * 40, self._awk_program(), str(broken)],
                             capture_output=True, text=True, timeout=30)
        assert res.returncode != 0

    def test_bootstrap_source_never_fetches_a_branch(self):
        text = (SCRIPTS / "bootstrap_backend.command").read_text()
        assert "--branch" not in text
        assert "refs/heads" not in text
        assert "KRAB_EAR_BRANCH" not in text


# ── KEG2-21: нотаризация fail-closed; KEG2-22: превью без абсолютного пути ─────


def _notarize_step(tmp_path: Path, notary_status: str, staple_rc: int) -> subprocess.CompletedProcess:
    text = (SCRIPTS / "build_distribution_dmg.command").read_text()
    match = re.search(r"^# ── Step 5: Notarize.*?(?=^# ── Step 6)", text, re.S | re.M)
    assert match, "шаг 5 не найден"
    stub = tmp_path / "stubbin"
    stub.mkdir()
    (stub / "xcrun").write_text(
        f'#!/bin/sh\ncase "$1" in notarytool) echo "status: {notary_status}";; stapler) exit {staple_rc};; esac\n')
    (stub / "xcrun").chmod(0o755)
    prelude = ('log() { echo "LOG $*"; }\nok() { echo "OK $*"; }\nwarn() { echo "WARN $*"; }\n'
               'err() { echo "ERR $*" >&2; exit 1; }\nDO_NOTARIZE=true\nDMG_PATH=/nonexistent.dmg\n'
               'APPLE_ID_EMAIL=e APPLE_ID_PASSWORD=p APPLE_TEAM_ID=t\n')
    env = {"PATH": f"{stub}:/usr/bin:/bin"}
    return subprocess.run([BASH, "-c", prelude + match.group() + 'echo REACHED_SUMMARY\n'], env=env,
                          capture_output=True, text=True, timeout=30)


class TestNotarizeFailClosed:
    def test_rejected_notarization_stops_the_build(self, tmp_path):
        res = _notarize_step(tmp_path, "Invalid", 0)
        assert res.returncode != 0, res.stdout
        assert "REACHED_SUMMARY" not in res.stdout
        assert "Notarization stapled" not in res.stdout

    def test_staple_failure_stops_the_build(self, tmp_path):
        res = _notarize_step(tmp_path, "Accepted", 1)
        assert res.returncode != 0, res.stdout
        assert "REACHED_SUMMARY" not in res.stdout

    def test_accepted_and_stapled_passes(self, tmp_path):
        res = _notarize_step(tmp_path, "Accepted", 0)
        assert res.returncode == 0, res.stdout + res.stderr
        assert "REACHED_SUMMARY" in res.stdout


def test_settings_preview_resolves_project_relative_to_itself():
    text = (ROOT / "run_settings_preview.command").read_text()
    assert "/Users/" not in text
    assert 'cd "$(dirname "$0")" || exit 1' in text
