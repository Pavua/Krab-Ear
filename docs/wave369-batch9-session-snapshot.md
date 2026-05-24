---
name: wave-352-369-batch-9-post-v2-0-3-ship-hardening
description: Cross-cutting fixes + GigaAM padding bug discovery + dispatch invariant pattern formalization
metadata: 
  node_type: memory
  type: project
  originSessionId: 727793b5-af49-459e-9cc6-afa34e76ee01
---

# Wave 352-369 (2026-05-22)

## Headline accomplishments
- **2 production bugs found in single investigation** (Wave 358/359): GigaAM longform threshold + AudioChunker micro-advance — both 1-line fixes with regression tests
- **Cross-cutting pattern formalized**: dispatch table → _handle_X stubs vs direct delegation (12 PRs fixed по этому шаблону)
- **22 PRs merged** since 2026-05-21 (includes batch 8 tail + batch 9 opens)
- **v2.0.4 ship checklist** ready

## Bug count
| Wave | Bug | Impact |
|---|---|---|
| 358/359 | GigaAM longform threshold 24→30s | All 24-30s clips were broken |
| 358/359 | AudioChunker micro-advance creates 100s of 10ms chunks | Padding error на recordings с leading silence |
| 326 | Sentry options.dist never set | All events reported wrong version |
| 326 | release: nil at SDK init | Release tracking broken |
| 326 | No v2.0.3 release page in Sentry | events orphaned |
| 342 | sanitize_path relative traversal | Security: `../etc/passwd` from CWD bypassed |
| 306 | LM Studio Stream(gpu) mis-classified as timeout | Phase B metrics contaminated |

## Routine intel value
- backend-error-digest pre-aggregates → enabled Wave 358 GigaAM discovery (would never spot manually)
- agent-recovery.log + smoke-history.log → Wave 301 found AGENT-J was causing recovery FAILs all along
- VPN downstream complaints → Wave 316 identified macOS auto-update + KeepAlive=false as exact root cause

## Cumulative mega-marathon now
- Round 1: Waves 86-106 (20)
- Round 2: 107-146 (40)
- Bonus: 147-196 (50)
- Batch 4: 197-223 (25)
- Batch 5: 224-248 (25)
- Batch 6: 249-274 + v2.0.3 ship (26)
- Batch 7: 275-300 (26)
- Batch 8: 301-351 (50+)
- **Batch 9**: 352-369 (~18)
= **~280 waves total** multi-day session

## Production state
- v2.0.3 binary running, AGENT-J + K + M все fixed
- v2.0.4 ship-ready pending RecordingCoreService merge (#589)
- Sentry: 0 unresolved agent, 1 silent backend
- 6 services extracted; service.py <5000 LOC
- 10,200+ test methods
- 0 flake8 warnings в production code

## User action items (carried from Wave 316)
1. **VPN plist fix** (KeepAlive=true + RunAtLoad=true) → closes downstream complaints
2. **Disable macOS auto-update reboot** → eliminates daily 14:04 CEST reboot
3. **Install Krab Ear agent launchd plist** → auto-recover после reboots
4. **HF accept pyannote/speaker-diarization-3.1** → unblocks GigaAM longform
5. **v2.0.4 ship** when ready
