"""Реальный CLI staging: временный Git и plist, без launchd/IPC/прод-профиля."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/prepare_release_cutover.py"


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True,
    ).strip()


@pytest.fixture
def release_case(tmp_path):
    roots = []
    for name in ("old release", "new release"):
        root = tmp_path / name
        (root / "KrabEar/backend").mkdir(parents=True)
        (root / "KrabEar/main.py").write_text("# backend\n")
        (root / "KrabEar/backend/rest_server.py").write_text("# rest\n")
        git(root, "init", "-q")
        git(root, "add", "KrabEar")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", name)
        git(root, "checkout", "--detach", "-q")
        roots.append(root)
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    original = {}
    for role, entry in (("backend", "main.py"), ("rest", "backend/rest_server.py")):
        data = {
            "Label": f"ai.krab.ear.{role}",
            "ProgramArguments": [sys.executable, str(roots[0] / "KrabEar" / entry)],
            "EnvironmentVariables": {"PYTHONPATH": str(roots[0] / "KrabEar"),
                                     "HF_TOKEN": "private-fixture-&-<value>"},
            "WorkingDirectory": str(tmp_path),
            "StandardOutPath": str(tmp_path / "kept.log"),
            "KeepAlive": True,
        }
        if role == "backend":
            data["ProgramArguments"] += ["--data-dir", str(tmp_path / "live data")]
        raw = plistlib.dumps(data)
        path = agents / f"ai.krab.ear.{role}.plist"
        path.write_bytes(raw)
        path.chmod(0o600)
        original[role] = raw
    return roots, agents, tmp_path / "stage", original


def cli(case, *extra):
    old, new = case[0]
    return subprocess.run(
        [sys.executable, str(SCRIPT), "prepare", "--old-root", str(old),
         "--old-sha", git(old, "rev-parse", "HEAD"), "--new-root", str(new),
         "--new-sha", git(new, "rev-parse", "HEAD"),
         "--launchagents", str(case[1]), "--output", str(case[2]), *extra],
        capture_output=True, text=True, timeout=15,
    )


def verify(case, state="before"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--bundle", str(case[2]),
         "--state", state], capture_output=True, text=True, timeout=15,
    )


def test_stage_changes_only_entrypoint_and_pythonpath_and_keeps_exact_backups(release_case):
    case = release_case
    result = cli(case)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "private-fixture" not in result.stdout + result.stderr
    assert (case[2].stat().st_mode & 0o777) == 0o700
    for role, entry in (("backend", "main.py"), ("rest", "backend/rest_server.py")):
        assert (case[2] / f"{role}.before.plist").read_bytes() == case[3][role]
        assert (case[1] / f"ai.krab.ear.{role}.plist").read_bytes() == case[3][role]
        expected = plistlib.loads(case[3][role])
        expected["ProgramArguments"][1] = str(case[0][1] / "KrabEar" / entry)
        expected["EnvironmentVariables"]["PYTHONPATH"] = str(case[0][1] / "KrabEar")
        assert plistlib.loads((case[2] / f"{role}.after.plist").read_bytes()) == expected
    for path in case[2].iterdir():
        assert (path.stat().st_mode & 0o777) == 0o600
    assert verify(case).returncode == 0
    assert verify(case, "after").returncode != 0
    for role in case[3]:
        (case[1] / f"ai.krab.ear.{role}.plist").write_bytes(
            (case[2] / f"{role}.after.plist").read_bytes())
    assert verify(case, "after").returncode == 0
    assert verify(case).returncode != 0


@pytest.mark.parametrize("field", ["entry", "pythonpath", "label", "interpreter"])
def test_unexpected_live_configuration_fails_before_creating_bundle(release_case, field):
    case = release_case
    path = case[1] / "ai.krab.ear.rest.plist"
    data = plistlib.loads(path.read_bytes())
    if field == "entry":
        data["ProgramArguments"][1] += ".other"
    elif field == "pythonpath":
        data["EnvironmentVariables"]["PYTHONPATH"] += ":/unreviewed"
    elif field == "label":
        data["Label"] = "other.service"
    else:
        data["ProgramArguments"][0] = "/missing/python"
    path.write_bytes(plistlib.dumps(data))
    assert cli(case).returncode != 0
    assert not case[2].exists()


def test_wrong_expected_sha_and_dirty_release_are_refused(release_case):
    case = release_case
    assert cli(case, "--new-sha", "0" * 40).returncode != 0
    assert not case[2].exists()
    (case[0][1] / "KrabEar/main.py").write_text("# changed\n")
    assert cli(case).returncode != 0
    assert not case[2].exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_irregular_plist_is_refused_without_blocking(release_case, kind):
    case = release_case
    path = case[1] / "ai.krab.ear.backend.plist"
    path.unlink()
    if kind == "symlink":
        path.symlink_to(case[1] / "ai.krab.ear.rest.plist")
    else:
        os.mkfifo(path)
    assert cli(case).returncode != 0
    assert not case[2].exists()


@pytest.mark.parametrize("drift", ["live", "candidate", "release", "permissions", "manifest"])
def test_verify_refuses_drift(release_case, drift):
    case = release_case
    assert cli(case).returncode == 0
    if drift == "live":
        (case[1] / "ai.krab.ear.rest.plist").write_bytes(b"changed")
    elif drift == "candidate":
        (case[2] / "backend.after.plist").write_bytes(b"changed")
    elif drift == "release":
        (case[0][1] / "KrabEar/main.py").write_text("# changed\n")
    elif drift == "permissions":
        (case[2] / "rest.before.plist").chmod(0o644)
    else:
        path = case[2] / "manifest.json"
        data = json.loads(path.read_text())
        data["new_sha"] = "0" * 40
        path.write_text(json.dumps(data))
    result = verify(case)
    assert result.returncode != 0
    assert "private-fixture" not in result.stdout + result.stderr


def test_existing_output_is_never_overwritten(release_case):
    case = release_case
    case[2].mkdir()
    marker = case[2] / "owner-file"
    marker.write_text("keep")
    assert cli(case).returncode != 0
    assert marker.read_text() == "keep"


def test_bundle_verification_allows_partial_install_but_still_checks_backups(release_case):
    case = release_case
    assert cli(case).returncode == 0
    (case[1] / "ai.krab.ear.backend.plist").write_bytes(
        (case[2] / "backend.after.plist").read_bytes())
    assert verify(case).returncode != 0
    assert verify(case, "after").returncode != 0
    assert verify(case, "bundle").returncode == 0
    (case[2] / "rest.before.plist").write_bytes(b"corrupt")
    assert verify(case, "bundle").returncode != 0
