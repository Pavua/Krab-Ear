#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2026-07-24: health-check self-hosted GitHub Actions runner (krab-ear-m4max).

ci.yml/krabear-ci.yml macOS-джобы перевели на self-hosted runner (GitHub
billing: account-wide $0 budget + Stop usage=Yes на Actions; macOS-минуты
биллятся ×10 против Linux — вероятный главный драйвер исчерпания бюджета).
Раннер — постоянный launchd-сервис на этой же машине; класс риска
«инструмент выглядит подключённым, но никогда не работал» / «тихо умер,
никто не заметил» — тот же паттерн, что у сиблинг-раннера krab-m4max
(Krab-openclaw, W-2026-07-23), которого этот файл — прямая копия под
Krab-Ear.

Опрашивает GitHub API ``GET /repos/{repo}/actions/runners``, ищет раннер по
имени. offline >= OFFLINE_STREAK_THRESHOLD подряд идущих прогонов (не
единичный) -> Telegram digest владельцу — тот же hysteresis-паттерн, что
krab_metrics_drift_check.py / krab_runner_health_check.py (Krab-openclaw).
Recovery после алерта — отдельное info-сообщение, streak сбрасывается.

GITHUB_TOKEN и Telegram-креды — общие на все проекты владельца, лежат в
.env главного Krab (не дублируются здесь).

Usage (LaunchAgent ai.krab.ear.runner-health, каждые 15 минут):
    .venv_krab_ear/bin/python scripts/krab_ear_runner_health_check.py
Флаги:
    --dry-run            детект без персиста streak и без Telegram
    --no-telegram         не слать (статус всё равно в stdout)
    --runner-name NAME    имя раннера (default krab-ear-m4max)
    --repo OWNER/REPO     репозиторий (default Pavua/Krab-Ear)
    --state PATH          путь к streak-файлу
Exit codes:
    0 — runner online (или offline streak ниже порога)
    1 — actionable: offline streak >= threshold
    2 — GitHub API/network error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Общие креды владельца (GITHUB_TOKEN, Telegram) живут в .env главного Krab —
# один бот/токен на все проекты, не дублируем per-repo.
_SHARED_ENV_PATH = Path.home() / "Antigravity_AGENTS" / "Краб" / ".env"

DEFAULT_STATE_PATH = Path.home() / ".openclaw" / "krab_runtime_state" / "krab_ear_runner_health_streak.json"
DEFAULT_REPO = "Pavua/Krab-Ear"
DEFAULT_RUNNER_NAME = "krab-ear-m4max"
OFFLINE_STREAK_THRESHOLD = 3  # consecutive checks offline -> actionable
HTTP_TIMEOUT = float(os.environ.get("RUNNER_HEALTH_TIMEOUT_SEC", "15"))

TG_TOKEN = os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
TG_CHAT = os.environ.get("OWNER_NOTIFY_CHAT_ID") or (
    os.environ.get("OWNER_USER_IDS", "").split(",")[0].strip()
)


def _load_dotenv() -> None:
    """Минимальная подгрузка общего .env (KEY=VALUE), не перетирая уже выставленное."""
    if not _SHARED_ENV_PATH.exists():
        return
    try:
        for line in _SHARED_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass


def atomic_write_json(path: Path, payload: dict) -> None:
    """Atomic write через tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".krab_ear_runner_health_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"streak": 0, "alerted": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"streak": 0, "alerted": False}
        return {
            "streak": int(data.get("streak", 0) or 0),
            "alerted": bool(data.get("alerted", False)),
        }
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {"streak": 0, "alerted": False}


def fetch_runner_status(repo: str, runner_name: str, token: str) -> str | None:
    """Возвращает 'online'/'offline' раннера, или None если он не найден вовсе
    (снят с регистрации — тоже actionable, трактуется как offline выше)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{repo}/actions/runners"
    resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    for runner in data.get("runners", []):
        if runner.get("name") == runner_name:
            return str(runner.get("status") or "unknown")
    return None


def send_telegram(text: str) -> None:
    """Отправка digest владельцу через Telegram bot API. Best-effort, guarded."""
    if not TG_TOKEN or not TG_CHAT:
        print(
            "WARN: нет OPENCLAW_TELEGRAM_BOT_TOKEN/OWNER_*CHAT — Telegram пропущен",
            file=sys.stderr,
        )
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        resp = httpx.post(url, json={"chat_id": TG_CHAT, "text": text}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(
            f"WARN: telegram_send_failed error_type={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )


def evaluate(status: str | None, state: dict) -> tuple[dict, bool, bool]:
    """Чистая функция streak-gate: (новый_state, actionable, recovered).

    online -> streak сброшен; recovered=True если до этого был отправлен алерт.
    offline/не найден -> streak++; actionable когда streak достиг порога И
    алерт ещё не отправлялся для этого эпизода (не долбим Telegram на каждом
    прогоне после первого).
    """
    online = status == "online"
    if online:
        recovered = bool(state.get("alerted"))
        return {"streak": 0, "alerted": False}, False, recovered

    streak = int(state.get("streak", 0) or 0) + 1
    already_alerted = bool(state.get("alerted"))
    actionable = streak >= OFFLINE_STREAK_THRESHOLD and not already_alerted
    new_state = {
        "streak": streak,
        "alerted": already_alerted or actionable,
    }
    return new_state, actionable, False


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    global TG_TOKEN, TG_CHAT
    TG_TOKEN = TG_TOKEN or os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
    TG_CHAT = (
        TG_CHAT
        or os.environ.get("OWNER_NOTIFY_CHAT_ID")
        or (os.environ.get("OWNER_USER_IDS", "").split(",")[0].strip())
    )

    parser = argparse.ArgumentParser(description="Self-hosted CI runner health-check (Krab Ear)")
    parser.add_argument("--dry-run", action="store_true", help="детект без персиста/Telegram")
    parser.add_argument("--no-telegram", action="store_true", help="не слать Telegram")
    parser.add_argument("--runner-name", default=DEFAULT_RUNNER_NAME)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    state_path = Path(args.state)
    state = load_state(state_path)

    try:
        status = fetch_runner_status(args.repo, args.runner_name, token)
    except httpx.HTTPError as exc:
        print(
            f"ERROR: github_api_failed error_type={type(exc).__name__} error={exc}",
            file=sys.stderr,
        )
        return 2

    new_state, actionable, recovered = evaluate(status, state)
    print(
        f"runner_health: name={args.runner_name} status={status or 'not_found'} "
        f"streak={new_state['streak']} actionable={actionable}"
    )

    send_fn = None if (args.dry_run or args.no_telegram) else send_telegram
    if recovered and send_fn:
        send_fn(f"✅ Krab Ear self-hosted CI runner '{args.runner_name}' снова online.")
    if actionable and send_fn:
        send_fn(
            f"🔴 Krab Ear self-hosted CI runner '{args.runner_name}' offline "
            f"{new_state['streak']} проверок подряд (status={status or 'not_found'}). "
            f"macOS CI (ci.yml python+swift, krabear-ci.yml swift-build) не выполнится, "
            f"пока раннер не поднимется — проверь: "
            f"cd ~/actions-runner-krab-ear && ./svc.sh status"
        )

    if not args.dry_run:
        atomic_write_json(state_path, new_state)

    return 1 if (new_state["streak"] >= OFFLINE_STREAK_THRESHOLD and status != "online") else 0


if __name__ == "__main__":
    raise SystemExit(main())
