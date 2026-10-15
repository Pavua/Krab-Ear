# Фоновый status item: одинаковый tick без AppKit churn

> Исполнение: superpowers:executing-plans inline; тесты, merge и deploy Ear
> разрешены владельцем. Продуктовый визуал не менять.

**Goal:** одинаковый health-tick не создаёт NSImage и не меняет NSStatusButton
каждую секунду, сохраняя видимые переходы и восстановление временного title.

**Evidence:** пользователь не связывает AppHang с действиями UI. В живом
sample 2026-10-16 main в основном ждёт события, но есть NSSceneStatusItem
commit; production applyHealthStateToStatusItem раз в секунду безусловно
создаёт image, меняет image/title и тут же вызывает refreshStatusItemTitle.
Это доказанный лишний путь перерисовки, но причинность пяти исторических
AppHang пока UNKNOWN. Sentry issues не закрывать по unit-тесту.

**База:** origin/codex/krab-ear-v2,6f07569c. Отдельный worktree
/Users/pablito/.codex/worktrees/ear-status-idle-20261016/Krab Ear.
**Баны:** canonical dirty WIP, Main/VG, OrbStack/VPN и их конфигурации не
менять; не запускать второй KrabEarAgent; не менять цвета/шрифты/layout;
не включать REST_IN_PROCESS; PR1875 запрещён; не грузить LM Studio/models;
реальный lifecycle только после fresh idle/quiet и exact-SHA CI.
**Tech Stack:** существующие AppKit/XCTest, без новых зависимостей.

### Task1: RED на настоящем callback

Files: native/KrabEarAgent/Tests/KrabEarAgentTests/StatusItemIdleRefreshTests.swift.
Создать AgentAppDelegate без startup, isolated UserDefaults и скрытый
NSStatusItem. KVO настоящего NSStatusButton.image должен увидеть первый
render, но ноль setter-вызовов при десяти идентичных ticks. Запись должна
включить image; повторное recording-состояние тоже не перерисовывать.
Внешний synthetic title должен восстановиться следующим tick.

- [ ] `swift test -c debug --skip-update --disable-automatic-resolution --jobs 2 --filter StatusItemIdleRefreshTests`.
  До фикса FAIL: repeated image setter writes >0.

### Task2: минимальный gate и GREEN

Files: main+HealthMonitor.swift; новый StatusItemRefreshGate.swift.
Хранить per-delegate gate через associated object; ключ Equatable содержит
health/recording/processing/preset/privacy/severity/opacity/appearance.
Snapshot после render хранит weak button, title, image identity, imagePosition.
Точный повтор ключа И наблюдаемого результата пропускает только тяжёлые
button/image операции. StatusIndicatorView синхронизировать отдельно.
Ключ либо результат изменился — исполнить прежний callback целиком,
записать новый snapshot. Визуальный результат и переходы сохраняются.

- [ ] GREEN новой регрессии + StatusIndicatorViewTests, StatusBarTooltipTests,
  MainHealthMonitorWiringTests, QuickPresetsCycleTests, BookmarksAsyncOffloadTests.
- [ ] `swift build -c release --skip-update --disable-automatic-resolution --jobs 2`;
  `git diff --check`; применимые Python Swift-source guards и hosted macOS CI.
- [ ] Independent whole-diff gate; PR с explicit paths; exact-head и merge-SHA CI.
- [ ] Перед выпуском согласовать общий Ear maintenance window с Main/VG;
  post-deploy PID/SHA/health и UI smoke. Исторический AppHang остаётся UNKNOWN
  до воспроизведения или достаточного нового runtime evidence.

Примечание harness: XCTest в release не видит существующие #if DEBUG хуки соседних тестов. Тесты запускать в debug, production build — release. Test hooks не менялись.
