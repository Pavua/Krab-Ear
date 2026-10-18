# Общая очередь input/clipboard: продолжение async paste

База ветки: `d4768539ce4177b8e23b19d645dd5ea3c8244804`, remote base
`origin/codex/krab-ear-v2` = `19124f51`. Исполнитель один, inline executing-plans.
Авторитетный контракт: `2026-10-16-paste-async.md` в предыдущем изолированном
worktree; пункты 1–6 сохраняются. Независимый финальный gate: Astra Ultra.

## Границы

Только source/tests/docs/commits в текущем изолированном worktree. Нет push,
merge/deploy/restart, пользовательского clipboard, реальных клавиш/AX writes,
микрофона, настроек или моделей. Чужой WIP не трогать. Тесты и сборки только
после ROOT GO; до него допустимы чтение и тестовый код. `git add` явными путями.

## Контракты

1. Paste/replay/delete/revision async; одна bounded очередь и lease для clipboard
   и input. Revision delete→insert под одной lease, deadline monotonic включает
   очередь. Подменяются только OS leaves, приватный named NSPasteboard.
2. Target/operation identity неизменяемы до await; captured dictation/QuickEdit A
   сохраняется после старта B. Streaming target выбирается для каждого принятого
   chunk; revision отказывает при другом владельце ранее вставленного текста.
   После await проверяются operation/target/secure/concealed/changeCount.
3. Clipboard записывается непосредственно перед CmdV. Explicit writers получают
   lease либо инвалидируют pending. Key-down всегда получает key-up. Outcome:
   noEffects/completed/partial/unknown, точное число backspace-пар. Partial/unknown
   запрещают полный fallback/redelete и paste_status=ok.
4. Единственный bounded off-main runner переиспользует AudioDuckingCommandRunner;
   сохраняет original target, не использует sync NSAppleScript/Process.waitUntilExit.
   Child удерживает lease до cleanup; cleanupUnconfirmed quarantines automation.
   Переданный AppleEvent с неопределённым исходом запрещает второй канал.
5. Streaming serial внутри generation. Final barrier возвращает immutable итог
   своей session; G1 completion не читает/сбрасывает G2. Delegate использует этот
   итог вместо общего didStreamThisRecording. Только noEffects разрешает fallback.
6. PasteTargetActivation behavior/13 тестов сохраняются. AX messaging bounded.
   SelectionTranslator copy/paste/restore в той же очереди, translation IPC вне
   lease, restore CAS changeCount. Устранение всех AppKit hangs не обещается.

### Task 1: Правдивое уведомление P3

Сначала actual handlePasteFailure + real PasteService с fake notification process
и private pasteboard: nil text и never_copy не обещают clipboard; concealed
refusal сохраняет буфер; successful copy вправе обещать запись.

RED/GREEN: из `native/KrabEarAgent`:
`swift test -j 1 --filter PasteFailureNotificationTests`.
Expected RED: 2 assertions nil/never_copy; successful/concealed controls PASS.
Минимальный production fix только после RED; отдельный commit после GREEN.
ROOT GO ограничен этим фильтром с `-j 1`; после коммита ресурсный checkpoint.
Heartbeat/queue tests, audit и release требуют отдельного GO.

Checkpoint 2026-10-18: P3 commit `b4aead38813aa4fb07e0dc3518009ad25edbf5ae`.
Actual handler/service RED: 4 теста, 2 ожидаемых assertion failures (nil/never_copy),
concealed/successful-copy controls PASS. После минимального fix тот же фильтр:
GREEN 4/4. Первая compilation 63.04s, incremental GREEN 7.60s; tests0.580/0.100s.
Логи и `p3-receipt.json` в owning ignored SDD workspace. Pending OS-leaf instrumentation
в этот коммит не включена. После P3 — STOP до отдельного resource GO для Task2.

### Task 2: Actual async service + общая lease

