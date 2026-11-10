"""Installer backend не выполняет lifecycle без общего admission/drain."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/install_backend_launchagent.command"


@pytest.mark.parametrize("args,expected", [
    ([], 1), (["--force"], 1), (["--wait", "08"], 1),
    (["--wait"], 2), (["--wait", "x"], 2), (["--wait", "1" * 7], 2), (["--other"], 2),
])
def test_install_refuses_without_admission_fence(tmp_path, args, expected):
    project = tmp_path / "project"
    home = tmp_path / "home"
    bindir = tmp_path / "bin"
    (project / "scripts").mkdir(parents=True)
    (project / "KrabEar/launchagents").mkdir(parents=True)
    (project / ".venv_krab_ear").mkdir()
    home.mkdir()
    bindir.mkdir()
    script = project / "scripts/install_backend_launchagent.command"
    shutil.copy2(INSTALLER, script)
    (project / "KrabEar/launchagents/ai.krab.ear.backend.plist.template").write_text("<plist/>")
    marker = tmp_path / "loaded"
    marker.touch()
    log = tmp_path / "launchctl.log"
    launchctl = bindir / "launchctl"
    launchctl.write_text('''#!/bin/sh
case "$1" in
  print) test -f "$FAKE_LOADED" ;;
  bootout) printf 'bootout\\n' >> "$FAKE_LOG"; rm -f "$FAKE_LOADED" ;;
  bootstrap) printf 'bootstrap\\n' >> "$FAKE_LOG" ;;
esac
''')
    launchctl.chmod(0o700)
    for name in ("sleep", "plutil"):
        stub = bindir / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o700)
    env = {
        "HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin", "LC_ALL": "C",
        "KRAB_EAR_SKIP_HF_TOKEN": "1", "FAKE_LOADED": str(marker), "FAKE_LOG": str(log),
    }
    result = subprocess.run(["/bin/bash", str(script), *args], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == expected
    assert not log.exists() or not log.read_text(), result.stdout + result.stderr
    assert not list(home.iterdir())
    assert not (project / "logs").exists()
