# NOW — что делать сейчас (Krab Ear)

## 2026-10-10 — A5.4: пакет применён, runtime smoke PASS

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
LIMIT, поэтому весь A5.4 не закрыт. Main сохранён; Gateway остаётся в
согласованном простое. Global IPC inflight не наблюдаем. Sentry auth/organization
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

## База и runtime snapshot

- Репозиторий: [Pavua/Krab-Ear](https://github.com/Pavua/Krab-Ear)
- Прод-колея: **`origin/codex/krab-ear-v2`** @ `4bf1c4e9` (26.09: whisper #2054,
  polish+P3 #2047, глоссарий #2046, A5.2a #2052, CI-фикс #2053)
- **Прод-код (Python-бэкенд):** `bc09490f` — A5.2a и всё UI-мержи **source-only**,
  в прод не задеплоены; `history_encryption_enabled` остаётся **OFF**
- Агент (Swift) пересобран 26.09 (`make sign`) под голосовой ввод во время
  входящего скрининга; pid меняется, смотреть `pgrep -fl KrabEarAgent`
- Worktree: `git worktree add .worktrees/<slug> -b feat/<slug> origin/codex/krab-ear-v2`
- Main Krab Q2 (:8080 purpose slots, RIS/SergeyRG) — **не Ear**: [`ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md`](../ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md)

## CI-изоляция — cutover СДЕЛАН (read-back 2026-09-26)

- **API read-back:** в публичном `Pavua/Krab-Ear` — **0 self-hosted раннеров**;
  PR/push CI идёт на GitHub-hosted `macos-latest` (disposable) и не трогает личный Mac.
- Единственный self-hosted раннер — `krab-ear-m4max-private` в приватном
  `Pavua/Krab-CI-Control`: **только** MLX/Metal-гейт по exact SHA доверенной колеи
  и только когда владелец не под бенчмарками. Hosted macOS не заменяет MLX-проверку:
  VM не даёт эквивалентного Metal-пути; красный private MLX gate — отдельный
  сигнал расследования, не подмена PR CI.
- Прежний текст «раннер `krab-ear-m4max` ещё зарегистрирован в public repo» устарел.


## Задеплоено 2026-09-05

- **#1997** — сенсор памяти: `vm_pressure` + swap у потолка; SIGKILL воркера → `stt.worker_killed`, не `mlx.oom`. **`memory_conductor_enforce*` всё ещё OFF** (shadow только логирует).
- **#1998** — визуал Call Observer, Claude Design-секций, оверлея диктовки; parity-бинарь + relaunch агента.
- **#1999** — C1 brain-holdoff: на стопе записи / rewriter / summarize **не** `lms load` при пустом Studio; lease только если реально грузим; OOM-path не целится в brain; cloud-fallback при **Studio недоступен** (не пустой каталог). **`cloud_rewriter_enabled` всё ещё OFF** — путь есть, флаг не включён.

## Политика LM Studio / brain (владелец 2026-09-05)

**15+ ГБ local** — один слот экосистемы: у Краба обычно **`lm-studio-local/gemma-4-26b-a4b-it@4bit`** (`LOCAL_PREFERRED_MODEL`), не «второй» Ear-only 27B. Ear `llm_brain_model` = `qwen/qwen3.6-27b` — lease/unload/OOM-UI, **preload-on-stop уже False**.

| Режим | Поведение |
|---|---|
| Idle / away | Краб отвечает в группах из того же RAM-слота; саммари звонков — если модель уже загружена |
| Работа (Cursor, диктовка) | **Не autoload.** Ear не должен `lms load` после ручной выгрузки владельца |
| Любой LLM-путь Ear | **Сначала LM Studio** (каталог / chat), без преждевременного `lms load` |
| **Пустой каталог** Studio | ≠ «Studio недоступен». Пусто → extractive / сырой STT, **без autoload** (#1999) |
| **Studio недоступен** (сеть/процесс) | Cloud, **если** `cloud_rewriter_enabled=true` и не privacy; иначе extractive/сырой текст (#1999) |
| Кондуктор | **`enforce_brain` — никогда.** Не включать `memory_conductor_enforce*` «чтобы выгнать» 27B |

🔴 Автовозврат 15+ ГБ после ручной выгрузки сейчас чаще **Краб** `ensure_model_loaded`, не Ear. Ear holdoff в проде (#1999); Краб — бриф в handoff §5.

Живые флаги (не трогать без владельца): `llm_rewrite_enabled=False`, `cloud_rewriter_enabled=False`, `llm_brain_preload_on_stop=False`, `memory_conductor_enforce*=False`, `mlx_oom_auto_unload_enabled=True` (brain исключён из target, #1999).

## Контекст (коротко, ещё актуально)

- Телефония только через Voice Gateway (#1989/#1990); ключ VG синхронизирован 03.09.
- REST fail-fast с логом (#1991); C3 в колее (#2000: attempt-deadline на Whisper; таймаут самого ожидания замка по-прежнему отвергнут — честная очередь за GPU).
- GigaAM `confidence=0.9` (#1985) — ретрай по уверенности для RU мёртв; решение за владельцем.
- P0 turbo/REST worker, Memory Conductor shadow, Call Observer w1 — в проде; детали в `ROADMAP` / `CLAUDE.md`.
- Не включать: `REST_IN_PROCESS_ENABLED`, `semantic_search` / SenseVoice / Voxtral на этой машине, `history_encryption_enabled`.

## Следующая волна

### A5 — исторический checkpoint 30.09 (не текущая очередь)

- **Source `origin/codex/krab-ear-v2` @ `efecb801` сверён 30.09.** A5.2a DONE (#2052): fail-closed гейты plaintext legacy-sinks при `history_encryption_enabled=ON`, re-check под `history.lock`; adversarial-ревью A5.2a закрыло 2 CRITICAL (TOCTOU) и 1 MAJOR.
- **A5.2b DONE в source:** b1 #2056/4971c24a (encrypted snapshot), b2 #2058/96554b75 (restore/recovery + union tombstones/purged), b3 #2060/7afa21da (diskguard+retention); далее integrity #2066/#2068/#2069 accepted.
- **Runtime — исторический снапшот 1ebd12eb #2072 (17:47), encryption OFF:** активации и live encrypted E2E-доказательства нет; релиз уже отражён вверху NOW, blanket-GO не даётся.
- **Scoped inventory 30.09:** ограниченная локальная инвентаризация завершена / review PASS; полное покрытие INCOMPLETE, содержимое UNKNOWN, сырой отчёт приватен и не публикуется.
- **Долг код-видим:** `A5_2B_CALLER_SUCCESS_LOG_DEBT` активен (`service.py` логирует migration-complete при `MigrationResult.reason=history_encryption_operation_unavailable`); у PolicyReader callback `push_error` есть, но manager-вызовы его не пробрасывают — отдельным долгом, fixed не заявляется.
- **Спека:** [A5 history-at-rest](superpowers/specs/2026-09-24-a5-history-at-rest-design.md); [handoff 25.09](superpowers/handoffs/2026-09-25-a5-lifecycle-start.md) — исторический порядок работ, A5.2b1–b3 уже приняты.
- **На дату 30.09 дальше планировались** logging и A5.3 по spec §7. К 08.10 A5.3 source завершён (7/7 PR); актуальная очередь — A5.4 release/acceptance в начале NOW. Утверждения этого checkpoint о долгах и нереализованных контрактах не использовать как текущую проверку кода.

### Инцидент Glovo 2026-09-26 — закрыт (whisper)

- Ребёнок позвонил на DID, скрининг слушал, а подсказать агенту «на лету» было
  нечем. Закрыто двумя тонкими клиентами к одному бэкенд-эндпоинту VG
  `POST /v1/sessions/{id}/agent/whisper`:
  - **Вариант C (сделан, #2054):** поле «Шепнуть скринеру…» в нативном Call Observer,
    видно только на `meta.screening`-сессиях, one-in-flight, 404/503 → «Скринер
    недоступен» (текст сохраняется), успех → тихий чек + haptic. Агент собран и запущен.
  - **Вариант B (DRAFT, ждёт owner-review):** TG-бот «Krab Call Control» —
    спека `Krab Voice Gateway/docs/superpowers/specs/2026-09-26-call-control-tg-bot-design.md`.
    Правки — в VG-репо отдельной сессией (граница экосистемы).
- Известные нюансы для владельца: лимита длины подсказки нет ни в клиенте, ни в VG
  (длинный текст уходит в промпт дословно — может разогнать стоимость хода);
  `whisper_pending` в ответе 200 клиент не показывает.

C2/C3/C5 закрыты в колее 07–08.09 (интеграция #2000, схема #2003, сверено 14.09):
Telnyx вырезан из CD-секции (осталась payload-совместимость `Models.swift` + тесты),
Whisper ждёт по attempt-deadline, валидатор отвергает неверные типы brain/cloud-строк,
paste-флаги типизированы. Не строить заново.

### Корни (порядок)

1. **R1** — DONE + задеплоено 16.09 (fail-closed, Fable retro-gate пост-квотой).
2. **R2** — DONE (unload-нейтер, soak ~22 с; CI nightly подтверждает скипы).
3. **PR #2001** — синтетика 10/10 (p50 0.4 с); ЖИВОЙ ЗВОНОК СОСТОЯЛСЯ
   16.09 (VG-сессия, отель SH Valencia Palace, 92 с, IVR, ~$0.03–0.04,
   запись+саммари в TG): тракт чист, НО Ear-профиль не задействован
   (0 обращений к :5005, es→groq). Живой RU-замер открыт: форсированный
   Ear-STT (карточка VG) или RU-сценарий. Busy-probe оппортунистически.
4. **Smoke-раннер Ear** — DONE (launchd, штатные прогоны OK 15–16.09).
5. **W1/W2** — DONE+задеплоено (mic-hold гейты; callassist ownership-gate).
   GigaAM-финал: вердикт без кода (роутинг уже GigaAM-first + покрыт).
6. **W4/F1 STT-лексика** — ЗАПЕЧЕНО 18.09 (#2029) и **В ПРОДЕ** (деплой
   `35b32bab` 18.09): 10 phonetic-записей (openclow, RU/ES-препараты,
   висперед→whisper, лрд/lrd→p0lrd), seed-hotwords оверлей/openclaw, tail-фильтр
   голого `dimatorzok`, `phonetic_vocab_enabled=True`, REST-движок wired.
   Live-добор выполнен (seed не доехал на непустые файлы): hotwords 42→43,
   phonetic 1/3→11/31 через IPC. WER до/после — ждёт R2-эталоны.
   `maby` НЕ запечён (ждёт примеров; EN "maybe" — контроль в R2-сценарии).
7. **Волна 0 (гигиена, до 30.09)** — 0.1 (изоляция e2e-моста), 0.2 (контракт),
   0.3 (quality_profile из настроек), 0.4 (паритет IPC-документации: 50 записей),
   0.5 (меню Update Channel удалено) — DONE+смержены 17.09. Остаток волны: ответы
   соседей (0.6, ждём Krab Main / VG).
8. **Ночная волна 18/19.09 (в колее, ждёт деплой-окна; флаги OFF)** — R1 табло
   (#2033/#2034: сканер+панель+launchd 06:00; первый снимок — fail из-за
   исторических bridge_401=24, вымылись к 10:00), F5 ленивая выгрузка семантики
   (#2037/#2038: `_semantic_step`, always-on, 1800с/0=off, бeз enforce и IPC),
   F2/F2b spend-cap (#2035/#2036 + #2039/#2040: атомарный резерв через
   `core/atomic_io`, inf-кламп, fail-closed; adversarial-ревью нашло 5 дыр →
   закрыты, SECURITY-PASS). Поведение прода не меняется (флаги OFF).
   D10 исполнен (−126 локально/−1260 origin, auto-delete on).

Позже: HealthMonitor 2 с (C6, не чинить sticky-hang заново), GigaAM confidence consumers (#1985 — решение за владельцем).

**Сиблинг (не этот чат):** включение `cloud_rewriter_enabled` — отдельное «да» владельца (путь #1999 уже в коде).

### agy / визуал

1. Глоссарий «Все настройки» (245 ключей) — **DONE** (`SettingsGlossary.swift` + `docs/settings-glossary-ru.md`, двухстрочный UI, полнотекстовый поиск, паритет 245/245).
2. «Автозвонки» VG-native — **разблокирован** (C2 в колее #2000).
3. Разговор + селекторы из `list_llm_models` — **после** политики brain (дорожка B).
4. Пилот дешёвого визуала (GPT 5.4 Mini) — мелкий фикс с гейтом диффа здесь; жирные брифы остаются за Gemini 3.1 Pro High.

## Не делать

- Не чекаутить `audit/*`, не мержить PR #1875 (`krab_ru` hard-negatives).
- Не строить заново C2/C3; не «чинить» HealthMonitor sticky-hang заново.
- Не `REST_IN_PROCESS_ENABLED`; не голый `launchctl kickstart -k` под запись — `scripts/safe_backend_restart.command`.
- Не запускать собранный `KrabEarAgent` из воркера. Не `git add -A`. Не коммитить `wake_word_models/hard_negatives_raw/tts_phrases.json`.
- Не трогать Main Krab runtime / VG `.env`. Не второй EventBridge. Не wake word на SSE.
- **Никогда** `memory_conductor_enforce*` / `enforce_brain`. Не дообучать `krab_ru` синтетикой.
