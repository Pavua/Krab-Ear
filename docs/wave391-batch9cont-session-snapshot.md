# Wave 352-391 Continuation Session Snapshot (2026-05-22)

> Batch 9 continuation — ~40 waves, 32 PRs merged since 2026-05-21

## Session summary

This is the continuation of Batch 9 (Waves 352–391), picking up where the
`wave369-batch9-session-snapshot.md` left off. Covers the morning session of
2026-05-22.

## Headline accomplishments

- **3 GigaAM/AudioChunker cascade bugs fixed**: Wave 358 (padding bug on 24–40s clips) →
  Wave 359 (partial fix: threshold 24→30s + AudioChunker micro-advance layer 1) →
  Wave 373 (complete fix: micro-advance layer 2)
- **Sentry dist:2.0.3 tracking fixed** (PR #588): 3 bugs at once — dist never set,
  release=nil at SDK init, no v2.0.3 release page
- **sanitize_path relative traversal fixed** (PR #592, Wave 342)
- **AGENT-M AppHang fixed** (PR #578, Wave 266): BackendToast.show AppHang,
  sister issue to AGENT-K
- **flake8 0 warnings** (was 17): chore PRs #580, #581
- **service.py audit v3** (Wave 380): 5476 LOC, 86 active dispatch, 6 services extracted
- **20-section production health observation script** (Wave 387)
- **v2.0.4 ship checklist ready** (Wave 364)

## PRs merged since 2026-05-21 (32 total)

| PR | Description |
|----|-------------|
| #513 | feat(wave163): regen contracts/schemas + drift CI guard |
| #514 | test(wave165): LLMRewriter deep tests |
| #516 | docs(wave167): BACKEND-J investigation |
| #517 | test(wave168): core/pipeline stage tests |
| #518 | test(wave169): Swift error handling + toast coverage |
| #520 | test(wave178): openwakeword_adapter unit tests |
| #521 | test(wave181): telegram_bridge unit tests |
| #522 | test(wave180): glossary_auto_learn unit tests |
| #523 | test(wave175): core/* tail coverage (mlx_lock etc) |
| #529 | refactor(wave173): TextProcessingService extraction |
| #530 | docs(wave177): Swift main+*.swift extensions audit |
| #539 | fix(wave188): Swift main+Bookmarks async IPC (AGENT-3) |
| #540 | docs(wave195): memory baseline diff — Wave 63 leak validated |
| #573 | test(wave253): audio_recorder lifecycle coverage |
| #574 | test(wave258): contracts module coverage |
| #576 | docs(wave260): Sentry sweep + AGENT-J post-fix verify |
| #577 | test(wave265): paste profile cache extraction + tests |
| #578 | fix(wave266): AGENT-M BackendToast.show AppHang |
| #579 | docs(wave275): dependencies + tests audit baseline |
| #580 | chore(wave280): marshmallow pin + tests flake8 cleanup |
| #581 | chore(wave285): final flake8 cleanup (17→0) |
| #582 | docs(wave290): CLAUDE.md sync post v2.0.3 |
| #583 | feat(wave295): Wave 65 batch 6 — 5 dead IPC handlers removed |
| #584 | investigate(wave306): LM Studio Stream(gpu,N) — Phase B Wave 81 |
| #586 | docs(wave316): VPN + reboot mitigation |
| #587 | docs(wave321): v2.0.3 Sentry verification |
| #588 | fix(wave326): Sentry dist:2.0.3 tracking |
| #590 | docs(wave336): Sentry dist verify |
| #591 | test(wave341): ipc_constants + sanitizer extras |
| #592 | fix(wave342): sanitize_path traversal fix |
| #593 | docs(wave351): batch 8 wrap |
| #594 | docs(wave358): GigaAM padding bug investigation |

## Production state at Wave 391

| Metric | Value |
|--------|-------|
| Binary | v2.0.3 (UUID FDAB353F) |
| Sentry agent | 0 unresolved |
| Sentry backend | 1 silent (BACKEND-J, rewriter.timeout) |
| service.py LOC | 5476 (peak 5777) |
| Services extracted | 6 |
| Test methods | 10,200+ |
| Test files | 385 |
| flake8 warnings | 0 |
| v2.0.4 status | ship-ready |

## Cumulative mega-marathon wave count

| Batch | Waves | Notes |
|-------|-------|-------|
| Round 1 | 86–106 (20) | |
| Round 2 | 107–146 (40) | |
| Bonus | 147–196 (50) | |
| Batch 4 | 197–223 (25) | |
| Batch 5 | 224–248 (25) | |
| Batch 6 | 249–274 (26) | v2.0.3 SHIPPED |
| Batch 7 | 275–300 (26) | |
| Batch 8 | 301–351 (50+) | |
| Batch 9 | 352–391 (~40) | this session |
| **Total** | **~315+ waves** | ~10 calendar days |

## Pending user actions

1. VPN plist: set `KeepAlive=true` + `RunAtLoad=true`
2. Disable daily macOS auto-updates: `sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false`
3. Rebuild + ship v2.0.4 (see Wave 364 checklist)
4. HF: accept `pyannote/speaker-diarization-3.1` gate
5. Optional: install Krab Ear agent launchd plist for auto-restart
