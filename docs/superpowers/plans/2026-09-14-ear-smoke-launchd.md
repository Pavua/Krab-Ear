# R4 Ear Smoke on launchd Implementation Plan

**Goal:** Ear E2E-smoke уходит с Claude scheduled-tasks (глохнут без живой
сессии — тишина 09-08→09-14) на launchd `ai.krab.ear.e2e-smoke`
(StartInterval 6h). Проверки 1-в-1 по спеку
`~/.claude/scheduled-tasks/krab-ear-e2e-smoke/SKILL.md`, read-only.

**Architecture:** Новый `scripts/ear_e2e_smoke.py` (stdlib + repo
`backend.sentry_quota`, токен поточечно из Main Krab `.env`, в plist
секретов нет) + `KrabEar/launchagents/ai.krab.ear.e2e-smoke.plist.template`
(Background, LowPriorityIO, `-u`, RunAtLoad false — первый прогон через 6h,
нулевого импакта на прод в момент bootstrap) +
`scripts/install_ear_e2e_smoke.command` (подстановка, bootout+опрос,
`plutil -lint`, bootstrap, verify). Порядок Sentry: сначала приём
(ok/blind/idle/unknown), потом issues `is:unresolved age:-6h`, порог >3.
Wave-50: agent==0 → `ensure_agent_running.command --quiet` → пересчёт.
Настройки — настоящие snake_case (`mode/auto_paste/quality_profile`;
в скилле ошибочно camelCase). Claude-таск не удаляем (чужое, может ожить —
двойные строки в history.log безвредны, append).

**Tech Stack:** то, что уже в репо (venv python, plist templates,
`launchctl print` verify). Новых зависимостей нет.

**База:** `origin/codex/krab-ear-v2`. Ветка `fix/ear-smoke-launchd-20260914`
(параллельных сессий нет).

**Баны:** вставь список из `docs/EXECUTOR_PLAYBOOK.md` §1. Плюс: скрипт
только read-only IPC (ping/diagnostics/list_audio_inputs/get_settings) —
никаких record/dictation; токен только из `.env` в runtime, никогда в
plist/аргументы/логи; Bash 3.2 в installer (нет mapfile/assoc);
`git add` явными путями; git-мерж в колею — отдельным решением (CI-гейт),
bootstrap нового read-only job — часть этой волны после foreground GREEN.

---

### Task 1: smoke-скрипт + plist + installer

**Files:**
- Create: `scripts/ear_e2e_smoke.py`
- Create: `KrabEar/launchagents/ai.krab.ear.e2e-smoke.plist.template`
- Create: `scripts/install_ear_e2e_smoke.command` (chmod +x)
- Test: `KrabEar/tests/test_ear_smoke_launchd_2026_09_14.py` (новый)

- [ ] **Step 1: Write the failing test**

```python
"""R4: launchd-обвязка Ear-smoke существует и корректна."""
from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "KrabEar" / "launchagents" / "ai.krab.ear.e2e-smoke.plist.template"
SCRIPT = REPO / "scripts" / "ear_e2e_smoke.py"
INSTALLER = REPO / "scripts" / "install_ear_e2e_smoke.command"


class SmokeLaunchdTest(unittest.TestCase):
    def test_template_structure(self):
        with open(TEMPLATE, "rb") as fh:
            pl = plistlib.load(fh)
        self.assertEqual(pl["Label"], "ai.krab.ear.e2e-smoke")
        self.assertEqual(pl["StartInterval"], 21600)
        self.assertFalse(pl.get("RunAtLoad", True))
        self.assertEqual(pl.get("ProcessType"), "Background")
        args = pl["ProgramArguments"]
        self.assertTrue(args[-1].endswith("scripts/ear_e2e_smoke.py"))
        self.assertIn(".venv_krab_ear", args[0])

    def test_files_present(self):
        self.assertTrue(SCRIPT.exists())
        self.assertTrue(INSTALLER.exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_ear_smoke_launchd_2026_09_14.py -v`
Expected: FAIL (`FileNotFoundError` — темплейта нет; RED-доказательство:
`launchctl print gui/$(id -u)/ai.krab.ear.e2e-smoke` тоже падает + тишина
smoke-history.log с 09-08).

- [ ] **Step 3: Write the files**

`scripts/ear_e2e_smoke.py` (полностью):

```python
#!/usr/bin/env python3
"""Krab Ear E2E smoke for launchd (R4, 2026-09-14). ... (docstring как в плане выше) ..."""
from __future__ import annotations

import json
import os
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
HISTORY_LOG = REPO_ROOT / ".remember" / "smoke-history.log"
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
    return len([l for l in out.splitlines() if l.strip()])


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


REMEMBER_DIR = REPO_ROOT / ".remember"

if __name__ == "__main__":
    raise SystemExit(main())
```

(Примечание: `REMEMBER_DIR` определён до использования в `main()` на
модульном уровне — порядок в файле: константа рядом с `HISTORY_LOG`.)

`KrabEar/launchagents/ai.krab.ear.e2e-smoke.plist.template` — по образцу
rest-темплейта: Label `ai.krab.ear.e2e-smoke`, ProgramArguments
`[__PROJECT_ROOT__/.venv_krab_ear/bin/python3, -u,
__PROJECT_ROOT__/scripts/ear_e2e_smoke.py]`, StartInterval 21600,
RunAtLoad false, KeepAlive false, LowPriorityIO true,
ProcessType Background, WorkingDirectory `__PROJECT_ROOT__`,
PATH + PYTHONPATH (+ явный HOME), StandardOut/Err
`__PROJECT_ROOT__/logs/krab-ear-e2e-smoke.{out,err}.log`. Без секретов.

`scripts/install_ear_e2e_smoke.command` — Bash 3.2: подстановка
`__HOME__`/`__PROJECT_ROOT__` через sed (`|`-разделитель, экранирование
`&`), `plutil -lint` до bootstrap, bootout + опрос освобождения (копия
`wait_for_bootout` из backend-установщика), bootstrap,
verify `launchctl print gui/$(id -u)/ai.krab.ear.e2e-smoke` +
StartInterval в выводе.

- [ ] **Step 4: GREEN unit + foreground live**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_ear_smoke_launchd_2026_09_14.py -v`
Expected: 2 PASS.
Run: `.venv_krab_ear/bin/python scripts/ear_e2e_smoke.py; echo exit=$?`
Expected: exit 0 + новая OK-строка в `.remember/smoke-history.log`
(`tail -1` — состояние приёма явно, без слова «quiet»).

- [ ] **Step 5: Bootstrap + verify + commit (без мержа)**

```bash
chmod +x scripts/ear_e2e_smoke.py scripts/install_ear_e2e_smoke.command
scripts/install_ear_e2e_smoke.command
launchctl print gui/$(id -u)/ai.krab.ear.e2e-smoke | grep "run interval"
git add scripts/ear_e2e_smoke.py scripts/install_ear_e2e_smoke.command KrabEar/launchagents/ai.krab.ear.e2e-smoke.plist.template KrabEar/tests/test_ear_smoke_launchd_2026_09_14.py docs/superpowers/plans/2026-09-14-ear-smoke-launchd.md
git commit -m "feat(smoke): Ear E2E-smoke на launchd (R4)"
```

Мерж в `origin/codex/krab-ear-v2` — отдельным решением после CI.
Claude-таск не трогаем.
