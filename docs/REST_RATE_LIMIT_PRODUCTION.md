# REST Rate-Limit Storage — Production Guide

Krab Ear's Flask REST API (port 5005) uses **flask-limiter** to enforce
per-IP request quotas.  By default the backing store is `memory://`, which is
safe for local / dev use but unsuitable for production.  This guide explains
why and how to switch to Redis or Memcached.

---

## Why `memory://` is unsafe for production

| Problem | Consequence |
|---------|-------------|
| **Process-local** | Each worker process keeps its own counter. Under gunicorn with N workers a client can exceed the limit N× before being blocked. |
| **Resets on restart** | Every time the REST server restarts (crash, deploy, `start_rest_service.command`) all counters are zeroed — burst traffic immediately after restart is unprotected. |
| **No persistence** | There is no record of recent requests across process boundaries; abuse patterns spanning a restart window are invisible. |

The backend logs a `WARNING` at startup when `RATE_LIMIT_ENABLED=true` and
storage is still `memory://`.  Search for **W809 M-2** in the log to confirm.

---

## Recommended: Redis

### 1. Install Redis (Homebrew)

```bash
brew install redis
brew services start redis   # start now + on login
redis-cli ping              # should return PONG
```

### 2. Verify connectivity

```bash
redis-cli -u redis://localhost:6379/0 ping
```

### 3. Set the env var

Add to your shell profile or the launch script (e.g. `start_rest_service.command`):

```bash
export KRAB_EAR_RATE_LIMIT_STORAGE_URI="redis://localhost:6379/0"
```

Then restart the REST server:

```bash
./start_rest_service.command
```

The startup warning for W809 M-2 will no longer appear.

### 4. Optional — Redis with password

```bash
export KRAB_EAR_RATE_LIMIT_STORAGE_URI="redis://:yourpassword@localhost:6379/0"
```

---

## Alternative: Memcached

```bash
brew install memcached
brew services start memcached

export KRAB_EAR_RATE_LIMIT_STORAGE_URI="memcached://localhost:11211"
```

Requires `limits[memcached]` Python package:

```bash
source .venv_krab_ear/bin/activate
pip install "limits[memcached]"
```

---

## Alternative: file-backed SQLite (single process, no Redis)

Suitable when you run a single gunicorn worker or the default single-threaded
dev server and just want persistence across restarts:

```bash
export KRAB_EAR_RATE_LIMIT_STORAGE_URI="file:///var/run/krabear-ratelimit"
```

---

## When does this actually matter?

Current load profile for Krab Ear (local single-Mac install):

- REST API is called by the Swift agent and occasional automation scripts.
- Typical rate: < 5 req/s sustained.
- Default limit: `60 per minute` (global) / `10 per minute` (transcription
  endpoint).

At this load `memory://` is practically fine.  Switch to Redis **before** any
of the following:

- Running gunicorn with `--workers > 1`.
- Exposing port 5005 beyond localhost (LAN, VPN, reverse proxy).
- Adding external integrations that poll the REST API continuously.

---

## Checking the active backend at runtime

```bash
# Logs include the storage URI on startup (search for "Rate-limit storage")
tail -50 ~/Library/Logs/KrabEar/backend.log | grep -i "rate.limit"

# Quick sanity check via Redis CLI
redis-cli -u redis://localhost:6379/0 keys "LIMITER*" | head -10
```

---

## Related settings

| Env var | Default | Description |
|---------|---------|-------------|
| `KRAB_EAR_RATE_LIMIT_ENABLED` | `true` | Set `false` to disable entirely (tests, dev). |
| `KRAB_EAR_RATE_LIMIT_STORAGE_URI` | `memory://` | flask-limiter storage URI. |

See `KrabEar/core/config.py` and `KrabEar/backend/rest_server.py` (lines 98–134)
for implementation details.
