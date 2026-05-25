---
name: Wave 486-495 Phase B Wave 82 complete
description: 3 HIGH priority Wave 82 codes WIRED (Wave 490). ERROR_REGISTRY 51→54. ~410 total waves cumulative.
type: project
---

# Wave 486-495 (May 24)

## Headline
- **Wave 490**: 3 HIGH Phase B Wave 82 codes WIRED — production now visible:
  - `disk.critical` (separate from disk.low_space warn) — would have caught 0.22 GB silent emergency на 2026-05-22
  - `system.proc_cmdline_permission` — Sequoia KERN_PROCARGS2 blocked psutil errors теперь surface
  - `startup.stt_model_cache_miss` — Whisper HF cache miss flagged before user sees stall
- ERROR_REGISTRY: 51 → 54 (+3)
- Component Literal "startup" added

## Cumulative mega-marathon
- Round 1-10 + continuations: Waves 86-495
= **~410 total waves** across 12 calendar days

## State
- service.py: 5478 LOC, 86 active handlers (stable post Wave 65 convergence)
- 9 services extracted
- 10,864+ test methods / 404+ files
- ERROR codes: 54 (51 + 3 wired Wave 82) + 3 more Wave 82 candidates pending
- 0 flake8 warnings в production
- v2.0.3 SHIPPED; v2.0.4 ship-ready
- Sentry: 0 unresolved agent, 1 silent backend

## Remaining Wave 82 codes (3 candidates pending wire)
- stt.postprocess_drop (full transcript silently discarded before retry)
- rewriter.circuit_cascade (HALF_OPEN→OPEN storm dedup)
- stt.gigaam_longform_unavailable (combined dedup для padding_mismatch+hf_cache_miss double-toast storm)

## User action items (carried, top 5)
1. Merge PR #585 + worktree cleanup (~100 GB)
2. VPN plist fix
3. Disable macOS auto-update reboot
4. Ship v2.0.4 (Wave 364 checklist)
5. HF accept pyannote

## Next session recommended focus
1. Wire 3 remaining Wave 82 codes (Wave 491+)
2. Mega-merge train after Wave 470 + 490 CI runs settle (~50 PRs waiting)
3. CallAutomationController + GlobalStatusBar Unicode glyph fixes
4. v2.0.4 ship execution
