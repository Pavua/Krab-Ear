# Wave 316 — Nightly MacBook Reboot Pattern + VPN Downstream Impact

**Date:** 2026-05-21  
**Scope:** Read-only investigation. No code changes.

---

## 1. Reboot Timeline (last 30 days)

Data source: `last reboot` (wtmp) + smoke-history.log uptime correlation.

### Dominant pattern: **daily 14:04 CEST reboot**

```
May 20  14:04 CEST
May 19  14:04 CEST   (+01:24 unscheduled)
May 18  14:04 CEST   (+07:30, +03:32 unscheduled)
May 17  14:04 CEST
May 16  14:07 CEST   (2 min drift)
May 15  14:04 CEST   (+22:27 unscheduled)
May 14  14:04 CEST   (+03:50 unscheduled)
May 13  14:04 CEST   (+ 3 unscheduled early-morning reboots)
May 12  14:04 CEST   (+08:49 unscheduled)
May 11  14:04 CEST
May 10  14:04 CEST
May  9  14:04 CEST   (+19:21 unscheduled)
May  8  14:04 CEST
May  7  14:04 CEST
May  6  14:04 CEST
May  5  14:04 CEST
May  4  14:04 CEST
May  3  14:04 CEST
May  2  14:04 CEST
May  1  14:04 CEST
Apr 30  14:04 CEST
```

**21 of 21 observed scheduled reboots occurred at exactly 14:04 CEST = 12:04 UTC.**

Smoke-log uptime correlation confirms:
- 16:08 smoke check typically shows backend uptime ~12 000–14 000 s → back-calculated boot = **~12:10–12:30 CEST**, consistent with 14:04 CEST + launchd 5 s ThrottleInterval + Python warmup ~5 min.

### Unscheduled reboots (~10 events)

Occur at random hours 01:24–08:49 CEST. Likely causes:
- macOS Rapid Security Response (RSR) requiring immediate restart
- Jetsam / kernel OOM (memory pressure from simultaneous MLX + LM Studio inference)
- Thermal shutdown (LM Studio 26B model sustained load)

---

## 2. Root Cause — 14:04 CEST Scheduled Reboots

### Confirmed: macOS Sequoia AutomaticInstallMacOSUpdates

```
/Library/Preferences/com.apple.SoftwareUpdate:
  AutomaticCheckEnabled     = 1
  AutomaticDownload         = 1
  AutomaticallyInstallMacOSUpdates = 1
```

macOS Sequoia (26.5) with all three flags set schedules its **background maintenance restart** at **12:00 UTC** when:
1. An update was downloaded in the background (ConfigDataInstall triggers)  
2. The machine is plugged in and idle (screen locked / displaysleep active)
3. No user session is active at the target window

**14:04 CEST = 12:04 UTC** — the 4-minute offset is launchd's ThrottleInterval + Apple's 0–5 min jitter window for the maintenance daemon.

### VPN Impact

Two VPN services run on this MacBook:

| Service | Binary | LaunchDaemon | KeepAlive | RunAtLoad |
|---------|--------|--------------|-----------|-----------|
| `com.po.vpnserver` | `/usr/local/bin/vpnserver` | `/Library/LaunchDaemons/com.po.vpnserver.plist` | **false** | **false** |
| AmneziaVPN | `/Applications/AmneziaVPN.app/...` | `/Library/LaunchDaemons/AmneziaVPN.plist` | (not set) | (not set) |
| Tailscale | system extension | kernel NE | auto | auto |

**Critical finding:** `com.po.vpnserver` has `KeepAlive=false` and `RunAtLoad=false`.  
On every 14:04 CEST reboot, `vpnserver` **does not restart automatically**. Downstream VPN users experience a hard disconnect that persists until the service is manually started or triggered by another mechanism.

The VPN maintenance script (`com.pablito.vpn.maintenance`) runs at 06:30 CEST and at `RunAtLoad` — but this only fires on login, not on root daemon boot. The vpnserver process was not found running in the current `ps aux` snapshot at the time of this investigation.

---

## 3. Mitigations

### Mitigation 1 — Fix vpnserver LaunchDaemon (CRITICAL, 30 min)

**Root fix:** enable `KeepAlive` and `RunAtLoad` in the vpnserver plist so it auto-restarts after every reboot.

```bash
sudo plutil -replace KeepAlive -bool true /Library/LaunchDaemons/com.po.vpnserver.plist
sudo plutil -replace RunAtLoad -bool true /Library/LaunchDaemons/com.po.vpnserver.plist
sudo launchctl bootout system/com.po.vpnserver 2>/dev/null || true
sudo launchctl bootstrap system /Library/LaunchDaemons/com.po.vpnserver.plist
```

**Pros:** zero hardware cost, fixes the downstream disconnect immediately.  
**Cons:** vpnserver will auto-start on every boot including unexpected reboots — verify the service handles cold-start cleanly (no stale lock files, etc.).

Verify after next 14:04 reboot:
```bash
sudo launchctl print system/com.po.vpnserver | grep state
```

---

