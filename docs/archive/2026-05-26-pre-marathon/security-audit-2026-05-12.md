# Krab Ear — Security Audit 2026-05-12

**Auditor:** Claude (automated static analysis)
**Scope:** Python backend (`KrabEar/`), Swift agent (`native/`), scripts (`scripts/`), configs
**Branch:** `claude/zealous-williams-cdeb87`

---

## Critical

### CRIT-1 — Hardcoded LM Studio Bearer Token in Two Committed Scripts

**Files:**
- `scripts/r19_bench.py:23` — `LM_STUDIO_TOKEN = "sk-lm-***REDACTED***"`
- `scripts/r20_bench.py:28` — same token, identical value

**Status:** Both files are tracked by git (confirmed via `git ls-files`). The token is a real LM Studio API key committed in wave 43/44 (`a001795`, `e9f1aea`). It exists in git history.

**Risk:** Anyone with read access to the repository (or git history) can extract the token and call the local LM Studio endpoint if it is exposed on a network interface. If LM Studio is ever bound to `0.0.0.0` instead of `127.0.0.1`, this is a remote code injection vector.

**Fix:**
1. Rotate the token immediately in LM Studio (Settings > API > Regenerate key).
2. Replace the hardcoded value with an environment variable:
   ```python
   import os
   LM_STUDIO_TOKEN = os.environ.get("LM_STUDIO_TOKEN", "")
   ```
3. Add `scripts/r*_bench.py` to `.gitignore` **or** strip the token from git history via `git filter-repo --path scripts/r19_bench.py --invert-paths` (nuclear) or `git filter-repo --replace-text` with a substitution rule.
4. Add a pre-commit hook or CI check: `grep -rn "sk-lm-" --include="*.py" . && exit 1`.

---

## High

### HIGH-1 — REST Server Auth Disabled by Default (Port 5005 Unauthenticated)

**File:** `KrabEar/core/config.py:180`
```python
REST_API_AUTH_ENABLED: bool = False
REST_API_KEY: str = ""
```

**File:** `KrabEar/backend/rest_server.py:162` — the `require_api_key` decorator passes through when both flags are off.

**Risk:** Flask REST API on port 5005 is completely unauthenticated by default. Any process on localhost (or remote if macOS firewall is not blocking) can call `/transcribe`, `/metrics`, and other endpoints without credentials. The `@require_api_key` decorator exists but is a no-op out of the box.

**Fix:**
- Change default to `REST_API_AUTH_ENABLED: bool = True` and generate a random `REST_API_KEY` at first launch if none is set.
- Or at minimum document prominently that the REST server must never be exposed outside loopback without setting `REST_API_KEY`.

### HIGH-2 — IPC HMAC Signing Disabled by Default

**File:** `KrabEar/core/config.py:231`
```python
IPC_SIGNING_ENABLED: bool = False
IPC_SIGNING_SECRET: str = ""
```

**Risk:** The Unix socket at `~/Library/Application Support/KrabEar/krabear.sock` is correctly mode `srw-------` (user-only — GOOD). However, any process running as the same user can connect and issue arbitrary IPC commands (e.g., `set_settings`, `export_history_srt`, `start_call_assist`) without HMAC verification. This is a local privilege concern if untrusted code runs as the same user.

**Fix:** Enable `IPC_SIGNING_ENABLED = True` by default and auto-generate `IPC_SIGNING_SECRET` at first run (persist to `~/.krab_ear_data/ipc_secret` with mode 0600). Swift agent must read and send the same secret.

### HIGH-3 — Sensitive Keys Missing from `settings_backup.py` SENSITIVE Set

**File:** `KrabEar/backend/settings_backup.py:27-31`

Current `_SENSITIVE` frozenset covers: `voice_gateway_api_key`, `hf_token`, `rest_api_key`, `lm_studio_api_key`.

**Missing:**
- `telnyx_api_key` (Telnyx call API key)
- `twilio_auth_token` (Twilio credential)
- `twilio_account_sid` (Twilio SID — considered sensitive)
- `sentry_dsn` / `sentry_dsn_agent` (contains project DSN URL with auth token embedded)

**Risk:** Settings backups in `~/Library/Application Support/KrabEar/settings_backups/` are plain JSON files. If a backup is included in a crash report, iCloud sync, or shared with support, Telnyx/Twilio credentials and Sentry DSN leak.

**Fix:**
```python
_SENSITIVE: frozenset[str] = frozenset({
    "voice_gateway_api_key",
    "hf_token",
    "rest_api_key",
    "lm_studio_api_key",
    "telnyx_api_key",        # ADD
    "twilio_auth_token",     # ADD
    "twilio_account_sid",    # ADD
    "sentry_dsn",            # ADD
    "sentry_dsn_agent",      # ADD
})
```

---

## Medium

### MED-1 — HuggingFace Token Written to Environment Variables in Child Process

**File:** `KrabEar/core/pipeline/stt_gigaam.py:245-248`
```python
if hf_token:
    os.environ["HF_TOKEN"] = hf_token
    os.environ["HUGGING_FACE_HUB_TOKEN"] = hf_token
```

