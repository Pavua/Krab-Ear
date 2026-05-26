# Wave 663 — GitGuardian Audit: PRs #575 + #535

**Date:** 2026-05-26  
**Scope:** PRs #575 (`test/wave263-telnyx-twilio-parity`) and #535 (`fix/wave187-rest-timing-attack`)

## Findings Summary

| PR | Pattern matched | Category | Verdict |
|----|----------------|----------|---------|
| #575 | `AC00000000000000000000000000ffff` | Twilio-style SID | **False positive — fixture** |
| #575 | `AC00000000000000000000000000aaaa` | Twilio-style SID | **False positive — fixture** |
| #575 | `test-fake-authtoken-do-not-use` | Auth token | **False positive — fixture** |
| #575 | `test-fake-tok-do-not-use-bbbbbb` | Auth token | **False positive — fixture** |
| #575 | `Bearer` (in test name / assertion) | Auth header check | **False positive — test assertion** |
| #535 | `test-fake-key-32ch-aabbccddeeff` | REST API key | **False positive — fixture** |
| #535 | `test-fake-restkey-abc` | REST API key | **False positive — fixture** |
| #535 | `mylegacykey` / `somekey` | Generic key | **False positive — fixture** |

## Detail

### PR #575 — Twilio/Telnyx parity tests

All flagged values are in `KrabEar/tests/test_call_provider_parity.py` (new file).

- `AC00000000000000000000000000ffff` / `AC00000000000000000000000000aaaa` — synthetic Twilio Account SIDs composed entirely of zeroes + `ffff`/`aaaa` hex padding. Not a real credential (real Twilio SIDs are 34 hex chars with random entropy; these are all-zero fillers).
- `test-fake-authtoken-do-not-use` / `test-fake-tok-do-not-use-bbbbbb` — strings contain explicit "fake" / "do-not-use" markers.
- `Bearer` occurrences are unittest assertion strings (`assertIn("Bearer", header)`), not credential values.

**No real secrets. No code change needed.**

Recommended GitGuardian annotation: add `# ggignore` comments on the two fixture factory lines to suppress future alerts.

### PR #535 — REST auth timing-attack fix

All flagged values are in `KrabEar/tests/test_rest_timing_attack.py` (new file).

- Keys are assigned to `settings.REST_API_KEY` inside `setUp`/`tearDown` with `orig` save-and-restore — standard unit test isolation pattern.
- Values like `test-fake-key-32ch-aabbccddeeff`, `test-fake-restkey-abc`, `mylegacykey`, `somekey` are ephemeral in-process assignments, never stored to disk or network.

**No real secrets. No code change needed.**

## Actions Taken

No code changes required — both PRs contain only test fixtures with no real credential entropy.

Recommended suppressions (optional, no PR needed):

```python
# PR #575 — KrabEar/tests/test_call_provider_parity.py
def _twilio(sid: str = "AC00000000000000000000000000ffff",  # ggignore
            token: str = "test-fake-authtoken-do-not-use") -> TwilioAdapter:
```

```python
# PR #535 — KrabEar/tests/test_rest_timing_attack.py
settings.REST_API_KEY = "test-fake-key-32ch-aabbccddeeff"  # ggignore
```
