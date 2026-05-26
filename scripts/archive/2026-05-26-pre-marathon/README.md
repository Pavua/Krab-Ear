# Archived Scripts — 2026-05-26 pre-marathon

Archived per W810 audit (PR #736). These scripts were present in `scripts/` at the start of the
Wave 810 audit but are no longer active in any CI job, Makefile target, or live workflow.

Git history is fully preserved — `git log --follow scripts/archive/2026-05-26-pre-marathon/<file>`
to see the original commit trail.

## Autonomous-cycle cluster (9 scripts)

These files implemented a "self-driving sprint" concept from the initial baseline import (2026-02-15).
No CI, Makefile, or active tooling references them. The underlying Python helpers they invoke are
included in the same cluster.

| File | Reason archived |
|------|----------------|
| `run_autonomous_cycle.command` | Self-driving sprint runner; no CI/Makefile reference |
| `run_autonomous_hour.command` | Timed variant of above; stale reference in ARCHITECTURE-KRAB-EAR.md (removed W814) |
| `run_daily_driver_validation.command` | Daily-driver concept; superseded by CI pipeline |
| `run_sprint_prioritizer.command` | Sprint scoring runner; no active callers |
| `run_roadmap_self_update.command` | Roadmap self-update runner; no active callers |
| `run_regression_radar.command` | Regression-radar runner; no active callers |
| `roadmap_self_update.py` | Python driver for `run_roadmap_self_update.command` |
| `regression_radar.py` | Python driver for `run_regression_radar.command` |
| `score_roadmap_sprints.py` | Python driver for `run_sprint_prioritizer.command` |

Note: the root-level wrapper `.command` files (`Run Autonomous Cycle.command`, etc.) still exist in the
repo root — they delegate to these archived scripts. They are themselves candidates for a follow-up
cleanup wave.

## Self-deprecated install script (1 script)

| File | Reason archived |
|------|----------------|
| `install_agent.command` | Replaced by `install_agent_launchagent.command` (Wave 59). The old script contained a self-deprecation notice. |

## Superseded bench scripts (2 scripts)

| File | Reason archived |
|------|----------------|
| `r19_bench.py` | Superseded by `r21_bench.py` and `r22_bench.py`. Security note: previously contained a hardcoded LM Studio token (CRIT-1, fixed post-Wave 47) — now reads from env. |
| `r20_bench.py` | Same as above. Referenced in `docs/llm-conversion-guide.md` as historical reference only. |

## References updated by W814

- `docs/ARCHITECTURE-KRAB-EAR.md` — removed stale bullet for `scripts/run_daily_driver_validation.command`