**Risk:** Setting env vars in the main process propagates them to all subsequently spawned child processes (subprocess, workers). On macOS, env vars are visible via `/proc/<pid>/environ` equivalents. The token is never cleared after use.

**Fix:** Clear env vars after the call, or pass the token via a temporary file/keychain reference. If the subprocess approach is mandatory, spawn a fresh process and let it inherit only the necessary env.

### MED-2 — Transcript Content Logged at DEBUG Level in Some Paths

**File:** `KrabEar/backend/service.py:1852`
```python
logger.debug("Не удалось emit realtime.final_transcript", exc_info=True)
```

This specific line is safe (only logs failure, not content). However, the Sentry breadcrumb pattern should be verified globally: `docs/IPC_API_REFERENCE.md` describes breadcrumbs as "privacy-respecting (no transcript text)". Spot-check confirmed the current code does not log raw transcript text. No immediate PII exposure detected, but this should be enforced via linter rule.

**Fix:** Add a lint rule (grep or flake8 plugin) that fails CI if any `logger.*` call in `KrabEar/backend/` or `KrabEar/core/` contains variable names matching `transcript`, `text`, `result` alongside log levels above DEBUG.

### MED-3 — `docs/llm-bench-results-R19.md` Tracked in Repo

**File:** `docs/llm-bench-results-R19.md` (new untracked file shown in git status, but confirmed tracked via `git ls-files`)

The file itself does not contain the raw token, but it references `LM Studio: http://localhost:1234/v1` and model names. Low sensitivity on its own, but its existence confirms the bench script ran with the leaked token.

**Fix:** Add `docs/llm-bench-results-*.md` to `.gitignore`.

### MED-4 — Accessibility API Grants Full AX Read/Write Scope

**Files:** `PasteService.swift`, `SelectionTranslator.swift`

`AXIsProcessTrustedWithOptions` grants Krab Ear the ability to read `kAXSelectedTextAttribute` from ANY focused application and write text back via `AXUIElementSetAttributeValue`. This is by design for paste functionality, but the scope is maximal — there is no per-app restriction in the macOS AX API.

**Risk:** If KrabEarAgent binary is compromised or exploited, an attacker gains keylogger-equivalent access to all UI text in all apps.

**Mitigation (not a fix, by design):** Ensure the binary is always codesigned with a stable identity (`Krab Ear Dev Local` per PR #235). Document this in the security model. Verify `AXIsProcessTrusted()` is only called when a recording/paste action is explicitly triggered (confirmed in current code — no background polling).

---

## Low

### LOW-1 — `.gitignore` Does Not Explicitly Exclude Bench Scripts

`scripts/r*_bench.py` files are not in `.gitignore`. The `.gitignore` only excludes `KrabEar/tests/benchmark_results.json` and `.benchmarks/`.

**Fix:** Add `scripts/r*_bench.py` or `scripts/*_bench.py` to `.gitignore` to prevent future bench scripts from being accidentally committed with hardcoded credentials.

### LOW-2 — Unix Socket Permissions Are Correct But Not Enforced on Creation

**Current state:** `srw-------` — correct, user-only.

The socket file permissions depend on the process `umask` at creation time. If the umask is permissive (e.g., 0022), the socket would be `srw-r--r--`.

**Fix:** In `IPCServer.__init__` or socket creation code, explicitly call `os.chmod(socket_path, 0o600)` after binding, regardless of umask.

### LOW-3 — `scripts/build_and_deploy.command` Contains No Credential Risk But Lacks Input Validation

**File:** `scripts/build_and_deploy.command` (new untracked file). Not yet committed. No hardcoded secrets found. Low risk as-is.

**Recommendation:** Before committing, verify the script does not accept external input that could be used for path traversal or command injection.

---

## Summary

| Severity | Count | Items |
|----------|-------|-------|
| Critical | 1 | CRIT-1: Hardcoded LM Studio token in 2 committed scripts |
| High | 3 | HIGH-1: REST unauthenticated; HIGH-2: IPC signing off; HIGH-3: Missing sensitive keys in backup |
| Medium | 4 | MED-1: HF token in env; MED-2: PII log risk; MED-3: bench result tracked; MED-4: AX scope |
| Low | 3 | LOW-1: gitignore gap; LOW-2: socket chmod; LOW-3: build script validation |

## Top 3 Immediate Recommendations

1. **CRIT-1 — Rotate the LM Studio token now** and strip `scripts/r19_bench.py` + `scripts/r20_bench.py` from git history or replace the hardcoded value with `os.environ.get("LM_STUDIO_TOKEN", "")`.

2. **HIGH-3 — Expand `_SENSITIVE` set in `settings_backup.py`** to include `telnyx_api_key`, `twilio_auth_token`, `twilio_account_sid`, `sentry_dsn`, `sentry_dsn_agent`. One-liner fix, no tests to update.

3. **HIGH-1 — Enable REST auth by default** (`REST_API_AUTH_ENABLED = True`) or auto-generate `REST_API_KEY` at first launch. The REST server on port 5005 is completely open to localhost by default.
