# W1 inventory — 2026-08-16

База: `codex/krab-ear-v2` @ `5a6559df`.
Снято: `2026-08-16T14:35:33+0200`.
Волна исполняется в worktree `.worktrees/w1-repo-hygiene` на `feat/w1-repo-hygiene`.

## Git

- local branches: 1813
- remote branches: 1626
- local audit*: 298
- remote audit*: 360
- open PRs besides #1875: none

## Worktrees

```
/Users/pablito/Antigravity_AGENTS/Krab Ear                                                       5a6559df [codex/krab-ear-v2]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.claude/worktrees/krab-ear-c3b-scratchpad-d7e2df      62ca747f (detached HEAD)
/Users/pablito/Antigravity_AGENTS/Krab Ear/.claude/worktrees/stoic-haibt-629bc7                  b849bfcd [claude/quirky-dewdney-fe7d13]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.claude/worktrees/zealous-shaw-157a1d                 2acd9de8 (detached HEAD)
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/call-assist-lifecycle-20260720             54a1a1d8 [codex/call-assist-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/ci-bash32-chunk-guard-20260722             a488d1b9 [codex/ci-bash32-chunk-guard-20260722]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/conversation-audio-rate-contract-20260720  8649da18 [codex/conversation-audio-rate-contract-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/conversation-generation-hotkey-20260720    63cdc03b [codex/conversation-generation-hotkey-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/conversation-hotkey-lifecycle-20260720     ed0fa005 [codex/conversation-hotkey-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/fallback-signal-hardening-20260720         2d5b2d39 [codex/fallback-signal-hardening-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/gigaam-cache-fingerprint-20260720          94807694 [codex/gigaam-cache-fingerprint-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/gigaam-input-contract-20260720             1c244a88 [codex/gigaam-input-contract-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/gigaam-warmup-lifecycle-20260720           b757869f [codex/gigaam-warmup-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/history-encryption-atomic-20260720         546a6898 [codex/history-encryption-atomic-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/ipc-handler-lifecycle-20260720             100807c9 [codex/ipc-handler-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/live-subtitles-sse-lifecycle-20260720      193e9f7a [codex/live-subtitles-sse-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/meeting-session-lifecycle-20260720         93844e0a [codex/meeting-session-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/python-chunk-lifecycle-20260720            e5014f1e [codex/python-chunk-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/python-chunk2-lifecycle-20260720           0fbe6d1d [codex/python-chunk2-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/r2-recording-ownership-20260725            94c5a57e [codex/r2-recording-ownership-20260725]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/rest-timeout-process-safety-20260720       d25b5bb0 [codex/rest-timeout-process-safety-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/rest-upload-dir-resilience-20260720        a5180b24 [codex/rest-upload-dir-resilience-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/shutdown-orchestration-20260720            a6e1f15b [codex/shutdown-orchestration-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/single-instance-hardening-20260720         5dc0586b [codex/single-instance-hardening-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/sse-protocol-hardening-20260720            9d37458c [codex/sse-protocol-hardening-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/swift-defaults-isolation-20260720          af002984 [codex/swift-defaults-isolation-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/swift-monitor-isolation-20260720           416d8c5e [codex/swift-monitor-isolation-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/system-test-optin-20260720                 dd43fef9 [codex/system-test-optin-20260720]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/user3-recording-rescue-20260722            260c9341 [codex/user3-recording-rescue-20260722]
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/w1-repo-hygiene                            feat/w1-repo-hygiene (эта волна)
/Users/pablito/Antigravity_AGENTS/Krab Ear/.worktrees/wake-word-watchdog-lifecycle-20260720      ff4794b9 [codex/wake-word-watchdog-lifecycle-20260720]
/Users/pablito/Antigravity_AGENTS/KrabEar_gigaam_mlx_wave_20260731                               2a57431f [claude/llm-warmup-gate]
```

Кандидаты на поздний prune (НЕ удалять в этой волне): каталоги
`.worktrees/*` с датой 20260720–20260725 в имени, если
`git -C <path> status --short` пуст И
`git -C <path> log origin/codex/krab-ear-v2..HEAD --oneline` пуст.

Не трогать: основной checkout `Krab Ear`, любой worktree с dirty status,
`KrabEar_gigaam_mlx_wave_20260731` (чужой путь/ветка).

## Issues

- open `ci: *`: 55
- keep open: #1909 (chronic local hang), #1919 (ubuntu 2026-08-13 unique files)
- close candidates: все остальные open с заголовком
  `ci: test suite failure — YYYY-MM-DD` или `ci: flaky test —`

## Scheduled task

Patched `~/.claude/scheduled-tasks/krab-ear-test-health/SKILL.md` (backup `/tmp/krab-ear-test-health.SKILL.md.bak`).
Issue title pattern `ci: test suite failure — DATE` retired.

## Untracked (не коммитить)

- `wake_word_models/hard_negatives_raw/tts_phrases.json`
