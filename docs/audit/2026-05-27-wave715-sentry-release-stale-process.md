# Wave 715 — Sentry Release Tag: Stale-Process Root Cause

**Date:** 2026-05-27  
**Branch:** wave715/sentry-release-stale-doc  
**Status:** Closed — no code fix required

---

## Finding (W701)

Captured Sentry events carried `release: krab-ear@2.0.0` even after v2.0.3/v2.0.4 shipped. This looked like a code bug in release-string construction.

## Investigation

`get_release_string()` in `backend/observability.py` already returns the correct value (`krab-ear@2.0.4`) with the current code. Priority chain:

1. `KRAB_EAR_RELEASE` env var
2. `CFBundleShortVersionString` from `Info.plist`
3. `__version__.py`

Swift `SentryConfig.swift` also reads `CFBundleShortVersionString` from `Bundle.main` — correct at compile time.

W709 "fix" was a no-op: the fix was already in tree from an earlier wave.

## Actual Root Cause

The backend process was **started before** the v2.0.4 code was deployed. The running process loaded the old `observability.py` and called `sentry_sdk.init(release="krab-ear@2.0.0")` at startup. Subsequent events inherited that release tag for the lifetime of the process — no amount of in-tree fixes affects a stale-running process.

Same applies to the Swift agent: the old binary in `native/runtime/KrabEarAgent` (two-binary drift) was still running after the bundle was updated.

## Verification

```bash
# Confirm current code is correct (should print krab-ear@2.0.4)
source .venv_krab_ear/bin/activate
python3 -c "
import sys; sys.path.insert(0, 'KrabEar')
from backend.observability import get_release_string
print(get_release_string())
"
```

## Fix

Restart the backend and Swift agent after every release bump:

```bash
pkill -f "python.*KrabEar/main.py" || true
pkill -f KrabEarAgent || true
# Then relaunch normally (launchd or Open "Krab Ear.app")
open "Krab Ear.app"
```

## Recommendation: Startup Diagnostics

Add a log line in `StartupDiagnostics` (or `main.py` startup block) that records the resolved Sentry release string:

```python
logger.info("Sentry release resolved", extra={"release": get_release_string()})
```

This creates an observable breadcrumb in production logs. If monitoring sees events tagged with a release older than the expected version, the mismatch is immediately visible — catching stale-process drift before it pollutes issue grouping.

---

*Audit by Wave 715. ~270 words.*
