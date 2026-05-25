# Wave 301-351 Batch 8 Session Snapshot

**Date**: 2026-05-21  
**Waves**: 301-351 (~50 waves)  
**New PRs**: #576, #584, #586, #587, #588  
**Basis**: post v2.0.3 ship (Wave 274, commit 018581c)

---

## Headline accomplishments

### RecordingCoreService extraction (Wave 331, PR #589)
- -833 LOC from `backend/service.py` (5821 → 4988)
- Largest single refactor of the batch
- 6 services extracted total: CallSession, AudioAnalytics, Vocabulary, Reporting, Integration, RecordingCore

### Sentry dist:2.0.3 tracking fixed (Wave 326, PR #588)
- 3 bugs fixed simultaneously: `options.dist` never set, `release=nil` at call sites, no release page
- Both `krab-ear-backend` and `krab-ear-agent` release pages created
- Pending: rebuild Swift agent binary for dist tag to appear in events

### VPN root cause identified (Wave 316, PR #586)
- 21/21 nightly reboots at exactly **14:04 CEST** → macOS `AutomaticallyInstallMacOSUpdates` cron window
- VPN plist has `KeepAlive=false` → does not auto-restart after reboot
- 3 actionable fixes documented (see user action items)

### LM Studio "Stream(gpu, N)" chronic fixed (Wave 306, PR #584)
- 12× errors previously mis-classified as `rewriter.timeout`
- New error code `rewriter.lm_studio_stream_gpu_lost` added (Phase B Wave 81)
- 2s retry logic added

### Security fix: sanitize_path (Wave 342, PR #592)
- Relative path traversal vulnerability closed in `InputSanitizer`
- Affected: any IPC method accepting file paths

### mlx_lm.server LaunchAgent disabled
- Closed 24/7 background drain of ~15 GB RAM (RotorQuant standalone process)

---

## Production state

| Item | Status |
|------|--------|
| Sentry backend unresolved | 0 (BACKEND-J reclassified post-PR #584) |
| Sentry agent unresolved | 0 |
| test methods | ~10,200+ |
| flake8 warnings | 0 |
| services extracted | 6 |
| service.py LOC | <5000 (4988) |
| ERROR_REGISTRY codes | 48+ |
| v2.0.3 binary | shipped (AGENT-J + AGENT-K + AGENT-M fixes) |

---

## Critical findings from routines audit

1. **agent-recovery.log FAILs** since 2026-05-16: AGENT-J crash-on-startup prevented Swift agent from registering before pgrep check. Fixed in v2.0.3.
2. **Daily 14:04 CEST reboots**: macOS `AutomaticallyInstallMacOSUpdates` fires in background. VPN `KeepAlive=false` means VPN drops and doesn't recover.
3. **LM Studio Stream(gpu, N)**: 12× per-day error was GPU context loss, not model timeout. Root cause = Metal memory pressure during concurrent MLX + LM Studio inference.
4. **pyannote VAD model still gated**: requires manual accept at `hf.co/pyannote/speaker-diarization-3.1` to unblock GigaAM longform.

---

## Cumulative marathon totals

| Batch | Waves | PRs |
|-------|-------|-----|
| Round 1 | 86-106 | ~20 |
| Round 2 | 107-146 | ~40 |
| Bonus | 147-196 | ~50 |
| Batch 4 | 197-223 | ~25 |
| Batch 5 | 224-248 | ~25 |
| Batch 6 (v2.0.3 ship) | 249-274 | ~25 |
| Batch 7 | 275-300 | ~26 |
| **Batch 8** | **301-351** | **~5** |
| **TOTAL** | **~261 waves** | **~216 PRs** |

---

## User action items

1. **VPN plist fix** (top priority — closes downstream reconnection complaints):
   ```
   sudo nano /Library/LaunchDaemons/com.po.vpnserver.plist
   # Add: <key>KeepAlive</key><true/>  <key>RunAtLoad</key><true/>
   sudo launchctl bootstrap system /Library/LaunchDaemons/com.po.vpnserver.plist
   ```

2. **Disable macOS auto-update nightly reboot**:
   ```
   sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
   ```

3. **Install Krab Ear agent launchd plist** for auto-restart:
   ```
   scripts/install_agent_launchagent.command
   ```

4. **Rebuild + restart Swift agent** after PR #588 merge → activates `dist:2.0.3` tag in Sentry events.

5. **Accept pyannote gate on HuggingFace**: visit `https://huggingface.co/pyannote/speaker-diarization-3.1` and accept terms → unblocks GigaAM longform transcription.
