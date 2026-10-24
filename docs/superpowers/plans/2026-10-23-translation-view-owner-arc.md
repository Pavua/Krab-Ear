# Translation view owner: устранить цикл удержания callback

База origin/codex/krab-ear-v2 @3f08c9dc; isolated ear-translation-owner-1023.
Source-only scope: parent lazy getter weak view capture, один реальный ARC test.
Никаких моделей, IPC, capture, app/panel startup, lifecycle, визуала или изменения
SSE Task2 frozen b8da3189. Отдельно от Task3 lifetime/reconnect.

- [x] Confirm source: lazy associated view owns onToggleCapture closure; closure
  captures local view strongly even though self is weak. Timer/SSE view lifetime
  therefore may outlive controller. Actual runtime memory not measured.
- [x] Prepare test: compile unchanged production getter body in bounded Swift
  process with UI/capture leaf stubs; verify caching then release controller/view.
  Native capture leaf fatalError, no click/IPC/network/model effects.
- [x] RED: Main resource FINISH received; actual getter compiled, ARC_LEAK exit1 (8.6s). Voice acceptance remains separately HOLD.
- [x] Minimal fix: weak view capture + guard; preserve callback behavior while alive.
- [x] GREEN: same ARC regression, Python3.12/current PASS (~0.25s each), Ruff/diff PASS.
- [x] Independent read-only source review SOURCE PASS: no concrete defects in two-line fix or actual-getter ARC test.
- [ ] Full AppKit build/hosted CI not yet run; requires next coordinated source window.
- [ ] Publish/hosted only in next coordinated Ear source window.

Resource ruling: Main requested quiet cutover/voice window23.10; test execution
held until FINISH. Source/test files may be prepared without processes.

Resource update22.10 22:23Z: Ear tiny tests FINISH; bounded fulloffline slot passed
to Gateway. No real UI/capture/IPC/ML/network or runtime lifecycle affected.
