# AppHang/Whisper — установленный выпуск и приёмка 16.10.2026

## Source, проверки и merge

- Whisper PR2094: reviewed head `ab5125f21ca77ee58e757021b85ba5a97a33e256`,
  merge `1267aec23e76cc3a8f81422f1e011efd92f94258`; полные деревья совпали.
  Два full Ubuntu backend1109files/16chunks/Aggregate0. Focused49; всего35 distinct
  затронутых test files локально PASS;14 audit gates/flake8/diff PASS.
  Локальный ubuntu-parity harness остановился до тестов из-за MLX в /tmp/py312;
  это не PASS. Полная Python3.12 проверка получена в hosted Ubuntu CI.
- Status PR2095: reviewed head `038135b219e99cf8a0258b6596409bb7c96e9d15`,
  merge `e996f30efc3ef7ba9ad497d423a7095b38e05dd1`. Оба full backend1107files/
  16chunks/Aggregate0; все три hosted Swift jobs PASS. Основной Swift CI:
  187 selected tests включая6 новых actual-callback WiringTests. Local62/
  independent63 selected Swift cases и release build PASS;25 Python guards PASS.
- Оба head:27checks SUCCESS/SKIPPED и3workflow SUCCESS. Final tree
  `7b221acd7713f2ec00d8461088ce72c7e1d40e39` точно совпал с immutable tree,
  получившим AstraUltra whole-diff PASS. Python тождественен2094, native2095.
  Deployed26542→base6f содержит только три документа.