API: общий thread-safe `InputAutomationQueue`,
`acquire(deadline:) async -> Result<InputAutomationLease, InputAutomationFailure>`,
синхронные `release`, `invalidatePending`, `quarantine`. FIFO, максимум 8 ожидающих
и 1 активная операция. Lease берётся delegate ДО activator; service принимает её
либо самостоятельно получает ту же очередь. Истёкшие/отменённые tickets удаляются.

Обычная вставка: общий budget 5s. Revision: `4s + count * 35ms`, арифметика с
проверкой переполнения; deadline фиксируется до enqueue. В текущем source нет max
count (только `count > 0`), каждая backspace-пара занимает `15ms + 15ms`; поэтому
новый фиксированный 15s timeout незаметно обрезал бы длинную поддержанную revision.
Перед первым delete требуется remaining `>= count * 30ms + 150ms` для вставки,
иначе отказ noEffects до первого down. Scheduler/AX способны израсходовать запас
после preflight; тогда truthful partial + exact pairs, без полного fallback.
Key-up обязателен даже после deadline. Overflow/invalid count и queue-wait
insufficiency проверяются до effects.

RED matrix: heartbeat создаётся внутри первого held-state leaf; deletion heartbeat;
bounded queue full/deadline/cancel до effects; target/concealed/clipboard drift;
key-up после cancel; N/M partial deletion без повтора; живой child удерживает lease.
Команда: `swift test -j 1 --filter 'PasteService|InputAutomationQueue'`.
Expected RED: behavior assertion, не compile error. GREEN: та же команда.

### Task 3: Streaming/delegate/translator wiring

RED: actual delegate→service и controller→adapter→service; QuickEdit A после B;
revision единой lease; translator не вклинивается; G1→G2 immutable final outcome.
Команда: `swift test -j 1 --filter 'StreamingPaste|PasteHandling|PasteTargetActivation|DictationPasteTarget|SelectionTranslator'`.
Expected RED: соответствующие behavior assertions. GREEN: тот же фильтр.

### Task 4: Финальная проверка и передача

Swift affected groups + актуальный hosted filter, зависимые Python source tests,
`make audit-all`, `swift build -c release -j 2`; логи в owning ignored SDD workspace.
Full backend hosted CI запускает root после final diff. Root получает commits,
source seal, RED/GREEN logs, rulings и границы для независимого whole-diff review.
Local evidence не выдаётся за native acceptance. Push/PR только через root.

## Текущие rulings и границы проверки

После ROOT GO выполняются только узкие Swift filters с `-j 1`; source/test
заморожены на весь compiler/linker/test process. Release/full backend/audit-all
требуют отдельного ресурсного допуска root. Прежние compile/ABI диагностики
не считаются causal RED. Новая queue API получила postimplementation coverage,
а heartbeat, translator yield/CAS, concealed-after-copy и own-write edge chain
имеют отдельные behavior RED.

Legacy `didStreamThisRecording` после explicit reset отражает новые effects:
неудачная revision без effects его не выставляет; partial/unknown попытка —
выставляет. Авторитетный immutable final outcome при ранее вставленных chunks
остаётся partial и запрещает полный fallback независимо от legacy property.
G1 update меняет activity только при сохранении того же owner у controller.

Clipboard continuity — bounded цепь собственных `(before, after)` edges плюс
проверка generation. User copy, затем собственная вставка не способны связать
старый snapshot через пользовательский gap. Explicit own copy инвалидирует
pending и не добавляет automation-safe edge. Переполнение 64 edges приводит
к безопасному отказу старых операций; очередь ограничена 8 waiting + active,
включая concurrent admission при отсутствии active.

Подтверждение пары CG down/up означает отправку пары в captured PID, а не
физическую доставку текста. После await отмена/target/focus/secure/clipboard
проверяются повторно; необязательный collapse при drift пропускается. Nil
injected AX facts не вызывают настоящий AX. Backspace pairs считаются точно.
AppleEvent failure после запуска консервативно unknown: второго канала нет,
cleanupUnconfirmed закрывает automation queue до перезапуска приложения.

