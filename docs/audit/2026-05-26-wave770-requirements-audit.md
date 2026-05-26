# Wave 770 — requirements.txt Pinning & Freshness Audit

**Date:** 2026-05-26  
**Auditor:** Claude (wave770 / requirements-audit-W770)  
**Scope:** `KrabEar/requirements.txt` — pinning consistency, version freshness  
**Venv scanned:** `.venv_krab_ear` (pip 25.x, Python 3.x on M4 Max)

---

## 1. File Overview

`KrabEar/requirements.txt` has **19 active package lines** (excluding commented-out optional packages).  
Optional packages (commented out): `gigaam`, `openwakeword`, `sentence-transformers`.

---

## 2. Pinning Classification

| # | Package | Specifier in file | Classification | Installed version |
|---|---------|-------------------|----------------|------------------|
| 1 | `mlx-whisper` | *(none)* | **Unpinned** | 0.4.3 |
| 2 | `pyannote.audio` | `==4.0.4` | **Pinned (exact)** | 4.0.4 |
| 3 | `numpy` | *(none)* | **Unpinned** | 2.4.4 |
| 4 | `sounddevice` | *(none)* | **Unpinned** | 0.5.5 |
| 5 | `requests` | *(none)* | **Unpinned** | 2.33.1 |
| 6 | `pydantic-settings` | *(none)* | **Unpinned** | 2.13.1 |
| 7 | `soundfile` | *(none)* | **Unpinned** | 0.13.1 |
| 8 | `pyperclip` | *(none)* | **Unpinned** | 1.11.0 |
| 9 | `flask` | *(none)* | **Unpinned** | 3.1.3 |
| 10 | `flask-smorest` | *(none)* | **Unpinned** | 0.47.0 |
| 11 | `flask-sock` | `>=0.7.0` | **Loose (>=)** | 0.7.0 |
| 12 | `marshmallow` | `>=3.18,<5` | **Loose (range)** | 4.3.0 |
| 13 | `pyobjc-framework-Cocoa` | *(none, platform-gated)* | **Unpinned** | 12.1 |
| 14 | `pyobjc-framework-Vision` | *(none, platform-gated)* | **Unpinned** | 12.1 |
| 15 | `websockets` | `>=12.0` | **Loose (>=)** | 16.0 |
| 16 | `gunicorn` | `>=21.2.0` | **Loose (>=)** | 25.3.0 |
| 17 | `flask-limiter` | `>=3.5.0` | **Loose (>=)** | 4.1.1 |
| 18 | `flask-cors` | `>=4.0.0` | **Loose (>=)** | 6.0.2 |
| 19 | `sentry-sdk` | `>=2.0` | **Loose (>=)** | 2.58.0 |
| 20 | `pytest-xdist` | `>=3.5` | **Loose (>=)** | 3.8.0 |

### Summary

| Category | Count | Packages |
|----------|-------|---------|
| **Pinned (exact ==)** | 1 | `pyannote.audio` |
| **Loose (>=, range)** | 7 | `flask-sock`, `marshmallow`, `websockets`, `gunicorn`, `flask-limiter`, `flask-cors`, `sentry-sdk`, `pytest-xdist` |
| **Unpinned** | 12 | `mlx-whisper`, `numpy`, `sounddevice`, `requests`, `pydantic-settings`, `soundfile`, `pyperclip`, `flask`, `flask-smorest`, `pyobjc-framework-Cocoa`, `pyobjc-framework-Vision` |

**Total active packages: 20** (19 lines; `pytest-xdist` is listed separately, `pyobjc` is 2 lines)

---

## 3. Outdated Packages (pip scan result)

Scan ran against `.venv_krab_ear` on 2026-05-26. Total outdated in entire venv: **56**.  
Packages from `requirements.txt` that are outdated: **5**.

