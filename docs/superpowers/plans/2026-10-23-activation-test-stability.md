# Стабильность проверки MainActor при активации адресата

**Goal:** отделить проверку yielding MainActor от задержек планировщика CI.
**База:** origin/codex/krab-ear-v2; отдельный worktree codex/ear-activation-test-1023.
**Scope:** только тесты PasteTargetActivationWiringTests; production-дедлайны не менять.
**Баны:** без runtime, запуска агента, моделей, restart/deploy, Main/VG исходников, общего WIP и git add -A.

- [x] RED: воспроизвести старую фикстуру с задержкой подтверждения 250 мс при бюджете 150 мс; heartbeat работает, paste отказан по deadline.
- [x] GREEN: дать тесту yielding запас 5 секунд; проверять heartbeat до paste и факт единственной вставки в исходный PID.
- [x] Сохранить существующие отдельные проверки отказа при таймауте и истёкшем бюджете.
- [x] Mutation: синхронное ожидание MainActor должно ломать yielding-тест.
- [ ] Проверить полный AppKit build и применимый CI Swift test pack, без запуска production executable.
- [ ] Независимый source review, frozen SHA, PR в v2 и terminal exact-head CI.

## Локальные доказательства

- Старый тест с дополнительной async задержкой подтверждения250ms: RED, paste отсутствует.
- Тот же delayed-confirmation сценарий с5s: GREEN,0.259s.
- Mutation настоящего алгоритма с блокирующим ожиданием: RED (3assertions), production source не менялся.
- Полный AppKit debug build и применимый CI pack:308PASS.
- Независимый read-only review:SOURCE PASS; production400ms и все safety-tests сохранены.
