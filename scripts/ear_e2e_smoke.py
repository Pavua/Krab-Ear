#!/usr/bin/env python3
"""Krab Ear E2E smoke for launchd (R4, 2026-09-14).

Порт Claude scheduled-task
~/.claude/scheduled-tasks/krab-ear-e2e-smoke/SKILL.md на launchd
(StartInterval 6h). Read-only: НЕ триггерит record/dictation.

Проверки (порядок важен — сначала приём Sentry, потом issues):
1. Backend ping (unix socket).
2. Diagnostics: секции system/stt/llm/history/settings_cache + status != critical.
3. Swift agent: ровно 1 процесс (0 → Wave-50 auto-recovery, >1 → escalate).
4. Sentry ingest СНАЧАЛА (backend.sentry_quota: ok/blind/idle/unknown),
   потом unresolved age:-6h; issues > 3 → escalate. blind/unknown — FAIL,
   пустой список при них НЕ означает здоровье.
5. Audio: list_audio_inputs count >= 1.
6. Settings: валиден + ключи mode/auto_paste/quality_profile (snake_case).

Persistence: OK → one-liner в .remember/smoke-history.log (состояние приёма
явно, слово «quiet» запрещено); FAIL → .remember/smoke-diagnostic-YYYY-MM-DD.md.
Exit 0 = OK (включая auto-recovered), 1 = FAIL/escalation.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "KrabEar"))

SOCK = Path.home() / "Library/Application Support/KrabEar/krabear.sock"
REMEMBER_DIR = REPO_ROOT / ".remember"
HISTORY_LOG = REMEMBER_DIR / "smoke-history.log"
KRAB_ENV = Path.home() / "Antigravity_AGENTS" / "Краб" / ".env"
ENSURE_AGENT = REPO_ROOT / "scripts" / "ensure_agent_running.command"
SENTRY_ORG = "po-zm"
SENTRY_PROJECTS = ("krab-ear-backend", "krab-ear-agent")
ISSUE_THRESHOLD = 3
AGENT_PATTERN = "Krab Ear.app/Contents/MacOS/KrabEarAgent"
REQUIRED_DIAG = ("system", "stt", "llm", "history", "settings_cache")
REQUIRED_SETTINGS = ("mode", "auto_paste", "quality_profile")
SOCK_TIMEOUT = 5


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


def ipc(method, params=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(SOCK_TIMEOUT)
    try:
        s.connect(str(SOCK))
        s.sendall(json.dumps({"id": "smoke", "method": method,
                              "params": params or {}}).encode() + b"\n")
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if data.endswith(b"\n"):
                break
        return json.loads(data.decode())
    finally:
        s.close()


def read_sentry_token() -> str:
    try:
        for line in KRAB_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("SENTRY_AUTH_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'").strip()
    except OSError:
        pass
    return ""


def check_backend(ctx):
    try:
        r = ipc("ping")
    except Exception as exc:
        return False, f"ping failed: {type(exc).__name__}"
    if not r.get("ok"):
        return False, f"ping ok=false: {str(r)[:120]}"
    uptime = (r.get("result") or {}).get("uptime_sec", "?")
    ctx["uptime"] = uptime
    return True, f"alive (uptime {uptime})"


def check_diagnostics(ctx):
    try:
        r = ipc("get_diagnostics")
    except Exception as exc:
        return False, f"diagnostics failed: {type(exc).__name__}"
    res = r.get("result") or {}
    missing = [k for k in REQUIRED_DIAG if k not in res]
    if missing:
        return False, f"sections missing: {missing}"
    if res.get("status") == "critical":
        return False, "status=critical"
    ctx["nsections"] = len(res)
    return True, f"OK ({len(res)} sections)"


def agent_count():
    try:
        out = subprocess.run(["pgrep", "-f", AGENT_PATTERN],
                             capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except Exception:
        return -1
    return len([line for line in out.splitlines() if line.strip()])


def check_agent(ctx):
    n = agent_count()
    if n == 1:
        return True, "1 (healthy)"
    if n == 0:
        try:
            subprocess.run([str(ENSURE_AGENT), "--quiet"], timeout=180,
                           capture_output=True)
        except Exception as exc:
            return False, f"absent, recovery failed to run: {type(exc).__name__}"
        n2 = agent_count()
        if n2 > 0:
            ctx["recovered"] = True
            return True, f"auto-recovered ({n2})"
        return False, "absent, auto-recovery did not help"
    if n < 0:
        return False, "pgrep failed"
    return False, f"zombie alert: {n} processes"


def check_sentry(ctx):
    try:
        from backend.sentry_quota import (fetch_quota_counts, classify_quota,
                                          format_quota_line)
    except ImportError:
        return False, "unknown (sentry_quota module unavailable)"
    token = read_sentry_token()
    if not token:
        return False, "unknown (no SENTRY_AUTH_TOKEN)"
    try:
        accepted, limited = fetch_quota_counts(token)
    except Exception as exc:
        return False, f"unknown (quota fetch failed: {type(exc).__name__})"
    state = classify_quota(accepted, limited)
    ctx["sentry_line"] = format_quota_line(state, accepted, limited)
    if state in ("blind", "unknown"):
        return False, f"{state} ({ctx['sentry_line']})"
    issues = []
    try:
        for slug in SENTRY_PROJECTS:
            q = urllib.parse.urlencode(
                {"query": f"is:unresolved age:-6h project:{slug}"})
            req = urllib.request.Request(
                f"https://sentry.io/api/0/organizations/{SENTRY_ORG}/issues/?{q}",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                issues += [(slug, i.get("shortId")) for i in json.load(resp)]
    except Exception as exc:
        return False, f"{state} but issues unreadable: {type(exc).__name__}"
    ctx["nissues"] = len(issues)
    if len(issues) > ISSUE_THRESHOLD:
        ctx["issue_ids"] = [s for _, s in issues[:10]]
        return False, f"{state} but {len(issues)} unresolved (>3)"
    return True, f"{state} ({ctx['sentry_line']}), issues {len(issues)}"


def check_audio(ctx):
    try:
        r = ipc("list_audio_inputs")
    except Exception as exc:
        return False, f"audio failed: {type(exc).__name__}"
    n = (r.get("result") or {}).get("count", 0)
    ctx["ndevices"] = n
    if n < 1:
        return False, "no devices"
    return True, f"{n} devices"


def check_settings(ctx):
    try:
        r = ipc("get_settings")
    except Exception as exc:
        return False, f"settings failed: {type(exc).__name__}"
    res = r.get("result")
    if not isinstance(res, dict):
        return False, "settings not a dict"
    missing = [k for k in REQUIRED_SETTINGS if k not in res]
    if missing:
        return False, f"keys missing: {missing}"
    ctx["nkeys"] = len(res)
    return True, "valid"


CHECKS = (
    ("backend", check_backend),
    ("diagnostics", check_diagnostics),
    ("agent", check_agent),
    ("sentry", check_sentry),
    ("audio", check_audio),
    ("settings", check_settings),
)


def main() -> int:
    ctx: dict = {}
    failed = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn(ctx)
        except Exception as exc:  # fail-closed: исключение проверки = FAIL
            ok, detail = False, f"checker crashed: {type(exc).__name__}"
        if not ok:
            failed.append((name, detail))
    recovered = " (auto-recovered Swift agent)" if ctx.get("recovered") else ""
    if not failed:
        line = (f"{now()} OK — backend alive (uptime {ctx.get('uptime')}), "
                f"agent 1{recovered}, Sentry {ctx.get('sentry_line')}, "
                f"audio {ctx.get('ndevices')} devices")
        with open(HISTORY_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line)
        return 0
    body = [f"Krab Ear E2E Smoke — {now()} FAIL",
            *[f"- {name}: {detail}" for name, detail in failed],
            "", "Action: manual review."]
    path = REMEMBER_DIR / f"smoke-diagnostic-{datetime.now():%Y-%m-%d}.md"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    print("\n".join(body))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
