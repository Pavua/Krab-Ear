# NOW — архив исторических checkpoint'ов (2026-09-11 … 2026-10-14)

> Вынесено из `docs/NOW.md` дословно 2026-10-19 при компактизации (оперативный фронт — одна страница).
> Здесь лежат старые релизные/приёмочные снимки; актуальное состояние — в `docs/NOW.md`.

## 2026-10-14 — Checkpoint: объединённый релиз установлен, RU call-profile PASS

Runtime source — `26542ccdc50ee1a8e062c32b93da24a2e16c61cb`:
[#2090](https://github.com/Pavua/Krab-Ear/pull/2090),
[#2091](https://github.com/Pavua/Krab-Ear/pull/2091) и
[#2092](https://github.com/Pavua/Krab-Ear/pull/2092) MERGED. Оба post-merge
workflow точного final SHA SUCCESS: [backend](https://github.com/Pavua/Krab-Ear/actions/runs/38504270511),
[CI](https://github.com/Pavua/Krab-Ear/actions/runs/38504270497).

Owner разрешил merge/deploy/E2E; Main и VG подтвердили новое тихое окно.
После двух полных минутных idle gates и штатного drain установлены оба
release plist и подписанное приложение. Backend PID **70875**, REST **71046**,
Swift **71349**, у каждого `runs=1`; loaded entrypoint/PYTHONPATH у Python
указывают на закреплённый чистый release worktree `26542ccd`.
Свежие startup markers: GigaAM ready, engine warmup, Swift passive Variant B.
Accessibility startup `trusted=true`; настоящая вставка текста не проверялась.

На 03:44:47 UTC локальный ephemeral RU E2E: **HTTP 200 за 2.062 s**, GigaAM
subprocess, непустой русский текст, профиль `voice_gateway_call`, deadline8,
без сохранения. История **23274**, encryption/migrating/restore/recording/meeting
все exact False. IPC history-page возвращает одну запись без restore warning.
После E2E завершено **15min / 16 контрольных снимков PASS**, без смены
PID/start/runs. На 04:01:30 UTC Sentry API200/complete: backend0 recent,
Swift5 прежних AppHang; lastSeen после новых bootstrap отсутствует.
Новый Ear ingress этим не аттестован. Тихое окно Main/VG освобождено.

Открыто: причина прежних default REST Whisper/MLX timeout/failfast70 и пяти
недавних Swift AppHanging UNKNOWN. RU PASS не доказывает устойчивость обычного
Whisper-пути, реальных звонков или устранение прежней причины HTTP504.
Real encryption activation, migration, purge и rotation не выполнялись.
[Подробный runtime checkpoint и следующий шаг](superpowers/handoffs/2026-10-14-combined-runtime-release.md).
Sol 6.1 Medium — docs/CI; High — следующая диагностика; Astra Ultra точечно
для нового security/architecture/release gate. Ниже — исторические checkpoints.

## 2026-10-14 — Checkpoint: history key safety, final verification

Source-база — `86a0b1978086bb4e2d9bbbf7505a264cd2abf928`:
[#2091](https://github.com/Pavua/Krab-Ear/pull/2091) MERGED после всех 27
exact-head checks (24 SUCCESS / 3 SKIPPED) и independent Astra Ultra GO.
У #2090 и #2091 оба post-merge workflow SUCCESS; для #2091 основной
[CI SUCCESS](https://github.com/Pavua/Krab-Ear/actions/runs/38493755193),
[backend SUCCESS](https://github.com/Pavua/Krab-Ear/actions/runs/38493755224):
19 SUCCESS / 2 SKIPPED jobs на точном merge SHA `86a0b197`.

Новая [карточка history key safety](superpowers/plans/2026-10-14-history-key-safety.md)
закрывает replacement-key рядом с `ENC1` и обход fresh preflight при generic
OFF→ON/import/restore. Dedicated `enabled` принимает exact bool. Все десять
journals проверяются под EX; passive lookup сохраняет SH. Legacy restore
проверяет live ∪ prospective и применяет один prepared commit.

Early Astra review нашёл P2 в новом patch: cache ключа публиковался до
отказавшего settings replace. Proper RED 3 FAIL / 2 controls PASS →
tuple3 `(settings, serialized, crypto_candidate)`; cache публикуется после
replace, до directory fsync. Исправленный frozen production diff получил
**Astra Ultra production GO**. На исходной версии тестовой фикстуры Python
3.12.7 и 3.14.8, все 33 выбранных файла: по **1238 PASS + 142 subtests**,
0 skips; 3 Swift cases deselected.
Новые regressions включают 48 Store/AES и 12 настоящих temp Unix IPC cases.
Дополнительно четыре файла с дочерними процессами на каждой версии прошли
усиленный exact-target guard: по 185 PASS + 90 subtests. Critical lint и
14 audit gates PASS. Whole-card review нашёл test-only P2: общий HOME socket
мог направить запрос чужому backend. Proper RED (истории 1/1 вместо 1/0) →
уникальный temp socket + owned LISTENING. Финальный IPC-файл **13 PASS на
каждой версии**; остальные 32 test paths и production байт-идентичны.
Повторный scoped review и новый hosted CI ещё требуются.
[Checkpoint и ограничения](superpowers/handoffs/2026-10-14-history-key-safety.md).

**Runtime:** retained `9d7a0aac`, root ещё не переключал app/plists/processes.
Bundle `86a0b197` подготовлен, не применён; объединённая выкладка crypto+
Whisper возможна после всех собственных gates и нового тихого окна Main/VG.
Real encryption OFF; activation, real migration, purge и rotation исключены.
Причины прежних RU GigaAM HTTP504, default REST MLX timeout/failfast70 и
AGENT-19 AppHanging остаются UNKNOWN. Sol 6.1 High — implementation/CI;
Astra Ultra — final security/release gate. Ниже — прежние checkpoints.

## 2026-10-14 — CURRENT: passive key merged; Whisper lifecycle RED→GREEN

Source-база — `cdfcf8ee57aa75f13ee9a2e2ae4c0ae465af6958`:
[#2089](https://github.com/Pavua/Krab-Ear/pull/2089) и
[#2090](https://github.com/Pavua/Krab-Ear/pull/2090) MERGED.
У #2090 exact head `ac2a0d91` все три workflow SUCCESS (24 SUCCESS / 3 SKIPPED
checks). Для post-merge `cdfcf8ee` основной
[CI SUCCESS](https://github.com/Pavua/Krab-Ear/actions/runs/38488885927);
[backend workflow](https://github.com/Pavua/Krab-Ear/actions/runs/38488885928)
на момент начала следующей карточки ещё выполнялся. Факт готовности итогового
релиза проверяется на его собственном post-merge SHA.

Текущая [карточка Whisper lifecycle](superpowers/plans/2026-10-14-whisper-lifecycle.md)
закрывает два подтверждённых MED: partial WAV cleanup и stale watchdog,
убивающий следующий запрос того же subprocess. Proper RED→GREEN на Python
3.12/3.14; по **81 PASS + 2 subtests**, включая два настоящих temp JSON-pipe
integration tests, lint/diff-check и 14 audit gates PASS.
[Checkpoint и границы](superpowers/handoffs/2026-10-14-whisper-lifecycle.md).
Independent whole-diff gate и новый hosted CI обязательны до интеграции.

**Runtime:** retained `9d7a0aac`, новый app и оба plist только подготовлены.
Выкладка объединяется с Whisper-карточкой после CI и тихого окна
Main/VG; OrbStack/VPN и соседние проекты не меняются. Real encryption OFF,
activation/purge/rotation не входят в scope. Причина живого default REST MLX
timeout/failfast70, RU GigaAM call-profile HTTP504 и Sentry AGENT-19 AppHanging
остаётся UNKNOWN; ни один не объявляется закрытым этими локальными тестами.
Sol 6.1 High — реализация/CI, Astra Ultra — узкий final review/release gate.

## 2026-10-14 — Source stack checkpoint: merged, final CI PASS

Проверено 14.10 CEST: source-база `origin/codex/krab-ear-v2` —
`3c8a05bc67f51ef156034f4a37adf65f1807cec6`.
[#2084](https://github.com/Pavua/Krab-Ear/pull/2084),
[#2085](https://github.com/Pavua/Krab-Ear/pull/2085),
[#2086](https://github.com/Pavua/Krab-Ear/pull/2086),
[#2087](https://github.com/Pavua/Krab-Ear/pull/2087) и
[#2088](https://github.com/Pavua/Krab-Ear/pull/2088) **MERGED** 13.10,
03:34:59–03:36:54 CEST; открытых PR на момент проверки **0**.
Оба post-merge workflow точного final SHA —
[CI SUCCESS](https://github.com/Pavua/Krab-Ear/actions/runs/38332970422) и
[krab-ear-ci SUCCESS](https://github.com/Pavua/Krab-Ear/actions/runs/38332970357).
Check receipt: **19 SUCCESS / 2 Swift SKIPPED** по docs-фильтру.
`KrabEar/` и `native/` идентичны reviewed head
`7e49e4ced72b695834ced3c2872ebac8973f3d01`; повторный Swift run этим
post-merge receipt не подтверждается.

**Граница runtime:** release-конфигурация указывает на `9d7a0aac`, current
loaded Python SHA — **UNKNOWN**. Deployment merged stack и живая UI/Keychain
приёмка не подтверждены этой source/CI-проверкой. Текущие runtime/alert
наблюдения требуют отдельной диагностики; причина alerts о Swift не установлена.

**Дальше:** source review жизненного цикла ключа и границы activation;
отдельная owner-scoped UI/Keychain приёмка перед activation шифрования.
Merge/deploy/restart, реальные
history/shares/schedules, Keychain и purge в эту docs-карточку не входят.
[Receipt и повторная проверка](superpowers/handoffs/2026-10-14-source-integration-status.md),
[карточка](superpowers/plans/2026-10-14-source-integration-status.md).

**Ниже — исторические checkpoints на указанную дату.** Старые DRAFT/unmerged,
SHA/PID/health/HOLD описывают прежние снимки; актуальный source-статус дан выше.

## 2026-10-11 — Remaining RMW roots: SOURCE/CI завершены

От reviewed migration head `eb372f8b` в owned `codex/ear-rmw-roots-20261011`
завершена [RMW-карточка](superpowers/plans/2026-10-11-rmw-roots.md): повторная
проверка shares index перед mutation и строгая загрузка scheduler для записи.
Реальная corruption-after-start и transient-read → defaults → overwrite
проверяются временными filesystem fixtures. Source `26865bf1` (scheduler) и `a0bcea9c` (sharing): proper RED→GREEN,
15+17 methods (scheduler16subtests), 4pathCI-flake8/diff-check PASS.
AstraHigh task1/task2 spec+quality Approved/open0. Guard проверяет диск,
не заменяя healthy working index; post-payload failure честно count1.
Scheduler I/O не даёт write/rotation, repaired secondread сохраняет exports.
Whole-card AstraUltra нашла новый P2 в strict scheduled checkpoint после
payload: потерю tracking при transient guard read. Single final fixwave `28c4fe63` даёт
только alreadyhealthy/fresh-authorized auto checkpoint явно tolerate readOSError,
сохраняя полный journal+entry без rotation. Direct/config/cancel strict; real
rotation/unlink/write failures не подавляются. Proper actual-file unittestRED2→
GREEN23 и existing scheduler auth7/7 PASS; sharing15 unchanged. Final scoped
AstraUltra PASS/P2 закрыт/open0; повторный audit-all14gates PASS. Source/CI head
`7e49e4ced72b695834ced3c2872ebac8973f3d01`:27/27checks SUCCESS, все3runs
38137823544/38137823549/38137820784 SUCCESS. Оба backend1102files/16chunks,
Aggregate rc0/no fallback/isofail0; оба regression-файла входят в chunks5/12
полного test-glob. Все3Swift jobs actual production callback test PASS.
Local17filepytest batch остановлен exit143 после repeated30s Torch/matplotlib
imports; не PASS. Full dependency coverage получено на hosted Python3.12.
[PR2087](https://github.com/Pavua/Krab-Ear/pull/2087) остаётся DRAFT/unmerged;
proof/rawlogs/seal9paths/clean ownedWT сохранены. Source/CI-карточка100%,
live A5.4 acceptance отдельная. Final docs и исправленный cursor/limit контракт
get_history_page — [docs PR2088](https://github.com/Pavua/Krab-Ear/pull/2088),
с identical Python/Swift trees и отдельно отслеживаемым docsCI.

Предыдущий [draft #2086](https://github.com/Pavua/Krab-Ear/pull/2086) source/CI
завершён: `eb372f8b1d977bbf48e9de1993a7548c1d962c56`, 27/27 SUCCESS,
все3 runs38128598253/38128598248/38128594131 SUCCESS; оба backend suites
1102files/16chunks/rc0/no fallback. Astra whole-card + scoped Escape fix PASS.
Это receipt source/CI; live UI/Keychain/migration приёмка остаётся отдельной.

Новая ветка включает незамерженные #2084/#2085/#2086; remote base проверена
`16d2b226`. Merge/deploy/restart, реальные shares/history/schedules/Keychain
и соседние правки не входят в scope. Sol6.1High implementation, AstraUltra
whole-card gate; user-approved `agy` Gemini3.8FlashHigh и OpenCode
MuseSpark1.3ContributorFree — скауты на source-копиях, не security approval.
Малые serial проверки, полный backend/Swift CI hosted; shared venv не менять.


## 2026-10-11 — Migration observation lifecycle: source checkpoint

В owned `codex/ear-migration-lifecycle-20261011` от `5d0ed47f` реализована
[карточка](superpowers/plans/2026-10-11-migration-observation-lifecycle.md):
один production Foundation coordinator инвалидирует старые IPC/retry/sheet
callbacks при close→reopen. Backend migration при close продолжается.
Фаза finishing и revision guard сохраняют свежий terminal warning от late refresh;
AppKit остаётся adapter, IPC off-main и прежние0.5/1.5s/IPC-over-SSE сохранены.

Source commits `d237a376`, `c43a5a4d` и Escape fix
`260a37f361c040765b156d438373fe96e693bd93`.
Corrected extraction RED30/44, pending-start RED1/45 и найденный Astra P2
finishing-refresh RED2/48 → **48/48 callback checks PASS**. Affected headless
XCTest21/21, точечный Swift typecheck, flake8/diff-check и guarded Python3.12
parity PASS/no new worker leaks; source manifest9/9 подтверждён.
[Доказательства и границы](superpowers/handoffs/2026-10-11-migration-observation-lifecycle.md).
Astra task review после finishing fix: **spec + quality Approved**, новых
defects в finishing fix diff нет. Whole-card Astra нашла Escape bypass:
штатная Escape-ветка теперь вызывает cleanup перед прежним orderOut;
adapter regression RED1/21→GREEN21/21. Scoped final re-review и новый
exact-head hosted CI — completion gates; актуальный итог после этого source checkpoint записывается в PR body/checks
ветки, а не переносится с предыдущего SHA. P3 не считать deployed/live-accepted.

Предыдущий [PR #2085](https://github.com/Pavua/Krab-Ear/pull/2085) DRAFT:
head `5d0ed47fa5d0602eda9ef23171b47f5e453c3407`, final27/27 SUCCESS,
все3workflows SUCCESS, оба полных backend suites1101files/16chunks/rc0.
Stack включает незамерженные #2084/#2085; после их интеграции проверить actual
ancestry/base и повторить exact-head CI при rebase. Исторический CI родителей
не доказывает новую migration card. Merge/deploy/restart, realhistory/Keychain,
activation/migration/purge и live NSWindow/SSE acceptance не выполнялись.

Локально только малые serial harness; full build/tests — hosted CI. Ресурсный
снимок10:14CEST: swap~23.3GiB/pressure2/load317. Sol6.1High implementation,
Astra — lifecycle review. Main Krab уже в Codex; owner разрешил Main/VG обмен
данными, соседние runtime/правки не входят в эту карточку.

## 2026-10-11 — Timeline crypto IPC и fail-closed source проверены

Ветка `claude/haiku-agents-work-79f6d6`, 15 коммитов поверх `16d2b226`; подробности и список открытого —
[handoff](superpowers/handoffs/2026-10-11-failclosed-wave.md). Итог: privacy fail-closed при битых/нечитаемых
настройках и журналах (с деградацией вместо падения на старте и purge), RMW-защита шаринга и расписания
экспорта, итог миграции шифрования через IPC, таймлайн-экспорт через consent, сверка reason-строк Swift↔Python.
Ветка уже опубликована как [PR #2084](https://github.com/Pavua/Krab-Ear/pull/2084):
head `5e309376`, 27 exact-head checks SUCCESS; independent Astra Ultra source diff
`16d2b226 → 5e309376` PASS, все 33 файла просмотрены, новых P0–P2 не обнаружено.
P3: поздний migration IPC callback после закрытия окна может возобновить SSE;
прежние corruption-after-start sharing и transient-read scheduler ограничения
не закрыты этой волной. PASS не означает live UI/Keychain приёмку.

**Текущая source-карточка:** [synthetic Timeline Swift/Unix IPC/AES-GCM](superpowers/plans/2026-10-11-a54-timeline-crypto-ipc.md)
в owned `codex/ear-failclosed-followup-20261011` поверх PR #2084. Все три формата,
decline/privacy/stale/revoked grants и wrong-key/tag failure проверены на
временном профиле, без Keychain. Wrong-key/tag→пустой успешный Timeline export
подтверждён шестью actual-AES-GCM RED cases и исправлен во всех 3 handlers;
protected history/settings сохраняются. Новый файл **30 PASS** (18 production
Swift→Unix IPC и 12 direct IPC), selected-file Python 3.12 parity —
**83 PASS + 27 subtests**, independent Astra Ultra final delta PASS.
`audit-all`: все14gates PASS. Followup
[PR #2085](https://github.com/Pavua/Krab-Ear/pull/2085) DRAFT, source/test commit
`f1b86d10bf507f1074674401a35c5a80ce91f524`; оба workflows запускаются на base
`codex/krab-ear-v2`. Первый head `0d34b7d9`: 25 checks SUCCESS / два backend
lint F811 failure до suites; исправлен binding общего pytest fixture,
production source не менялся. Новый exact-head/status и receipt проверяются
в PR checks/body, исходные27checks #2084
не считаются CI followup. Локальная source-карточка100%; merge/deploy не выполнены.
[Проверки и границы](superpowers/handoffs/2026-10-11-a54-timeline-crypto-ipc.md).
Далее отдельный source fix P3 close/callback SSE race; живой click-through
consent-sheet таймлайна; бриф для Краба
(`get_history`); решение владельца по activation шифрования (HOLD, отдельный scope) и по поведению при
нечитаемом побочном журнале. Прод-runtime в этой волне НЕ менялся и не перезапускался.
Начальный снимок ресурсов этой Codex-сессии: swap ~16.4 GiB, диск 93%;
07:56 CEST — swap ~21.8 GiB, pressure2. Source/малые серийные
тесты допустимы, full Swift build/install/restart не выполняются. Предыдущая
гипотеза о перегрузке как причине hang не аттестует конкретные новые инциденты.

## 2026-10-10 — A5.4: пакет применён, runtime smoke PASS

Снимок Ear-приёмки: 10.10, 00:08 CEST; дальнейший Gateway lifecycle ведёт
соседний координатор, состояние соседей ниже относится к этому снимку.

Runtime source-база: `9d7a0aac0cd064a05294f796a6f5fde7804e33a7` (#2083).
Оба post-merge workflow этого SHA SUCCESS:
[CI](https://github.com/Pavua/Krab-Ear/actions/runs/37861320422),
[krab-ear-ci](https://github.com/Pavua/Krab-Ear/actions/runs/37861320426).
A5.3 source 7/7 завершён; synthetic crypto IPC — 109 checks / Astra PASS.
Повторять A5.3 или начинать уже закрытый logging-блок не требуется.

Новый Swift-пакет собран одним заданием из проверенной Git-копии и применён.
Все 164 входа, pinned dependencies и шесть binary archives сверены. Две копии
Sparkle полностью эквивалентны, включая служебные атрибуты; packaged framework
проверен до подписи. Strict/deep signature и сохранённое Accessibility
requirement PASS; arm64 UUID бинарника/dSYM/пакета совпадают. Старый пакет с
UNKNOWN source provenance не переиспользован; старые source/app сохранены.

**Runtime:** Backend → REST → Swift запущены по одному разу после полного
дренирования прежних процессов. Оба STT-движка прогреты; новая запись журнала
подтверждает passive supervision Swift. Smoke: все три PID/start/runs стабильны,
IPC и REST HTTP200/status ok PASS более 23 секунд. Конфигурация и argv указывают
на новый release; loaded Python module SHA этим не аттестуется.
**Новая owner-проверка диктовки и вставки без Cmd-V PASS (00:08 CEST).**
После неё same PID/start/runs, IPC и REST health подтверждены повторно.

Владелец разрешил выпуск Ear при текущей нагрузке; это не resource GO или
разрешение запуска Gateway. Wake word включён: Swift остановлен первым,
запущен последним после готовности Backend/REST. Откат после старта нового
Swift требует снова остановить Swift первым и повторить свежий idle gate.

**Дальше:** закрыть документационный PR; затем отдельная карточка оставшихся
A5.4 UI/Keychain acceptance с явным scope перед activation.
Шифрование реальной истории остаётся OFF; migration/restore не активны.
Activation/Keychain/purge не выполнялись. Timeline ON остаётся отдельным UI
LIMIT, поэтому весь A5.4 не закрыт. Main сохранён; Gateway на момент этой
приёмки был в согласованном простое. Global IPC inflight не наблюдаем. Sentry auth/organization
приём PASS; свежий ingress именно Ear UNKNOWN. Изменение hash settings после
старта сохранено как наблюдение с неустановленной причиной, без заявления
byte-exact settings parity.
[Фактический release и границы проверки](superpowers/handoffs/2026-10-10-a54-package-runtime-release.md).
Sol6.1High — механика/docs; Astra — package/lifecycle gate;
Muse/agy — только публичные scout briefs.

Нижние разделы — checkpoints на указанную дату; старые PID/SHA/HOLD не являются
текущим состоянием. Package/runtime smoke не доказывает live crypto acceptance.

## 2026-10-09 — A5.4 synthetic crypto IPC проверен; release HOLD

Тестовый блок завершён локально: **109 checks**, независимый Astra recheck
**PASS**. Настоящие production AES-GCM, StateStore и Unix IPC выполняются на
изолированном synthetic профиле; заменён только источник случайного ключа.
Restart сохраняет историю/статус/аннотацию; неверный ключ и повреждённый tag
дают fail-closed read/compact, protected journals/settings остаются byte-exact.
Export проверен без grant, с grant и со stale grant после restart.
Исправлен test-scanner P2 для JSON-escaped key representations через RED→GREEN
и positive controls в actual owned sinks. Production sources не менялись.
[Матрица и границы приёмки](superpowers/handoffs/2026-10-09-a54-synthetic-crypto-ipc.md).
[Точная карточка](superpowers/plans/2026-10-09-a54-synthetic-crypto-ipc.md).
Тестовый source-блок — 100%; весь A5.4 release этим результатом не закрыт.

Docs [#2082](https://github.com/Pavua/Krab-Ear/pull/2082) MERGED по отдельному
owner разрешению: `c106199f25d0be63d6d548cf9f3836277ebcff0e`.
Post-merge CI этого SHA: основной CI SUCCESS, krab-ear-ci ещё IN_PROGRESS
на снимке 00:45 CEST. У нового test PR exact-SHA CI проверяется отдельно.

Byte-exact rollback текущего Swift .app/runtime подготовлен; strict/deep
codesign verify PASS, live bytes/PID неизменны. Старый consent binary имеет
совпадающие executable inputs, но source→binary provenance UNKNOWN;
CI artifacts отсутствуют, reuse HOLD. Новый пакет ещё не собран/применён.
Sentry по явному read-only scope: auth/API PASS, organization errors за24h
108 accepted/0 rate_limited. Свежий ingress именно Ear UNKNOWN: latest backend
issue 07.10 17:48 UTC, agent issues пусты; это не доказательство live health.
Ресурсный снимок 00:32–00:33 CEST: четыре пробы pressure level2,
free RAM87–408MiB и активный paging при уменьшающемся swap. Full Swift build
и cutover HOLD до стабильного окна, package provenance и release gate.
Keychain/UI/live encrypted-history acceptance и activation не выполнялись.
Main/Gateway/shared WIP сохранены. Для механики — Sol6.1High;
для конкретного package/release gate — Astra точечно.

## 2026-10-08 — old release восстановлен; A5.4 bundle подготовлен

По явному owner разрешению 23:48 CEST прежний root `ear-release-1ebd12eb/Krab Ear`
восстановлен атомарно в **23:54:01 CEST**; все 2644 blobs/modes совпали с old Git tree,
HEAD/status чисты, fsync/postcheck PASS. Backend `3957`, REST `3924`, Swift `2800`
сохранили PID/start time; plist не менялись, restart не выполнялся, health OK.
Actual in-memory module SHA остаётся UNKNOWN; причина исчезновения root не установлена.
[Recovery evidence и конкретная карточка](superpowers/plans/2026-10-08-a54-prepare-candidate-rollback.md).

Существующий helper теперь прошёл **prepare + verify(before)** для приватного
bundle `1ebd12eb` → `9fee1ef4` (0700/0600). Конфигурация не применена.
При повторной ресурсной пробе swap уже ~27.9 GiB used: full Swift build/restart отложены;
Swift package, Sentry qualification, независимый release gate
и свежее maintenance window остаются необходимыми. A5.4 deploy/live/crypto не закрыт.
Текущий Swift supervisor подтверждён passive: marker 03:39:12.765 совпал со
start time PID2800. Sentry freshness UNKNOWN: callable MCP/read token в этом
harness отсутствуют; настроенный remote server сам по себе не доказывает auth/ingress.
Main/Gateway/WIP сохранены; quiet window соседям закрыто после recovery.

## 2026-10-08 — A5.3 source завершён; следующий этап A5.4

**7/7 PR MERGED**, база `origin/codex/krab-ear-v2`:
`9fee1ef4c2f5540636fd4faae1e53c93142054a5`. Оба post-merge workflow SUCCESS
на этом SHA: [CI](https://github.com/Pavua/Krab-Ear/actions/runs/37833572070),
[krab-ear-ci](https://github.com/Pavua/Krab-Ear/actions/runs/37833572064).
21 check: 19 SUCCESS, 2 Swift SKIPPED по changed-path filter; три Swift CI jobs
на signing head с идентичным tree прошли до merge. Full backend: 1091 файлов,
все 16 chunks. Combined Python 3.12: 121 passed, 38 subtests passed;
независимый Astra Ultra **SOURCE COMPOSITION PASS**.

Порядок PR: #2076 → #2075 → A #2077 → B #2078 → C #2079 → D #2081 → signing #2080.
[Точная таблица heads/merge SHA и границы доказательства](superpowers/handoffs/2026-10-08-a53-source-merged.md).
Source-блок завершён на 100%; этот процент не описывает live A5.4 или весь A5.

**Дальше:** [план A5.4 release/acceptance](superpowers/plans/2026-10-08-a54-release-acceptance.md).
Сначала конкретная карточка подготовки candidate/rollback с разрешёнными probes;
cutover — отдельное owner окно после проверяемого результата.
Production package A5.3 не применён; в source-этапе нет новых build-install/restart,
encryption activation или чтения реальной истории/Keychain. Main/Gateway сохранены.
Текущие runtime PID/loaded SHA/флаг не проверялись. Timeline ON остаётся
fail-closed UI LIMIT; crypto-deny plaintext fixture D не доказывает encrypted E2E.
Ранее owner-confirmed автоматическая вставка в Codex — отдельная live-приёмка
Swift signing repair, без нового restart GO.

**Ниже — исторические checkpoints на их дату.** Их слова «открыт», «pending»,
«не смержен» и старые SHA/PID не использовать как текущую merge/runtime картину.

## 2026-10-08 — CI repair A5.3 и исправление Accessibility

Свежая база линии: `origin/codex/krab-ear-v2` =
`efecb801aae3f3c62fa03314ba4f5b142ac8e906`. A/B/C остаются открытыми PR.
A #2077 `00ba3300`, B #2078 `d24f001a`, C #2079 `886f061a`:
полный exact-SHA CI GREEN (по 27 checks, оба backend jobs SUCCESS).
C три Swift build jobs SUCCESS. Local B/C fixture repair: 34 файла,
839 passed, 37 subtests, 1 skipped; independent fixture delta review PASS.
[CI repair](superpowers/handoffs/2026-10-08-a53-ci-contracts.md).

Card D: isolated real IPC + production Swift IPC/coordinator harness LOCAL GREEN.
27 integration tests + 1 low-disk isolation regression прошли project py312
harness. Final independent Astra Ultra Linux delta + whole-D/matrix review
PASS; [D #2081](https://github.com/Pavua/Krab-Ear/pull/2081) открыт,
CI `68a05ae` выявил optional-ML import-side-effect только в D fixture;
CPU-only import fence закрыт 12 RED→GREEN cases без ослабления guard.
Текущий exact-SHA CI статус — PR Checks. База PR — основная линия, чтобы запускались все
guards; отдельная D-дельта — поверх C `886f061a`. Production sources A/B/C не менялись; дополнительный test helper P2
закрыт в D. Transport failure-output P2 также закрыт: 6 RED→GREEN cases.
Full local Swift build пропущен при высоком swap; малый harness
скомпилирован и выполнен, новый D exact-SHA CI ещё требуется.
[Матрица D и ограничения](superpowers/handoffs/2026-10-08-a53-card-d-verification.md).

A5.3 не смержен/не выкачен, шифрование не активировано. Timeline UI при ON
остаётся documented narrow-C LIMIT; production UI/live/encrypted-history E2E
не заявляются. Main/Gateway и старые worktree WIP сохранены. Исторические
runtime PID ниже не применять как текущие.

Accessibility repair #2080 `2290b528`: полный CI GREEN, PR ready for review.
Ранее по отдельному разрешению владельца исправлена только подпись живого
Swift-агента и выполнен его управляемый рестарт. Owner подтвердил вставку
диктовки в Codex без Cmd-V; Backend/REST были сохранены. Это отдельная приёмка
подписи, не deployment A5.3 и не новый restart GO.

Ниже сохранены более ранние checkpoints, актуальные только на их дату.

## A5.3 Card C, 2026-10-03 — работа продолжается

Card A [#2077](https://github.com/Pavua/Krab-Ear/pull/2077): source/local PASS,
полный backend CI FAILED в 29 тестовых файлах; исправления fixtures/docs ведутся
отдельно. Card B [#2078](https://github.com/Pavua/Krab-Ear/pull/2078): source/local
PASS, backend CI пока выполняется. Startup #2075 и docs #2076 CI PASS.
Card C поверх `9358cd78`: Astra High source review, release build, 324 Swift
tests, 8 зависимых Python-файлов, новый launcher py3.12 parity и audit-all PASS.
Далее C PR/CI и Card D isolated IPC E2E. Merge/deploy/encryption activation
не выполнялись.
[Текущий checkpoint C](superpowers/handoffs/2026-10-03-a53-card-c-progress.md).

Ниже — более ранние checkpoints; их слова «ещё впереди» относятся к их дате.

## A5.3 Card B, 2026-10-03 — source checkpoint

База main-линии повторно сверена: `efecb801aae3f3c62fa03314ba4f5b142ac8e906`.
Card A: [PR #2077](https://github.com/Pavua/Krab-Ear/pull/2077),
`93c7e0b0d46b730c64c4a924d48b0f124cbce369`, OPEN; основной CI PASS,
backend chunked CI ещё выполняется. Card B поверх этого SHA:
Astra High source PASS, Python3.12 parity49files PASS, audit-all PASS.
Card B PR/CI ещё впереди; C/D код не начат. Merge/deploy не выполнялись.
[Матрица Card B и ограничения](superpowers/handoffs/2026-10-03-a53-card-b-progress.md).

## A5.3, 2026-10-03 — source checkpoint

База разработки: `origin/codex/krab-ear-v2` = `efecb801aae3f3c62fa03314ba4f5b142ac8e906`.
Card A завершён в `codex/ear-a53-completion`: независимый Astra High source PASS,
Python 3.12 parity 56 файлов PASS, audit-all PASS. Source PR/CI ещё впереди;
Card B/C/D ещё не реализованы. Деплой и включение шифрования не выполнялись.
Матрица и существенный риск прежней backup-изоляции тестов:
[Card A verification](superpowers/handoffs/2026-10-03-a53-card-a-verification.md).
[Текущий handoff](superpowers/handoffs/2026-10-03-a53-autonomous-progress.md).

Сведения runtime ниже — снимок 30.09, в этой source-сессии не перепроверялись.

## Cutover 2026-09-30 — текущий runtime

**Backend и REST работают на `1ebd12eb69695296a13c39eac35a9b7af3405ba5`**
([#2072](https://github.com/Pavua/Krab-Ear/pull/2072)) с 03:37 CEST:
Backend PID **28862**, REST PID **29222**, Swift PID **88227** не перезапускался.
Точный post-merge [CI](https://github.com/Pavua/Krab-Ear/actions/runs/36653010975)
и [krab-ear-ci](https://github.com/Pavua/Krab-Ear/actions/runs/36653010765)
завершились success. Исправлены ранний порядок `SettingsService` и гейты
автоматических LLM warmup/keepalive; при живых флагах rewrite/punctuation/keepalive
**OFF** стартовый прогрев LLM не должен запускаться.

По разрешению владельца выполнены четыре idle-пробы за 60 с и финальная проба,
последовательные `bootout` с подтверждением исчезновения старых job/PID,
проверенная замена двух plist, `bootstrap` Backend → IPC → REST. Изменились
только entrypoint и `PYTHONPATH`; interpreter, cwd, logs и прочие настройки
сохранены. Новый release — clean, detached, locked; `c3a1779f` и приватные
bundle сохранены для восстановления. Процедура описана в
[runbook](superpowers/plans/2026-09-29-safe-release-cutover.md); его старые SHA
и пути не применять повторно. Для этого cutover Backend readiness ограничен
360 с (сохранённый LLM catalog timeout 240 с), REST — 120 с после Backend.

**Независимая read-only приёмка PASS:** Backend/REST `runs=1` без выхода,
IPC ping и REST `/health` 200; четыре стабильных снимка за ~60 с. История
читается: счётчик 13 063 перед остановкой → 13 066 после старта, одна запись
проверена по ID без вывода текста. Журнал содержит 23 199 валидных JSON-строк;
последние новые записи датированы до cutover. `restore_pending=false`,
`key_present=false`, saved/runtime encryption **OFF**. В startup наблюдался
штатный STT warmup; признаков автоматического LLM warmup или прежнего
`_settings_svc` traceback нет. Существующие warnings о memory pressure и REST
конфигурации остаются. Sentry принимает события организации и не показывает
новых unresolved Ear issues после старта, но свежий ingress именно Ear-проектов
не доказан. Live-диктовка, MLX, TTS и encrypted E2E здесь не проверялись.

Очередь purge/FIFO P1/P2 закрыта. Шифрование не включать без отдельного решения
владельца по открытым копиям и ротации токена. Старые релизы и bundle не удалять.

## Cutover `c3a1779f` 2026-09-30 — исторический снимок

Backend PID 8854 стартовал в 02:21:38, REST PID 9848 — в 02:22:07 CEST;
до следующего cutover они работали на
`c3a1779fcd7b64dbe3d69231479d911a85d5c321`. Read-only приёмка запуска
прошла; история 13 058 → 13 062 после обновления старого кэша, журнал 23 195
валидных строк. Тогда был найден предсуществующий startup-дефект `_settings_svc`,
исправленный в #2072. Подготовка процедуры —
[#2070](https://github.com/Pavua/Krab-Ear/pull/2070), отчёт о предыдущем cutover —
[#2071](https://github.com/Pavua/Krab-Ear/pull/2071).

## Приёмка 2026-09-29 (исторический снимок)

**Проверенная база линии и прод-код:** `codex/krab-ear-v2` =
`245aae2d9a1cbe77b6675f8bb87783758e85a826` ([#2066](https://github.com/Pavua/Krab-Ear/pull/2066)).
Exact-SHA post-merge [CI](https://github.com/Pavua/Krab-Ear/actions/runs/36508537626)
и [krab-ear-ci](https://github.com/Pavua/Krab-Ear/actions/runs/36508537865)
завершились success. В 04:03:51 CEST 29.09 backend и REST переведены на
clean, detached, locked release
`~/.codex/worktrees/ear-release-245aae2d/Krab Ear`: backend PID 67525,
REST PID 67608, GigaAM PID 67572. Swift-агент PID 88227 не перезапускался;
прежний locked release `84deb513` сохранён для отката. Владелец разрешил
выкатку при текущей нагрузке машины; перед `bootout` прошли четыре idle-пробы
за 60 с и финальная проба.

**Read-only post-deploy smoke:** IPC ping и REST `/health` 200; запись/встреча
не активны, wake watchdog не wedged, restore_pending=false, key_present=false.
История читается: active_count=13 058 до и после, страница из одного элемента
получена без вывода текста. Live-диктовка, MLX и encrypted-history E2E этим
не проверены. Последние private MLX jobs отказали на admission до запуска
тестов; квалификации MLX для A5.2 они не доказывают.

**`history_encryption_enabled` остаётся OFF** — код в рантайме есть, но флаг не
включён. Прод-профиль содержит `history.ndjson` ~24 МБ **открытым текстом** и
бэкап `history.ndjson.bak-20260831-…` ~23.4 МБ. Включать флаг **только после**
решения владельца по бэкапам и ротации HF-токена (см. «необратимые решения»):
старые открытые копии шифрование уже не защитит.

**Исторический снимок до деплоя, 29.09 ~02:23 CEST:** backend/REST PID 36853/36856, релиз
`84deb513`, release checkout чистый; ping/diagnostics/REST health успешны,
запись и встреча не активны, restore_pending=false. Агент один (88227),
подписи валидны, полезная нагрузка app/runtime/build совпадает.
Sentry за 24 ч: backend issue `KRAB-EAR-BACKEND-1V`, четыре таймаута
`get_meeting_report` по 180 с (последний 28.09 07:52Z); связь с SHA не доказана.
Приём событий организации доступен (92 accepted / 0 limited), свежий ingress
агента не подтверждён. Полный E2E/MLX отложен: swap ~27.7 ГБ и чужой test gate.

**Принятые PR:** #2060, #2062, #2063, #2064, #2065 и #2066 смержены;
при начале приёмки открытых PR не было.

**Что НЕЛЬЗЯ делать без решения владельца (необратимо):**
- Удалять реальные копии/бэкапы в прод-профиле (сейчас ~23.4 МБ открытой истории
  в `.bak` + копии настроек с секретами).
- Включать `history_encryption_enabled` до отдельного решения владельца по
  старым открытым копиям и токенам.
- Любой новый деплой — только после exact-SHA CI/review и quiet-window
  с fail-closed проверкой активности; смена release pin требует
  `bootout`/`bootstrap` по процедуре 11.09.

**Закрыто:** два P1 блокера приёмки (late append при purge и parent fsync
перед restore replace) исправлены в [#2066](https://github.com/Pavua/Krab-Ear/pull/2066)
и задеплоены. Это не разрешает включать шифрование.

**Очередь на 29.09 (исторический снимок; пункты 2–3 закрыты):**
1. Инвентаризация копий вне профиля (Time Machine / iCloud / worktree'и) — purge
   их **не** закрывает, shred ключа открытые копии не нейтрализует.
2. Тогда `.secrets.bak*` и `auto_glossary.json.bak*` не покрывались purge;
   исправлено в [#2068](https://github.com/Pavua/Krab-Ear/pull/2068).
3. Тогда FIFO на пути `history_purged_ids.ndjson` мог блокировать чтение;
   дескрипторная защита реализована в [#2069](https://github.com/Pavua/Krab-Ear/pull/2069).

Claim старого checkpoint о `save_settings` вне lock опровергнут на
`31568b1f`: запись находится внутри `StateStore._lock()`; synthetic
contention и SH→EX проверки пройдены. Отдельный фикс по этому claim не нужен.

**Приоритеты повторной проверки сильными моделями** (что отдавать на GPT-6/Astra,
а что не требует): [`superpowers/plans/2026-09-28-strong-model-review-priority.md`](superpowers/plans/2026-09-28-strong-model-review-priority.md).

---

Оперативный раздел выше обновлён **2026-09-30**; плановая часть ниже — снимок **2026-09-18**. Журнал волн — [`ROADMAP-2026H2.md`](ROADMAP-2026H2.md), не очередь. Горизонт 2–4 нед: [`design-briefs/2026-09-05-horizon-plan.md`](design-briefs/2026-09-05-horizon-plan.md). Как работать: [`EXECUTOR_PLAYBOOK.md`](EXECUTOR_PLAYBOOK.md).

## Деплой 2026-09-18 №2 (исторический снимок) — ночная волна

- **Прод-код:** `e004ba3d` — R1 табло, F5 (ленивая выгрузка семантики),
  F2/F2b (спенд-кап харденинг), журналы. Поведение прода не меняется:
  `cloud_rewriter_enabled=False`, `semantic_search_enabled=False` (весь код
  спит до флагов). Процедура §деплой 09-11: busy 60 с idle + финальный,
  worktree --detach, swap SHA (бэкап `/tmp/ear-plist-backup-20260919/`),
  bootout (poll до «gone», ~5 с) → bootstrap без EIO/ретраев.
- Backend pid **89956**, REST **90743** (GigaAM worker 90534); агент **7602**
  не тронут. Прежний релиз `35b32bab` оставлен для отката.
- Постдеплой: ping ok (v2.0.5, ~10 с), diagnostics 13/13, REST `/health` 200
  (~6 с), e2e 44/44 + 21/21 green, privacy-gates hold, Sentry — 0 инцидентов Ear.
  F1-лексика цела (phonetic 11/31, hotwords 43); semantic off/model не грузился.

## Деплой 2026-09-18 №1 — F1 (35b32bab)

- **Прод-код:** `35b32bab` — F1 (лексика W4) в проде. Процедура §деплой
  09-11 без отклонений: busy-check 60 с idle + финальный, worktree --detach,
  swap SHA в обоих plist (бэкап `/tmp/ear-plist-backup-20260918/`), bootout
  (poll до «gone», ~6 с) → bootstrap (ретраи не потребовались; EIO не возник).
- Backend pid **41951**, REST **42836**; агент **7602** не тронут. Прежний
  релиз `5cab7988` оставлен для отката.
- Постдеплой: ping ok (v2.0.5, ~8 с), diagnostics 13/13, REST `/health` 200
  (~4 с), e2e-смоки 44/44 + 21/21 green, privacy-gates hold, Sentry — ноль
  инцидентов Ear за окно.
- 🔴 **Live-добор W4** (авто-seed в коде — только на пустой файл; у владельца
  файлы непустые): hotwords `оверлей`,`openclaw` через IPC (42→43);
  phonetic +10 кураторских записей / 28 вариантов через `add_phonetic_entry`
  (было 1/3 → стало 11/31). Следующие запекания лексики — тоже IPC-добором.
- WER до/после — ждёт R2-записи владельца (инструмент: `Record Golden Set.command`).

## Деплой 2026-09-16

- **Прод-код:** `5cab7988` (R1 encryption fail-closed + R2/R4 тесты, CИ зелёный:
  CI + krab-ear-ci + mlx-nightly). Процедура §09-11 без изменений; EIO на
  первом bootstrap REST сработал как задокументировано (ретрай +5с — ок).
- Backend pid **21156**, REST **22059** (оба свежие); агент **1020** не тронут
  (Swift не менялся). Прежний релиз `6561a030` оставлен для отката.
- Проверка «нет записи/встречи»: 60 с idle + финальный чек перед bootout.
  Постдеплой: ping ok, diagnostics 13/13, агент 1, Sentry — один
  `GigaAM worker shutdown` warn-batch (штатный артефакт рестарта, класс
  self-heal). Fable retro-gate R1 — пост-квотой (см. BACKLOG).
- R1 в проде нулевого эффекта (фича banned-off) — деплой гигиенический
  (колея == прод), не релиз фич.

## Деплой 2026-09-11

- **Прод-код:** `6561a030` (#2016). Backend и REST запускаются из неизменяемого
  release-worktree `~/.local/share/krab-ear/releases/<sha>` (locked, detached);
  путь прописан в `PYTHONPATH` и `ProgramArguments` обоих plist
  (`ai.krab.ear.backend`, `ai.krab.ear.rest`). 🔴 Деплой = `git worktree add --detach`
  нового SHA + замена SHA в plist + `bootout`/`bootstrap` (kickstart plist не перечитывает;
  bootstrap сразу после bootout даёт EIO, пока старый процесс гасится — повторить).
  Прежний релиз `375d4bed` оставлен для отката. Проверка «нет записи/встречи» — перед bootout.
- Вошло: privacy fail-closed #2005–#2016 (корень — `service._get_runtime_setting`
  для `privacy_mode_enabled`, #2016), brain-lease конечный TTL #2004. Swift не менялся —
  агент не пересобирался. Живой e2e (`scripts/run_e2e_smokes.command`): 65 PASS / 0 FAIL.
- Открыто 11.09 → закрыто: encryption fail-open — волна R1 (fail-closed +
  громко), задеплоено 16.09; soak-таймаут — волна R2 (per-cycle unload
  шторм убран фикстурой, soak ~22 с, гард 30 с цел).

**Source-дополнение 2026-09-07:** PR [#2001](https://github.com/Pavua/Krab-Ear/pull/2001)
добавляет телефонный STT-профиль для Voice Gateway: explicit auto до общего STT,
RU через уже загруженный owner GigaAM, без второго worker и history. Контракт и незакрытая
runtime qualification — в [плане](superpowers/plans/2026-09-07-vg-gigaam-call-profile.md).
В этой работе runtime не перезапускался; флаги Gateway
`KRAB_STT_EAR_CALL_PROFILE_ENABLED` и `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED` OFF.
Source-проверки не доказывают CI другого SHA или готовность живого звонка.
Текущие HEAD/CI — в primary `.remember/CODEX_CALL_STT_20260907.md` с повторной
проверкой Git/GitHub. SHA/PID ниже сохранены как snapshot 05.09, не текущая проверка.

