"""Проверяет точный результат hosted-only probe, не читая пользовательских данных."""

import json
import os
from pathlib import Path
import stat
import sys


CANARIES = ["network", "spawn", "protected_read", "exit"]
EVENTS = [["kill", None], ["flush", None], ["exit", 70]]
SCOPE = "synthetic response-close; not real 504, model, runtime, or drain"
MUTATIONS = {
    "backend_skip_mark": ["marker_before_adopt", "marker_after_close", "forbidden_operations"],
    "backend_wrong_module": ["marker_before_adopt", "marker_after_close", "forbidden_operations"],
    "backend_bypass_guard": ["forbidden_operations"],
}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def validate(path, scenario):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("receipt must be an owned private regular file")
    if not 2 <= info.st_size <= 8192:
        raise ValueError("invalid receipt size")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_size != info.st_size):
            raise ValueError("receipt changed during open")
        payload = stream.read(8193)
    if len(payload) != info.st_size:
        raise ValueError("receipt changed while reading")
    result = json.loads(payload, object_pairs_hook=_pairs,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    common = {"violations", "canaries", "suppressed_thread_starts", "suppressed_thread_joins", "scope"}
    if not isinstance(result, dict) or result.get("canaries") != CANARIES:
        raise ValueError("missing expected canaries")
    if result.get("violations") != [] or result.get("scope") != SCOPE:
        raise ValueError("unexpected boundary result")
    if (not isinstance(result.get("suppressed_thread_starts"), list)
            or any(not isinstance(item, str) for item in result["suppressed_thread_starts"])):
        raise ValueError("missing suppressed-thread record")
    if (not isinstance(result.get("suppressed_thread_joins"), list)
            or any(not isinstance(item, dict) or set(item) != {"target", "timeout"}
                   or not isinstance(item["target"], str)
                   or (item["timeout"] is not None and type(item["timeout"]) not in {int, float})
                   for item in result["suppressed_thread_joins"])):
        raise ValueError("missing join record")
    if scenario in MUTATIONS:
        if set(result) != common | {"scenario", "oracle", "failures"}:
            raise ValueError("wrong mutation receipt schema")
        if result["scenario"] != scenario or result["oracle"] != "failed" or result["failures"] != MUTATIONS[scenario]:
            raise ValueError("negative mutation did not fail the expected oracle")
    elif scenario in {"standalone_false", "standalone_true"}:
        if set(result) != common | {"scenario", "events"} or result["scenario"] != scenario:
            raise ValueError("wrong standalone receipt schema")
        if result["events"] != (EVENTS if scenario == "standalone_false" else []):
            raise ValueError("wrong standalone event order")
        if scenario == "standalone_false" and type(result["events"][-1][1]) is not int:
            raise ValueError("exit code must be an integer")
    elif scenario in {"backend", "backend_setup_failure"}:
        expected_failure = scenario == "backend_setup_failure"
        if set(result) != common | {"scenario", "setup_failure", "mutation", "adoption_marker", "events"}:
            raise ValueError("wrong backend receipt schema")
        if (result["scenario"] != "backend" or result["setup_failure"] is not expected_failure
                or result["mutation"] is not None or not isinstance(result["adoption_marker"], list)
                or len(result["adoption_marker"]) != 1 or result["adoption_marker"][0] is not True
                or result["events"] != []):
            raise ValueError("backend safety oracle did not pass")
    else:
        raise ValueError("unknown scenario")
    print("validated", scenario)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: validate_ci_receipt RECEIPT SCENARIO")
    validate(Path(sys.argv[1]), sys.argv[2])
