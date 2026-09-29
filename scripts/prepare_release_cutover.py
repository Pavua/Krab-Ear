#!/usr/bin/env python3
"""Подготовить/проверить приватные plist-копии. Никогда не вызывает launchctl.

Меняет только Python entrypoint и PYTHONPATH; не пишет в LaunchAgents.
Процедура применения/отката: docs/superpowers/plans/2026-09-29-safe-release-cutover.md.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess


ENTRIES = {"backend": "main.py", "rest": "backend/rest_server.py"}
LIMIT = 1024 * 1024


class Refused(Exception):
    """Диагностика без значений plist и секретов."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def read_file(path: Path, private=False) -> bytes:
    """Дескрипторная проверка не позволяет зависнуть на FIFO или пройти symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= LIMIT,
                "Ожидался небольшой обычный файл")
        if private:
            require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600,
                    "Приватный файл должен принадлежать владельцу и иметь mode 0600")
        raw = stream.read(LIMIT + 1)
    require(len(raw) <= LIMIT, "Превышен предел размера файла")
    return raw


def git(root: Path, *args) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, timeout=15, check=False)
    require(result.returncode == 0, "Не удалось проверить Git release")
    return result.stdout.strip()


def check_release(root: Path, sha: str):
    require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "Нужен полный Git SHA")
    require(root.is_absolute() and root.resolve() == root, "Нужен канонический release path")
    require(git(root, "rev-parse", "--show-toplevel") == str(root), "Неверный Git root")
    require(git(root, "rev-parse", "HEAD") == sha, "Release HEAD не совпадает с SHA")
    require(git(root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD",
            "Release должен быть detached")
    require(not git(root, "status", "--porcelain", "--untracked-files=normal"),
            "Release содержит незакоммиченные изменения")
    for entry in ENTRIES.values():
        read_file(root / "KrabEar" / entry)


def candidate(raw: bytes, role: str, old: Path, new: Path) -> bytes:
    before = plistlib.loads(raw)
    require(isinstance(before, dict), "Неверный plist")
    require(before.get("Label") == f"ai.krab.ear.{role}", "Неверный label")
    args = before.get("ProgramArguments")
    require(isinstance(args, list) and len(args) >= 2
            and all(isinstance(value, str) for value in args), "Неверные ProgramArguments")
    require("Program" not in before, "Отдельный Program не поддерживается")
    require(args[1] == str(old / "KrabEar" / ENTRIES[role]), "Неожиданный старый entrypoint")
    interpreter = Path(args[0])
    require(interpreter.is_absolute() and interpreter.is_file()
            and os.access(interpreter, os.X_OK), "Python interpreter недоступен")
    env = before.get("EnvironmentVariables")
    require(isinstance(env, dict) and env.get("PYTHONPATH") == str(old / "KrabEar"),
            "Неожиданный старый PYTHONPATH")
    after = copy.deepcopy(before)
    after["ProgramArguments"][1] = str(new / "KrabEar" / ENTRIES[role])
    after["EnvironmentVariables"]["PYTHONPATH"] = str(new / "KrabEar")
    return plistlib.dumps(after, sort_keys=False)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_private(path: Path, raw: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(args):
    old, new = args.old_root, args.new_root
    check_release(old, args.old_sha)
    check_release(new, args.new_sha)
    require(old != new and args.old_sha != args.new_sha, "Релизы должны различаться")
    agents = args.launchagents.resolve(strict=True)
    contents = {}
    hashes = {}
    # Вся валидация обоих юнитов до первой записи даже в staging.
    for role in ENTRIES:
        raw = read_file(agents / f"ai.krab.ear.{role}.plist", private=True)
        contents[f"{role}.before.plist"] = raw
        contents[f"{role}.after.plist"] = candidate(raw, role, old, new)
    for name, raw in contents.items():
        hashes[name] = digest(raw)
    manifest = {"version": 1, "old_root": str(old), "old_sha": args.old_sha,
                "new_root": str(new), "new_sha": args.new_sha,
                "launchagents": str(agents), "sha256": hashes}
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, raw in contents.items():
        write_private(args.output / name, raw)
    sync_dir(args.output)
    # Manifest — последний durable marker; неполный bundle нельзя применять.
    write_private(args.output / "manifest.json", json.dumps(manifest, indent=2).encode())
    sync_dir(args.output)
    sync_dir(args.output.parent)
    verify(args.output, "before")
    print("PREPARED: проверены оба SHA и 4 plist; runtime не изменён")


def verify(bundle: Path, state: str):
    info = bundle.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "Bundle должен иметь mode 0700")
    manifest = json.loads(read_file(bundle / "manifest.json", private=True))
    require(isinstance(manifest, dict), "Неверный manifest")
    require(manifest.get("version") == 1, "Неверная версия bundle")
    old, new = Path(manifest["old_root"]), Path(manifest["new_root"])
    check_release(old, manifest["old_sha"])
    check_release(new, manifest["new_sha"])
    require(old != new and manifest["old_sha"] != manifest["new_sha"],
            "Релизы должны различаться")
    agents = Path(manifest["launchagents"])
    for role in ENTRIES:
        before = read_file(bundle / f"{role}.before.plist", private=True)
        after = read_file(bundle / f"{role}.after.plist", private=True)
        for suffix, raw in (("before", before), ("after", after)):
            require(digest(raw) == manifest["sha256"][f"{role}.{suffix}.plist"],
                    "Контрольная сумма bundle не совпадает")
        require(after == candidate(before, role, old, new), "Недопустимые изменения plist")
        if state != "bundle":
            current = read_file(agents / f"ai.krab.ear.{role}.plist", private=True)
            require(current == (before if state == "before" else after),
                    "Действующий plist изменился; требуется повторная проверка")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("prepare", help="только подготовить приватные копии")
    for name in ("old-root", "new-root", "launchagents", "output"):
        stage.add_argument("--" + name, type=Path, required=True)
    for name in ("old-sha", "new-sha"):
        stage.add_argument("--" + name, required=True)
    check = commands.add_parser("verify", help="только проверить копии и действующие plist")
    check.add_argument("--bundle", type=Path, required=True)
    check.add_argument("--state", choices=("before", "after", "bundle"), required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
        else:
            verify(args.bundle, args.state)
            print(f"VERIFIED ({args.state}): bundle и SHA совпадают; loaded launchd/PID не проверялись")
    except Refused as error:
        parser.exit(1, f"REFUSED: {error}\n")
    except (OSError, ValueError, TypeError, KeyError, plistlib.InvalidFileException,
            subprocess.SubprocessError):
        # Исключение parser/OS может содержать содержимое plist: не печатать его.
        parser.exit(1, "REFUSED: ошибка чтения/формата/записи; runtime не изменён\n")


if __name__ == "__main__":
    main()