SelectionTranslator использует ту же очередь для AX/copy/write/restore, IPC вне
lease. Target process object удерживается до await, перед записью сверяется
captured AX element и secure role; clipboard restore только CAS. Изменение
concealed после copy не уходит в IPC. Явные UI writers инвалидируют pending.
QuickEdit presenter/activation factory/frontmost/audio — точечные OS leaves с
прежними production defaults; actual delegate/service/controller не подменяются.

Harmless own child test отдельно разрешён root: System runner запускает только
свой `/bin/sh` → `exec /bin/sleep`, проверяет bounded fixture PID и cleanup;
нет osascript, реальных key events, AX writes или пользовательского pasteboard.

## Финальный source checkpoint перед независимым gate

Оба hosted workflow выбирают behavioral input tests вместе с wiring/source
контрактами. Локально exact hosted regex: **293/293 PASS** (build7.35s,
tests17.249s, `hosted-sealed-green.log`). Отдельный существующий filter
`SelectionTranslator.*Tests|PasteHandlingTests|DictationPasteTargetTests`:
**56/56 PASS** (`existing-sealed-green.log`). Этот корректный filter учитывает
раздельные классы SelectionTranslator; прежний `SelectionTranslatorTests` их
не выбирал и доказательством покрытия не считается.

Последние whole-path causal RED→GREEN:

- queued delegate snapshot → explicit copy: отказ ДО activation, затем public
  autopaste refusal не копирует stale text поверх нового пользовательского clipboard;
- always_copy+autoPaste+QuickEdit не пишет до accept; clipboard-only always_copy
  остаётся рабочим контрольным сценарием;
- runner.success после copy/target/cancel/secure/deadline drift: unknown,
  подтверждённые script backspace pairs сохранены, второго канала и replay record нет.

Автоматический failure path теперь history-only. Только явный отдельный copy
handler может обещать clipboard после фактической успешной записи (P3 controls).
Перед activation проверяются lease/cancel/total deadline/epoch/continuous edges
и original target liveness; foreground до activator не требуется. После него
service проверяет полный набор условий; activation budget ограничен общей lease.

`make audit-all` PASS был получен ДО единственной последней Swift поправки
post-runner-success validate. После неё выполнены final hosted/existing и
`make audit-dead-swift`: selftest PASS, scanner exit0, мёртвых0. Остальные
Python static scanners не зависят от этой ветки Swift success outcome.
Pure Python source/settings: 47/47 PASS, dev Python3.14, без BackendService
warmup fixtures/inference. Нефатальный pyannote/torchcodec import warning не
исправлялся в этой source волне. Release `swift build --package-path native/KrabEarAgent -c release -j 1`: PASS,
38.30s, `release-build.log`.

Остались внешний whole-diff gate Astra Ultra и hosted CI точного нового SHA
через root. Native acceptance, runtime/deploy/restart и устранение всех AppHang
не заявляются. Source/test после final success-validate больше не меняются до
независимого review; docs/ignored ledger только фиксируют результаты проверок.

## Независимый review: CG outcome после key-up

Astra подтвердил P2: CG success ветка игнорировала post-await отказ и записывала
replay record. Actual service matrix copy/target/secure/cancel/deadline после down:
causal RED 2 tests/16 failures (`cg-postawait-red.log`), GREEN 2/2. После
обязательного up отказ даёт unknown/ok=false, точные backspace pairs, без collapse,
recordLastPaste и полного delegate fallback. Bounded sleep только fake OS leaf
моделирует истечение deadline, не доказывает latency реального OS.

Проверки этой поправки: hosted 294/294, existing affected 56/56, release PASS
(`cg-hosted-green.log`, `cg-existing-green.log`, `cg-release-build.log`).
Остальные подтверждённые замечания streaming acceptance/terminal fallback,
Undo queue и translator success будут отдельными последовательными срезами.

## Независимый review: accepted streaming input

Общая FIFO теперь синхронно reserve при acceptance partial/final, затем awaitReady
после previous. Snapshot/target/deadline принадлежат accepted request. Hidden tail
не создаётся после admission refusal; повторный final использует тот же barrier.
Deadline консервативно покрывает checked revisionBudget(max ранее accepted length),
не создаётся заново после tail. Неизменившийся/backoff запрос снимает ticket.
Caller cancel снимает только reserved ticket, активную lease удерживает service
до key-up/cleanup. Final ждёт previous даже при admission refusal, агрегирует
prior effects, сохраняет terminal reason и явно запрещает fresh fallback.
Disabled streaming оставляет ordinary dictation paste (положительный контроль).

