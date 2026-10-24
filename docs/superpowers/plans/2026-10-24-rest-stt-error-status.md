# REST STT: правдивая ошибка движка

Цель: ошибка словарного результата AudioEngine не превращается в HTTP200/status=ok.
База: origin/codex/krab-ear-v2 @ c44db7d9. Только source/offline, без runtime/model/lifecycle.

- [x] RED: Flask test client с fake transcriber: status=error и error без status дают503, безопасный stt_failed, без history/успешной metrics записи; общий и call-auto пути.
- [x] GREEN: проверить результат после privacy/deadline gate и до history/metrics. Не отдавать текст исключения, transcript или внутренние параметры.
- [x] Negative controls: штатная тишина/vad_skip и обычное распознавание сохраняют200; ошибки не подменяют privacy403.
- [ ] Узкие Python3.12/noMLX tests по файлам, audit-all, независимый whole-diff review; CI только hosted.
- [ ] Source/CI не закрывают disk-read root cause, nested drain или producer quiet. Никаких новых прогревов, повышения таймаутов, рестартов и смены моделей.

Review нашёл sibling privacy gap обычного профиля: новый503 не должен разрешать fallback при privacy, включившейся во время STT. Два дополнительных RED случая (toggle/read error) воспроизвели503вместо403; добавлен fail-closed recheck перед503. Live root cause и release/drain остаются вне source-доказательств.
