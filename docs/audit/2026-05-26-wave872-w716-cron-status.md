# Wave 872 — W716 Cron Retirement Status Check

**Date:** 2026-05-26  
**Script:** `scripts/check_wave716_cron_retirement.sh` (added Wave 788, PR #709)  
**Reference:** `docs/WAVE_716_CRON_RETIREMENT.md`  
**Verdict:** WAIT FOR DEPLOY — do NOT retire the cron yet.

---

## Raw Script Output

Run command:
```
bash scripts/check_wave716_cron_retirement.sh > /tmp/w716_status.txt 2>&1
```

Run timestamp: 2026-05-26 05:17:48 CEST

```
=== Wave 716 cron retirement criteria check ===
Reference: docs/WAVE_716_CRON_RETIREMENT.md
Date:      2026-05-26 05:17:48 CEST

[ 1/5 ] Backend version = 2.0.5
  FAIL — 'version 2.0.5' not found in ~/Library/Logs/KrabEar/backend.log
         (production is likely still v2.0.4; expected failure until v2.0.5 is deployed)

[ 2/5 ] Exactly 1 gigaam_worker process
  PASS — exactly 1 worker (pid line: 3693 /opt/homebrew/Cellar/python@3.12/3.12.11/
         Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/
         Python -u .../KrabEar/core/workers/gigaam_worker.py)

[ 3/5 ] No cron kills in logs (no "killing dup gigaam" entries)
  PASS — /tmp/kill_dup_gigaam.log does not exist (cron never fired a kill, or log not configured)
         Note: cron entry IS active: */10 * * * * .../kill_dup_gigaam.command

[ 4/5 ] REST server log confirms skip_gigaam_warmup=True path
  FAIL — 'skip_gigaam_warmup' or 'GigaAM warmup пропущен' not found in
         ~/Library/Logs/KrabEar/rest_server.log
         (log file does not exist — rest_server may not have started yet)
         This PASS requires v2.0.5 to be running with PR #619 changes.

[ 5/5 ] No Sentry gigaam_worker_crashed events in last 7 days
  MANUAL — Requires Sentry dashboard access.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SUMMARY

  No.   Criterion                             Result  Detail
  ─────────────────────────────────────────────────────────
  1.    Backend v2.0.5                        FAIL    not found in backend.log
  2.    Exactly 1 gigaam_worker               PASS    count=1
  3.    No cron kills in logs                 PASS    log absent — no kills recorded
  4.    REST skip_gigaam_warmup=True          FAIL    not found in rest_server.log
  5.    Sentry: no gigaam errors (7d)         MANUAL  check dashboard at de.sentry.io/organizations/po-zm

Overall: ONE OR MORE CRITERIA FAIL — do NOT retire yet
```

---

## Interpretation

| # | Criterion | Status | Notes |
|---|-----------|--------|-------|
| 1 | Backend running v2.0.5 | **FAIL** | `~/Library/Logs/KrabEar/backend.log` absent + no v2.0.5 line. Production is on v2.0.4 (deploy pending). Expected failure per Wave 788 spec. |
| 2 | Exactly 1 gigaam_worker | **PASS** | PID 3693 running, no duplicates. Wave 716 fix is holding — the cron has not needed to fire. |
| 3 | No cron kills recorded | **PASS** | `/tmp/kill_dup_gigaam.log` does not exist. The `*/10` crontab entry is active but has never triggered a kill. |
| 4 | REST skip_gigaam_warmup=True | **FAIL** | `~/Library/Logs/KrabEar/rest_server.log` absent. REST server not started in current session. This criterion requires v2.0.5 + PR #619 changes to log the skip message. |
| 5 | Sentry: 0 gigaam errors (7d) | **MANUAL** | Cannot check programmatically. Manual steps in script output. |

### Additional observations

- `~/Library/Logs/KrabEar/` directory does not exist — backend logs are currently writing to a different path (likely `~/.krab_ear_data/` in dev-standalone mode or the launchd log location). Criteria 1 and 4 both reference this path; they will remain FAIL until v2.0.5 is deployed via the standard launchd setup which creates this directory.
- The crontab entry `*/10 * * * * .../kill_dup_gigaam.command` is still active.
- Criteria 2 and 3 are healthy: the underlying Wave 716 bug (gigaam duplicate worker spawn) is not recurring.

### Retirement readiness

**NOT ready.** Two automated criteria (1 and 4) block retirement. Both unblock only after:

1. v2.0.5 is shipped and the backend restarts under launchd (populates `~/Library/Logs/KrabEar/backend.log`).
2. The REST server starts at least once under v2.0.5, logging `GigaAM warmup пропущен (skip_gigaam_warmup=True)`.

### What to do after v2.0.5 deploy

```bash
# Re-run the check
bash scripts/check_wave716_cron_retirement.sh

# If all automated criteria PASS, also verify criterion 5 manually in Sentry:
#   https://de.sentry.io/organizations/po-zm/issues/
#   project: krab-ear-backend, filter: gigaam_worker_crashed, last 7 days

# Then retire the cron:
crontab -l > /tmp/cron-pre-w716-retirement.bak
crontab -l | grep -v "kill_dup_gigaam.command" | crontab -
```
