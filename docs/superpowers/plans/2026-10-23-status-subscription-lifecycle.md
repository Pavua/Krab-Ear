# Жизненный цикл декоративного статуса панели

**Goal:** SSE app.status и heartbeat существуют только при видимой панели; EOF восстанавливает активную подписку.
**База:** codex/ear-sse-recovery-1022 (#2109); отдельная ветка codex/ear-status-subscription-1023.
**Architecture:** существующий SSESubscriptionTransport; видимость NSWindow через KVO isVisible и AppKit notifications, включая orderOut/close/minimize/reopen; callbacks слабые и отменяемые.
**Scope:** GlobalStatusBar.swift и компонентные тесты; Translation/capture/history не менять.
**Баны:** без runtime/restart/deploy/models, без правок Main/VG, без запуска production executable или git add -A.

- [x] Добавить узкую транспортную инъекцию без изменения старого lifecycle, воспроизвести RED: hidden owner создаёт stream; EOF не восстанавливается; deinit оставляет Timer.
- [x] Заменить private SSE session на существующий транспорт #2109, сделать start/stop идемпотентными.
- [x] Связать start/stop с видимостью окна, close, miniaturize и повторным показом; stop немедленно скрывает только декоративный статус.
- [x] На освобождении владельца отменять транспорт и invalidation heartbeat на main без удержания owner.
- [x] GREEN: controlled transport/window tests, stale callbacks/retry, ARC/Timer и живой loopback SSE EOF без production endpoints.
- [x] Независимый source review; full AppKit build + выбранные тесты по согласованному ресурсному окну.
- [ ] Frozen SHA, stacked PR к #2109 и exact-head terminal CI; после интеграции базы нужен retarget+повторный CI.

## Доказательства текущего блока

- RED: старый lifecycle с узкой инъекцией OS/network leaves —5тестов/9assertion failures, без production endpoints.
- Финальный узкий GREEN: 9/9 тестов, 1.774s: 8 lifecycle + существующий loopback transport. Проверены ARC окна/view, Timer/session invalidation, controlled miniaturize, настоящий offscreen NSWindow и доставка второго payload после EOF.
- Дополнительный настоящий offscreen NSWindow выявил отсутствие occlusion notification приorderFront. Прямой KVO isVisible:6PASS, включаяorderFront→orderOut→reopen→close.
- Source review первоначальноHOLD (visibility/ложное доказательствоidle), затемSOURCE PASS послеKVO и усиления теста второго payload; финальный тестовый diff проверен локально.
- Финальный full actual AppKit debug build-tests: exit 0, 15.76s (incremental). Расширенный CI-набор и затронутые GlobalStatusBar/SSE/Probe проверки: 339 PASS, 44.567s; без production executable, endpoints, clipboard или микрофона.
- Финальная release-сборка `swift build -c release --jobs 2`: exit 0, 48.76s.
- Production RSS/capture/модели не измерялись и не менялись.
