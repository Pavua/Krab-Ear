# Runner health: HTTP errors without credentials in logs

База origin/codex/krab-ear-v2 @3f08c9dc; isolated ear-runner-log-redaction-1023.
Scope: безопасные exception summaries Telegram/GitHub в runner health checker.
Реальные Telegram/GitHub requests, credentials, runtime logs/config/lifecycle не трогать.

- [x] Source: send_telegram logs str(HTTPError), HTTPStatusError includes token-bearing URL.
- [x] RED: synthetic errors and fully mocked network; secret marker absent from logs.
- [x] Minimal shared summary helper: exception type + HTTP status, no raw message/URL/body.
- [x] GREEN: Telegram status/request errors and GitHub main error exit2; normal alert contract.
- [x] Independent adversarial read-only SOURCE PASS; no concrete remaining security defects in diff.
- [ ] Publish/hosted CI in next coordinated Ear window after Gateway; runtime not updated.

Old runner-health exit2 report is not attributed to this source issue. Read-only
launchctl showed last exit0; real stderr logs were not read and credentials not rotated.
Three earlier Ear PR hosted CI runs remain active; this checkpoint not published yet.

RED3 synthetic marker exposures → GREEN3 currentPython and minimal ephemeral
Python3.12/httpx environment. All network functions mocked; no real outbound
messages, credentials or raw logs accessed. Ruff check/format(test) and diff PASS.