Actual controller/adapter/service/public delegate: RED3/11 assertions; cancellation
RED1/2. GREEN47/47 (`stream-reserved-green.log`): включая FIFO8+active при16
chunk callers, accepted final tail deadline, prior completed→final busy→partial,
copy before execution/за partial и public failed status без full repeat. Новые
reservation API edge tests добавлены после реализации, не заявляются fail-before.

## Независимый review: Undo — владелец общей input очереди

Actual Undo OS-leaf seam сначала сохранил старые sync пары/usleep и воспроизвёл
вставку CmdZ между revision backspaces, а также обход quarantine: RED1/2 assertions
(`undo-queue-red.log`). Затем Undo подключён к тому же PasteService и shared
FIFO через synchronous reserve, captured target/snapshot/deadline до Task. Unknown
target больше не разрешает HID tap в произвольный frontmost. Guard/modifier wait,
mandatory key-up и truthful unknown после down использует actual service. Clipboard
и replay record Undo не меняет. Interim seam удалён после wiring; OS defaults CG
остаются в единственном factory. GREEN12/12 (`undo-queue-green.log`). Тест находится
в уже выбранном обоими CI workflow InputAutomationIntegrationTests.

## Независимый review: translator outcome и финальная перепроверка

Actual translator с private pasteboard, fake AX/key leaves и notification runner:
causal RED2/7 assertions (`translator-outcome-red.log`) подтвердил ложный success
после copy/target/secure/epoch/cancel/deadline и ускоренный restore при cancellation.
После mandatory up и перед HUD проверяются lease/deadline/target/focus/epoch/count.
CAS restore сохранён; parent cancellation не сокращает bounded1s grace чтения
clipboard. После deadline/quarantine restore не выполняется. No-success при drift,
нет повтора input. Положительный контроль без drift продолжает показывать success.
Дополнительные Undo tests подтверждают immutable target/snapshot, queue deadline
и mandatory up после cancel. Targeted GREEN41/41 (`translator-undo-green.log`).

После ВСЕХ review source changes последовательно: exact hosted **302/302 PASS**
(`review-final-hosted.log`,36.599s), existing affected **56/56 PASS**
(`review-final-existing.log`,0.052s), dead Swift selftest PASS/scanner exit0/
мёртвых0 (`review-final-dead-swift.log`), release **PASS36.04s**
(`review-final-release.log`), затем полный **audit-all PASS**
(`review-final-audit-all.log`). Python47/47 — прежний source/settings receipt;
повтор не запускался по ruling root, Python/backend behavior не менялся.

Свежий fetch после проверок подтвердил базу19124f51. Этот source cut передаётся
root для нового Astra Ultra whole-diff gate; hosted exact-SHA CI, push, native
acceptance и lifecycle остаются за пределами данного локального receipt.

## Re-review: terminal noEffects notification

Actual public final→explicit copy test теперь наблюдает notification OS leaf:
causal RED1/2 assertions, GREEN9/9 с P3 manualcopy4 и real partial/unknown
controls (`terminal-notification-red.log`, `terminal-notification-green.log`).
Terminal noEffects сообщает «вставка не выполнена», без ложного partial/unknown
и без обещания clipboard. Только message branch; full release не повторялся.
Следующий подтверждённый P2: sync acceptance public auto/snapshot/replay до Task.

## Re-review: public acceptance до Task

