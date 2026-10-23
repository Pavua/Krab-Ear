# TranslationStreamView: очистить таймеры при уничтожении

База: #2113 @19539cf2, отдельный worktree ear-translation-timers-1023.
Scope: только deinit cleanup capture/glossary Timer, без изменения hidden-panel,
capture ownership, SSE reconnect, моделей, настроек или runtime lifecycle.

- [x] Source: deinit отменяет SSE task, но оставляет оба repeating Timer в RunLoop.
- [x] RED: actual deinit body + real Foundation timers remain valid after owner release.
- [x] Minimal fix: detach timer references, invalidate on main queue without capturing self.
- [x] GREEN: same probe main/background release; existing actual getter ARC regression.
- [x] Independent read-only SOURCE PASS; actual full AppKit target compiled and6tail regressions PASS.
- [ ] Publish/hosted CI in next coordinated source block; this checkpoint remains local.

Probe compiles only production deinit body with minimal owner/task leaves. It does
not instantiate the real view, start capture, access IPC, network or models.
This is timer ownership evidence, not production RSS/Metal usage or call readiness.
Initial #2111–2113 hosted CI remains the active publication block; no fourth push yet.

Validation23.10: RED2 compiled TIMER_LEAK main/background; GREEN both release modes
and existing getter ARC on Python3.12. Ruff check/format and diff PASS.
Actual full AppKit target +6tail tests exit0; existing unrelated concurrency warnings.
No claim of measured production RAM reduction.