| Package | Installed | Latest | Delta | Risk |
|---------|-----------|--------|-------|------|
| `gunicorn` | 25.3.0 | 26.0.0 | major | Low — WSGI server, no API surface used |
| `numpy` | 2.4.4 | 2.4.6 | patch | Low — patch release |
| `pydantic-settings` | 2.13.1 | 2.14.1 | minor | Low — settings layer only |
| `requests` | 2.33.1 | 2.34.2 | minor | Medium — security surface; see §4 |
| `sentry-sdk` | 2.58.0 | 2.60.0 | minor | Low — observability, no-op without DSN |

### Notable non-requirements packages outdated (relevant to runtime)

| Package | Installed | Latest | Notes |
|---------|-----------|--------|-------|
| `mlx` | 0.31.1 | 0.31.2 | Core MLX framework; patch release |
| `torch` | 2.11.0 | 2.12.0 | Minor; MPS stability history means caution (see §4) |
| `huggingface_hub` | 1.8.0 | 1.16.1 | Large jump; pyannote model downloads |
| `pydantic` / `pydantic_core` | 2.12.5 / 2.41.5 | 2.13.4 / 2.47.0 | Minor/patch |
| `certifi` | 2026.2.25 | 2026.5.20 | CA bundle freshness — worth updating |
| `pyannote-metrics` | 4.0.0 | 4.1 | Minor; companion to pinned `pyannote.audio` |

---

## 4. Key Package Notes

