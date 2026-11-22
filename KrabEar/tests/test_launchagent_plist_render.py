"""Проверяет только артефакт и синтаксис, без вызова launchctl."""

import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("service", ["backend", "rest"])
@pytest.mark.parametrize("token", ["", "SYNTHETIC<&>__HOME__"])
def test_staged_plist_is_private_and_preserves_literal_values(tmp_path, service, token):
    label = f"ai.krab.ear.{service}"
    template = ROOT / "KrabEar" / "launchagents" / f"{label}.plist.template"
    target = tmp_path / f"{label}.plist"
    home = str(tmp_path / "Owner's & home")
    project = str(tmp_path / "Project <&> __HF_TOKEN__")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/render_launchagent_plist.py"), str(template), str(target), home, project],
        input=token,
        capture_output=True,
        text=True,
        env={"PATH": os.defpath},
        check=True,
        timeout=10,
    )
    staged = Path(result.stdout.strip())
    assert staged.parent == target.parent
    assert staged != target
    assert not target.exists()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    with staged.open("rb") as stream:
        data = plistlib.load(stream)
    assert data["Label"] == label
    assert data["StandardOutPath"].startswith(project + "/")
    assert data["EnvironmentVariables"]["KRAB_EAR_DATA_DIR"].startswith(home + "/")
    variables = data["EnvironmentVariables"]
    if token:
        assert variables["HF_TOKEN"] == token
        if service == "backend":
            assert variables["KRAB_EAR_HF_TOKEN"] == token
    else:
        assert "HF_TOKEN" not in variables
        assert "KRAB_EAR_HF_TOKEN" not in variables


@pytest.mark.parametrize("service", ["backend", "rest"])
def test_installer_checks_staged_artifact_before_bootout(service):
    script = (ROOT / "scripts" / f"install_{service}_launchagent.command").read_text()
    assert script.index("render_launchagent_plist.py") < script.index('plutil -lint "$STAGED_PLIST"')
    assert script.index('plutil -lint "$STAGED_PLIST"') < script.index('launchctl bootout "gui/')
    assert script.index('launchctl bootout "gui/') < script.index('mv -f -- "$STAGED_PLIST" "$TARGET"')


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is the migration script interpreter")
def test_migration_script_parses_in_zsh():
    subprocess.run(
        ["zsh", "-n", str(ROOT / "scripts/migrate_to_canonical_launchagent.command")],
        capture_output=True,
        check=True,
        timeout=10,
    )
