"""Fail-closed аудит: public Krab Ear не маршрутизирует jobs на личный runner."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_RUBY_PARSE = r'''require "json"
require "yaml"
data = YAML.safe_load(
  File.read(ARGV.fetch(0)),
  permitted_classes: [],
  permitted_symbols: [],
  aliases: false
)
STDOUT.write(JSON.generate(data))
'''
_ALLOWED_HOSTED_RUNNERS = frozenset(
    {
        "ubuntu-latest",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "macos-latest",
        "macos-14",
        "macos-15",
        "macos-15-intel",
        "macos-26",
        "macos-26-intel",
        "windows-latest",
        "windows-2022",
        "windows-2025",
    }
)


@dataclass(frozen=True)
class Finding:
    """Одно нарушение policy в workflow."""

    workflow: str
    job: str
    reason: str

    def describe(self) -> str:
        suffix = f":{self.job}" if self.job else ""
        return f"{self.workflow}{suffix}: {self.reason}"


def load_workflow(path: Path) -> dict[str, Any]:
    """Читает YAML через доступный на runner Ruby/Psych без Python-зависимости."""
    ruby_path = shutil.which("ruby")
    if ruby_path is None:
        raise RuntimeError("ruby_unavailable")

    result = subprocess.run(
        [ruby_path, "-e", _RUBY_PARSE, str(path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    if result.returncode:
        error_type = result.stderr.strip().split(":", 1)[0] or "unknown_error"
        raise ValueError(f"yaml_parse_error:{error_type}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("yaml_parse_error:invalid_json_bridge") from exc
    if not isinstance(data, dict):
        raise ValueError("workflow_root_not_mapping")

    # YAML 1.1 трактует некавычное `on` как boolean true; JSON делает из него
    # ключ-строку "true". GitHub ожидает YAML 1.2 семантику, поэтому
    # нормализуем только этот известный межпарсерный дрейф.
    if "on" not in data and "true" in data:
        data["on"] = data.pop("true")
    return data


def _trigger_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    if isinstance(value, dict):
        return {str(key) for key in value}
    return set()


def _runner_selector_reason(value: Any) -> str | None:
    """Разрешает только явный GitHub-hosted scalar, не label-combination."""
    if isinstance(value, list):
        if any(isinstance(item, str) and item.casefold() == "self-hosted" for item in value):
            return "self_hosted_runner"
        return "runner_label_not_allowlisted"
    if not isinstance(value, str) or "${{" in value:
        return "dynamic_runs_on"
    if value.casefold() == "self-hosted":
        return "self_hosted_runner"
    if value not in _ALLOWED_HOSTED_RUNNERS:
        return "runner_label_not_allowlisted"
    return None


def audit_workflow(path: Path, root: Path) -> list[Finding]:
    """Возвращает policy findings одного workflow, не разрешая неясный YAML."""
    relative = str(path.relative_to(root))
    try:
        data = load_workflow(path)
    except (OSError, RuntimeError, ValueError) as exc:
        return [Finding(relative, "", str(exc))]

    findings: list[Finding] = []
    if "pull_request_target" in _trigger_names(data.get("on")):
        findings.append(Finding(relative, "", "pull_request_target_forbidden"))

    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return findings + [Finding(relative, "", "jobs_not_mapping")]

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            findings.append(Finding(relative, str(job_name), "job_not_mapping"))
            continue
        if "uses" in job:
            findings.append(Finding(relative, str(job_name), "reusable_workflow_forbidden"))
            continue
        reason = _runner_selector_reason(job.get("runs-on"))
        if reason:
            findings.append(Finding(relative, str(job_name), reason))
    return findings


def audit_tree(root: Path) -> list[Finding]:
    """Проверяет все YAML workflow public repository."""
    workflow_dir = root / ".github" / "workflows"
    workflows = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    if not workflows:
        return [Finding(str(workflow_dir), "", "no_workflows_found")]
    return [finding for workflow in workflows for finding in audit_workflow(workflow, root)]


def main(argv: list[str] | None = None) -> int:
    """Печатает findings и возвращает nonzero только при включённом strict режиме."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--fail-on-found", action="store_true")
    args = parser.parse_args(argv)

    findings = audit_tree(args.root.resolve())
    for finding in findings:
        print(finding.describe())
    return 1 if findings and args.fail_on_found else 0


if __name__ == "__main__":
    raise SystemExit(main())