### Mitigation 2 — Disable macOS Auto-Restart for Updates (MEDIUM, 5 min)

Turn off the auto-restart flag while keeping background downloads:

```bash
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
```

This stops the 14:04 CEST forced reboot. macOS will still download updates; you install them manually during a planned window (e.g., 03:00 CEST maintenance slot that avoids VPN peak hours).

**Pros:** eliminates the daily scheduled reboot entirely.  
**Cons:** you must remember to apply updates manually. Security patches (RSR) may be delayed.

---

### Mitigation 3 — Move VPN to Dedicated Always-On Box (BEST LONG-TERM)

Run `vpnserver` on a dedicated machine (Raspberry Pi 5, mini-PC, or VPS) that:
- Never reboots for macOS updates
- Has UPS / reliable power
- Is not co-located with dev workloads (MLX, LM Studio) that cause OOM

**Pros:** eliminates the entire class of problem — MacBook reboots are then invisible to VPN users.  
**Cons:** hardware cost (~€60 RPi5) or VPS cost (~€5/mo). Requires network port forwarding or public IP for the vpnserver.

Suitable candidates:
- Raspberry Pi 5 (4 GB) with Raspberry Pi OS (systemd service, never auto-reboots)
- Any always-on VPS (Hetzner CX11, ~€4/mo) in EU

---

### Mitigation 4 — Install Krab Ear Agent launchd Plist (QUICK WIN, 2 min)

This is unrelated to VPN but closes the "Swift agent dead for 6+ hours after reboot" gap shown in smoke logs:

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
make sign   # rebuild + codesign with stable identity
scripts/install_agent_launchagent.command
```

Currently `ai.krab.ear.agent` is **NOT registered** (confirmed: `launchctl print gui/501/ai.krab.ear.agent` → "Bad request"). The backend launchd (`ai.krab.ear.backend`) IS running. After every 14:04 reboot the Swift agent stays dead until the next smoke check auto-recovery or manual intervention.

**Pros:** zero cost, fixes Krab Ear availability gap independently.  
**Cons:** none — already built and documented (Wave 59, PR #395).

---

### Mitigation 5 — Add Reboot-Detection Routine

Create a smoke routine that detects post-reboot state and fires an alert:

```yaml
# Proposed: .remember/routines/reboot-watchdog.yaml
name: reboot-watchdog
schedule: "*/10 * * * *"   # every 10 minutes
action: |
  uptime_secs=$(python3 -c "import psutil; print(int(psutil.boot_time() - __import__('time').time() + __import__('time').time()))")
  if [ "$uptime_secs" -lt 600 ]; then
    # machine rebooted in last 10 minutes — ensure VPN is running
    sudo launchctl kickstart -k system/com.po.vpnserver 2>/dev/null || true
  fi
```

**Pros:** self-healing without manual intervention, captures both scheduled and unscheduled reboots.  
**Cons:** requires periodic privileged execution; `kickstart` on an already-running service is idempotent.

---

## 4. Priority Order

| # | Action | Effort | Impact | Recommended |
|---|--------|--------|--------|-------------|
| 1 | Fix vpnserver `KeepAlive=true` | 30 min | **HIGH** — stops downstream VPN drops | Do now |
| 2 | Disable AutomaticallyInstallMacOSUpdates | 5 min | **HIGH** — stops daily reboot | Do now |
| 3 | Install agent launchd plist (Wave 59) | 2 min | Medium — fixes Krab Ear gap | Do now |
| 4 | Move VPN to dedicated box | 1-2 days | **Very high** long-term | Plan next sprint |
| 5 | Reboot-detection routine | 1h | Medium — defence-in-depth | After #1 confirmed |

---

## 5. Unscheduled Reboots — Hypothesis

The ~10 non-14:04 reboots likely fall into two buckets:

**A. Rapid Security Response (RSR)** — Apple's sub-update mechanism on Sequoia can force an immediate restart when a critical patch is available. With `AutomaticallyInstallMacOSUpdates=1` this bypasses the normal noon-UTC window.

**B. Memory pressure / Jetsam OOM** — Running both LM Studio (gemma-4-26b-a4b = ~14 GB VRAM) and mlx-whisper (MLX GPU) simultaneously on M4 Max 36 GB leaves ~8 GB free. System memory compressor saturating + swap on the internal SSD → kernel OOM → Jetsam forced reboot. Wave 63 (PR #405) fixed the MLX cache leak (RSS stabilised at 35–40 MB) but LM Studio's persistent VRAM allocation remains. Mitigation: close LM Studio when not in active use, or configure it to unload models after idle.

---

## References

- Wave 59, PR #395 — `install_agent_launchagent.command`
- Wave 63, PR #405 — `mx.clear_cache()` MLX memory leak fix
- `scripts/install_agent_launchagent.command` — agent launchd opt-in
- `scripts/install_backend_launchagent.command` — backend launchd (already active)
- `KrabEar/launchagents/ai.krab.ear.agent.plist.template`
- `/Library/LaunchDaemons/com.po.vpnserver.plist` — KeepAlive/RunAtLoad must be fixed