### `mlx-whisper` — **Unpinned** (installed: 0.4.3)
**Concern:** This is the primary STT engine. It pulls in `mlx` and `mlx-metal` as transitive
dependencies. Breaking changes in minor releases have historically caused GPU memory corruption
(SIGSEGV — see W71 / PR #71 `mlx_lock` introduction).  
**Recommendation:** Pin to `mlx-whisper==0.4.3` until a new version is explicitly tested against
the `mlx_lock` and `mlx_inter_process_lock` wrappers. Any upgrade must be validated with the
`test_stt_warmup` and `test_stt_routing_scored` suites before production.

### `pyannote.audio` — **Pinned** `==4.0.4` ✓
**Status:** Correctly pinned. Comment in file explains the rationale (MPS working, Sequoia 26.5
compat verified 2026-04-18 by AF audit). The companion package `pyannote-metrics` is at 4.0.0
installed vs 4.1 latest — may need a coordinated bump if `pyannote.audio` is ever upgraded.  
**No action needed** for now.

### `numpy` — **Unpinned** (installed: 2.4.4, latest: 2.4.6)
**Concern:** numpy 2.x ABI differs from 1.x. The venv is already on 2.x, which is correct for the
current ecosystem. A patch update from 2.4.4 → 2.4.6 is low risk but unpinned means any `pip
install -r` on a fresh env could land on an untested future 2.x minor. Loose `>=2.0` or `numpy~=2.4`
pinning would be safer long-term.

### `torch` — not in requirements.txt (transitive via `pyannote.audio`)
**Concern:** torch 2.11.0 → 2.12.0 minor release. Historical lesson: torch+MPS upgrades have
caused Metal GPU stalls (W42 session), silent SIGSEGV risks (not mitigated by `mlx_lock` since
torch uses its own MPS allocator). **Do not auto-upgrade torch** — pin via constraints file or wait
for explicit pyannote.audio upgrade cycle.  
**Recommendation:** Add a `constraints.txt` or inline comment tracking the tested torch version.

### `sentry-sdk` — **Loose** `>=2.0` (installed: 2.58.0, latest: 2.60.0)
Minor version drift. The SDK is no-op without a DSN, so security risk is low. The `>=2.0` floor
is appropriate (ensures modern envelope protocol). A bump to `>=2.0,<3` would prevent accidental
major-version surprises.

### `requests` — **Unpinned** (installed: 2.33.1, latest: 2.34.2)
`requests` is used by `llm_probe.py`, `telnyx_adapter.py`, `twilio_adapter.py`, and webhook
calls — all make outbound HTTP. The 2.33→2.34 bump is a minor release; historically requests
minor releases include CVE patches. Worth upgrading, but not urgent given all connections are to
known endpoints on localhost or trusted APIs.

### `marshmallow` — **Loose** `>=3.18,<5` (installed: 4.3.0)
The `<5` upper bound is intentional and correct — it prevents a future marshmallow 5.x major from
breaking `flask-smorest` serialization schemas. This is the right pattern for range pinning.

### `websockets` — **Loose** `>=12.0` (installed: 16.0)
The installed version (16.0) is a major-version jump from the floor (12.0). The `VGWebSocketClient`
and `SystemAudioCapture` flow use `websockets`. API between 12.x and 16.x includes breaking changes
in connection handling. The fact it runs today means the codebase is compatible, but the wide range
creates risk if a fresh install lands on a future 17.x. Consider tightening to `websockets>=12.0,<17`.

### `flask-cors` — **Loose** `>=4.0.0` (installed: 6.0.2)
Flask-CORS 6.x was a major release with breaking config key renames. The installed version working
suggests code was migrated, but the `>=4.0.0` floor still allows a future pip install to land on 6.x
without that migration, breaking on older envs. Consider `flask-cors>=6.0.0` to match current reality.

---

## 5. Pinning Consistency Assessment

| Concern | Severity | Detail |
|---------|----------|--------|
| `mlx-whisper` unpinned | **HIGH** | Primary STT engine; MLX GPU instability history |
| `websockets` floor vs installed gap (12→16) | **MEDIUM** | 4 major versions of drift; API changes |
| `flask-cors` floor vs installed gap (4→6) | **MEDIUM** | Major release with breaking config renames |
| `numpy` unpinned with 2.x ABI lock-in | **MEDIUM** | Should at least specify `>=2.0` |
| `flask` fully unpinned | **LOW** | Flask 3.x is major; `>=3.0` floor would reflect reality |
| `torch` not listed (transitive only) | **LOW-MEDIUM** | No explicit version tracking; MPS risk |
| `requests` unpinned | **LOW** | Minor CVE surface; add `>=2.33` floor at minimum |
| `gunicorn` loose floor 21 vs installed 25 | **LOW** | Low risk; 26.0 is latest |
| `sentry-sdk` no upper bound | **LOW** | Add `<3` to prevent major-version surprise |

---

## 6. Recommendations (do not auto-execute)

Listed by priority:

1. **Pin `mlx-whisper==0.4.3`** — highest risk package; any upgrade needs explicit MLX regression
   testing (mlx_lock, memory leak, SIGSEGV validation). Add inline comment with date.

2. **Add numpy floor** — change bare `numpy` → `numpy>=2.0` to document the 2.x ABI requirement.
   Or pin `numpy~=2.4` for tighter control.

3. **Tighten websockets** — change `websockets>=12.0` → `websockets>=12.0,<17` to prevent silent
   incompatible major drops.

4. **Fix flask-cors floor** — change `flask-cors>=4.0.0` → `flask-cors>=6.0.0` to match the actual
   minimum that has been validated and is installed in production.

5. **Add sentry-sdk upper bound** — change `sentry-sdk>=2.0` → `sentry-sdk>=2.0,<3` to prevent
   major-version surprises.

6. **Track torch in a `constraints.txt`** — Add `torch==2.11.0` to a separate
   `KrabEar/constraints.txt` (used with `pip install -c constraints.txt -r requirements.txt`).
   This makes the tested torch version explicit without polluting the main requirements file,
   since torch is a transitive dep of pyannote.audio.

7. **Consider upgrading `requests`** — 2.33.1 → 2.34.2 is a minor bump with likely CVE patches.
   Low urgency but worth including in the next maintenance cycle.

8. **Add `flask>=3.0` floor** — makes it clear the codebase targets Flask 3.x REST API patterns.

---

## 7. What Was NOT Scanned

- `KrabEar/requirements-dev.txt` (does not exist — dev deps mixed into main file)
- Optional commented-out packages (`gigaam`, `openwakeword`, `sentence-transformers`)
- `venv_gigaam` separate environment (GigaAM-specific; out of scope)
- Swift Package.resolved (Swift dependency pinning — separate audit scope)

---

*Audit performed read-only. No packages were upgraded or modified.*