- Final post-merge [CI38820208308 SUCCESS](https://github.com/Pavua/Krab-Ear/actions/runs/38820208308),
  [backend38820208434](https://github.com/Pavua/Krab-Ear/actions/runs/38820208434)
  SUCCESS:21 terminal jobs SUCCESS, backend1109files/16chunks/Aggregate0.
  Промежуточный backend1267 отменён следующим
  merge по workflow concurrency, не объявляется успешным.

## Что исправлено

Общий monotonic deadline ограничивает очередь flock/session, создание WAV,
start/send и каждый compatibility variant. Watchdog ставится до блокирующей
записи pipe; короткий state-lock не ждёт чужой I/O. Подтверждённый GPU error
не превращается в admission timeout. Generation retirement сохраняется.

Повторный health callback прежде делал20 image setter writes за10 одинаковых
idle ticks. Теперь gate сравнивает состояние и фактический результат render:
повтор не создаёт NSImage; title/image внешних изменений восстанавливаются.
Цвета, layout, переходы recording/processing/privacy/severity сохранены.
Это подтверждённый лишний render path, не доказанная причина AppHang.

## Runtime и E2E

На00:45CEST один offline synthetic RU Whisper E2E: local cached turbo,
HF offline, новая MLXWhisperSession, непустой русский результат за4.173s.
Собственный worker reaped; история23277 и прежние production generations
не изменились. Это отдельная квалификация worker-транспорта.

Новый runtime source **e996f30efc3ef7ba9ad497d423a7095b38e05dd1** установлен
16.10 в согласованном Main(history/audit)/VG окне01:30–01:55CEST. Старый
26542ccd locked release, before plists и полное старое app сохранены.
Новый release отдельный detached/locked/clean; он обычный Git worktree,
app-managed attachment не удался. Сборка переиспользована из d4f23feb:
production inputs тождественны038/final e996. Проверены167 source inputs,
6 archive checksums,3 pins,3 whole-app manifests, прежняя designated requirement,
strict/deep signature. Native/runtime не перекладывался.

Первая idle-попытка остановилась до эффектов на неизвестном кратком TCP-клиенте;
PID/назначение исчезнувшего соединения не установлены. Повторный полный gate
23:31:22–23:32:24UTC PASS: все пять activity/crypto/restore flags exactFalse,
count23277, стабильные parent generations, только аттестованные Swift↔REST
TCP-пары. HTTP paths этих трёх пар не установлены. После полного Swift drain
второй gate23:33:16–23:34:18UTC PASS с нулём ESTABLISHED. Перед backend bootout
проверки повторены; соседний CPU chunk тоже drained, launcher оставлен его
владельцем в STOP. Чужие процессы root не останавливал.

Все старые job/PID/descendants/PGID и endpoint полностью drained. После drain
pressure1/free60%. В23:37:58UTC оба plist заменены атомарно с fsync; whole-app
заменён Darwin RENAME_SWAP с fsync обоих parent directories. **До bootstrap**
verify after, byte read-back обоих plist, все manifests/signature/UUID PASS.
Interpreter/cwd/остальные env/logs сохранены, изменены только Python entrypoints
и PYTHONPATH. Canonical non-app WIP десяти путей не затрагивался.

Порядок startup backend→IPC+fresh GigaAM ready/engine warmup→RESThealth/sole5005
→Swift passive marker выполнен. Новые поколения (времяCEST16.10):

| Role | PID | Start | runs |
|---|---:|---|---:|
| Backend |1247|01:38:27|1|
| REST |1838|01:39:07|1|
| Swift |9366|01:40:03|1|

Loaded Python roots/PYTHONPATH указывают на e996 clean release. Actual Python
in-memory commit отдельно не аттестован. Native UUID
`8F0C7FE2-5700-3ECE-8708-DB8438082B4D`, dSYM UUID совпал/uploaded; существующая
signing requirement сохранена. Fresh startup AX trusted=true; native AX state
и screenshot получены без UI input/микрофона/вставки. UI-observation roundtrip
1.885s включает AX+capture/tool overhead, это не измерение paint latency.
После старта проверено37s неизменных PID/start/runs и strict health.

**23:41:22UTC15.10 =01:41:22CEST16.10:** один actual default REST synthetic ES
WAV, без request_profile, quality balanced/cleanup off/diarize false/
persist_history false/deadline30. **HTTP200; весь probe с проверками0.916s**, statusok, engine exact
mlx-whisper, languagees, nonempty text boolean, empty history_id. History23277,
все parent generations неизменны, strict idle после запроса. Текст не логировался;
профиль пользователя не менялся. Это один узкий default REST PASS, не приёмка
микрофона/auto-paste/PSTN или всех языков/моделей.

Quiet/maintenance lease освобождён; actual FINISH получили действующие
координаторы Main и VG. Соседние тестовые процессы возобновляет их владелец.
С23:42:38 до23:57:38UTC завершён read-only availability/parent-generation
monitor: **16 снимков /900.293s PASS**, IPC/REST доступны, PID/start/runs
неизменны, история23277. Все activity/crypto/restore flags exactFalse на
каждом снимке; после FINISH допустимая новая активность не запрещалась.

Read-only runtime/marker/package helpers и one-shot offline/REST probes
проверены отдельно. REST probe учитывает до30s очереди плюс30s выполнения,
содержит cleanup reserve и не повторяет неоднозначный timeout. Lazy child
не считается рестартом родителя; другой adapter не проходит как Whisper.

## Открытые границы

Пять разобранных AppHang принадлежат прежнему UUID EE6DD5B0. Установка до
выкладки16.10 имела UUID C8197C13; текущая —8F0C7FE2. Main stack — AppKit/layout/drawing, app frames —
entrypoint/MainActor. Пользователь не помнит конкретного действия перед
зависанием. Причинная цепочка исторических AppHang/504 не установлена;
Sentry issues не закрываются по оптимизации и одному synthetic PASS.

Sentry checkpoint23:27:32UTC15.10: успешный query
`is:unresolved lastSeen:-48h`: backend0, agent2старых issue с lastSeen13.10.
Новый Ear ingress этим не доказан. Финальная сверка23:57:45UTC: оба API200,
query `lastSeen:>2026-10-15T23:37:58Z` вернул0 issues на обоих проектах;
отдельный `is:unresolved`/statsPeriod14d показал15 agent и12 backend старых
открытых issues, без pagination. Два прежних recent agent issue уже вышли
из48h окна; это не их разрешение. Fresh Ear ingress остаётся UNKNOWN.
Ошибочный statsPeriod48h дал HTTP400;
исправленный query с statsPeriod14d успешен, failed receipts сохранены.

Ограничение default REST30s queue+новый30s inference не изменено этим diff;
client disconnect не отменяет server work. Оно учитывается smoke-процедурой.
Real encryption activation/migration/purge/rotation, PSTN, Main/VG lifecycle,
OrbStack/VPN и пользовательские настройки не выполнялись root Ear.

## Closeout

15min monitor и fresh Sentry read-only сверка завершены. После выкладки
повторная package проверка PASS:167 source inputs/6 archives/3 pins/3 app
manifests, strict/deep signatures и UUID/dSYM. Десять canonical non-app WIP
hashes/modes, branch и HEAD совпали с baseline. Runtime source остаётся
e996f30e; отдельный closeout docs PR не требует повторного runtime restart. Канон:
[безопасный cutover](../plans/2026-09-29-safe-release-cutover.md).
Sol6.1High для механики; AstraUltra точечно для нового high-risk gate.
