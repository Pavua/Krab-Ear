"""Готовит приватный временный plist в каталоге установки."""

import os
import plistlib
import re
import sys
import tempfile
from pathlib import Path

PLACEHOLDERS = re.compile(r"__HOME__|__PROJECT_ROOT__|__HF_TOKEN__")


def render(template: Path, target: Path, home: str, root: str, token: str) -> Path:
    values = {"__HOME__": home, "__PROJECT_ROOT__": root, "__HF_TOKEN__": token}

    def substitute(value):
        if isinstance(value, str):
            return PLACEHOLDERS.sub(lambda match: values[match.group()], value)
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value

    with template.open("rb") as source:
        plist = substitute(plistlib.load(source))
    if not token:
        variables = plist.get("EnvironmentVariables", {})
        variables.pop("HF_TOKEN", None)
        variables.pop("KRAB_EAR_HF_TOKEN", None)

    # mkstemp выставляет 0600 до записи первого байта.
    fd, filename = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    staged = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(plist, stream)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


if __name__ == "__main__":
    template_path, target_path, home_dir, project_root = sys.argv[1:]
    # Токен передаётся через stdin, чтобы значение не попадало в argv.
    print(render(Path(template_path), Path(target_path), home_dir, project_root, sys.stdin.read()))