Actual public auto/snapshot/replay: RED4/16 assertions (`public-acceptance-red.log`),
GREEN41/41 с streaming/activation/replay controls (`public-acceptance-green.log`).
Copy и смена replay target происходят в том же MainActor turn, без ожидания
pendingCount; отдельные controls фиксируют replay text A и cooldown decision
раньше B. Shared PasteInputReservation/service.reserveInput переиспользуются
streaming adapter и delegate, отдельная очередь не добавлена. Auto/snapshot
синхронно резервируют snapshot/deadline/slot до Task. Replay prepare фиксирует
text/target/cooldown/snapshot/deadline, execute только валидирует original target.
Task/helper early-return снимает reserved ticket, active lease живёт до завершения
service/key-up/child cleanup. Existing sound OS leaf переиспользован вместо
эквивалентного inline Dispatch/Purr; fake не запускает аудио. Default сохранён.
Следующий confirmed sibling: translator read acceptance changeCount до lease wait.


## Re-review F7: immutable translator read acceptance

Actual performTranslation с удержанной общей lease: внешняя запись private clipboard
без invalidate даёт causal RED1/5 assertions (`translator-acceptance-red.log`).
Дополнительная фаза записи внутри AX leaf даёт RED1/3 assertions
(`translator-ax-acceptance-red.log`). Snapshot count/epoch/target/liveness и deadline
теперь фиксируются до synchronous reserve/awaitReady; один guard проверяет их до AX,
после AX и перед CmdC. Внешняя запись до grant не вызывает AX/keys/IPC; запись во время
AX не вызывает keys/IPC. Непрерывная bounded own-edge цепь остаётся разрешённой:
ранняя собственная automation проходит, saved clipboard читается после validation.
GREEN40/40 (`translator-acceptance-final-green.log`,15.495s). Отдельной очереди нет.

Итоговый source freeze после notification/public acceptance/F7: exact hosted
**306/306 PASS** (`acceptance-final-hosted.log`,37.471s), existing affected
**56/56 PASS** (`acceptance-final-existing.log`,0.046s), dead Swift selftest PASS,
scanner exit0/мёртвых0 (`acceptance-final-dead-swift.log`), release `-j 1`
**PASS35.92s** (`acceptance-final-release.log`), затем **audit-all PASS**
(`acceptance-final-audit-all.log`) после ВСЕХ source changes. Оба CI filters
сохранены; рост302→306 — три public acceptance tests и один F7 test с тремя фазами.
Свежий fetch подтвердил base19124f51. Python47/47 — прежний receipt, не повторялся
по ruling root; Python/backend behavior не менялся. После checks меняются только docs
и commit metadata. Логи/manifest/receipt — в ignored owning SDD каталоге.

Единая shared FIFO8+1 покрывает reservations всех input owners; noEffects не является
разрешением fresh fallback. Native physical delivery, полное отсутствие AppKit hangs,
exact-head hosted CI и runtime acceptance здесь не квалифицированы. Root организует
закрывающий Astra Ultra whole-diff gate; push/lifecycle не выполнялись.


## CI: синхронизация runner-success deadline test

Exact be55c0f2 push/PR CI воспроизвели 1/306 failure: runner не стартовал.
В deadline-фазе тест выделял350ms на весь paste, включая settle120ms до runner;
под scheduler delay сервис вправе отказать noEffects до запуска канала. Revision
фазы используют отдельный checked4.07s budget. Это ошибка предпосылки теста,
production guard/budget не менялись. Fake modifier leaf с задержкой400ms дал
локальный causal RED1/1 (`runner-ci-synchronization-red.log`,1.938s).

Тест использует обычный5s budget, cooperative bounded ack runner (6s) вместо
spin-yield, диагностирует drift/reason/effect при отсутствии ack. Deadline-phase
после ack ждёт именно immutable deadline, полученный runner.run, и лишь затем
разрешает success. Captured deadline защищён lock. Fake400ms задержка сохранена;
unknown/okfalse/exactpairs/no secondchannel/no replay assertions неизменны.
Focused GREEN1/1 + два обоснованных повтора:5.620/5.514/5.528s; затем exact hosted
**306/306 PASS42.148s** (`runner-ci-final-hosted.log`). Production sources не менялись,
повтор release/audit/backend не требовался по ruling root. Остальные CI не отменялись;
новый exact-SHA CI и scoped Astra test-diff gate организует root. Push не выполнялся.
