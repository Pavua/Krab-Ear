# Ear — композиция открытых PR и REST error contract

База: `002583c73112a33fd484fa7793ce662fe6dca637`, поверх remote
`165526430d6c4d378b69da5abfa71eb6076982c9` (ls-remote подтверждён 29Oct).
Owned isolated branch: codex/ear-rest-contract-integration-1029.
Scope: source/offline подготовка; без публикации, merge, runtime, STT/audio,
моделей, клона, OrbStack. Для исполнения отдельный Main START и reviewed guard.

## Source composition map

Три PR повторно проверены через GitHub: OPEN, не merged. Старые результаты CI
из body исторические; новые CI не запрашивались и не запускались.
Трёхсторонний text-only merge каждого файла против c44db7d9 и owned002583:

| PR / exact head | Files | Конфликты / приёмка |
| --- | --- | --- |
| 2120 / e56966ede3d91873f088660c32c5501b50bc8e25 | 3 | source text clean; текущая REST close() сохраняется |
| 2108 / 5974ca33818367b578da090843351ddf7db69552 | 7 | docs/NOW.md; TTS observer не доказывает полный drain |
| 2119 / 2fcf5014866c3571afd962d34172d71997a8f216 | 27 | live_subs_service.py, NOW.md, HealthMonitorTests.swift |

Это текстовая карта, не проверенная совместная композиция и не behavior GO.
У 2119 replay emit заменяет тот же участок, который merged2126 защищает
_delivery_lock/_window_is_current. Нельзя выбирать одну сторону: сохранять
invalidation/generation, worker ownership, privacy и сериализацию delivery.
REST finally должен сохранить close(), не вернуть старый reset().
service.py text clean не доказывает новый ingress lifecycle/purge/privacy.
Swift conflict source-сопоставлен:2136 заменяет только300ms sleep на bounded
waitForState(.hung);2119 переводит также healthy/counter cases на callbacks,
а counter fixture меняет на fail/success/fail/fail и проверяет4вызова.
Оба устраняют wallclock race, но не являются побайтовыми дублями.
Не возвращать fixed sleep и не терять проверку сброса счётчика. Swift не запускался.

## Первый минимальный перенос: 2120

Actual AudioEngine может вернуть status=error/error. Общий REST handler текущей
базы преобразует его в HTTP200/status=ok/empty text, пишет successful metrics.
Call-auto использует этот же путь; отдельный call-RU уже имеет error mapping.
Перенести существующие 10 строк, не писать вторую альтернативную реализацию.

Exact ответ ошибки: HTTP503 {"status":"error","error":"stt_failed","text":""}.
Privacy имеет приоритет403; обычный путь повторно проверяет privacy fail-closed.
Настоящая тишина/обычный текст сохраняют200. Engine details не выдавать.
Gateway получил exact contract; error_code не является ключом этого ответа.

- [x] Source map и exact PR schema сверены.
- [x] Старый regression test скопирован побайтово из e56966e, пока без source fix.
- [x] Подготовлен stdlib AST suffix harness: 3 метода,14 subcases, без app imports.
      Независимое Astra SOURCE_GO получено для final47a1835b; RED/GREEN ниже.
- [ ] Reviewed isolated execution profile: scratch HOME/TMP/cwd; deny network,
      actual device/provider/model/process starts; no production reads/writes.
      Альтернативный первый causal gate исполняет только AST suffix через
      test_rest_stt_result_slice_offline.py; full Flask regression остаётся отдельно.
      Для оставшегося full Flask gate import test_rest_vg_contract_M1
      транзитивно загружает core.engine и REST;
      нельзя запускать старый pytest напрямую как гарантированно безопасный.
- [x] Main fresh START: causal RED actual7 failing subcases:6 engine-error
      200vs503 + ordinary error/privacy200vs403; call privacy уже PASS.
      3 test methods/14subcases,0errors,0.136s; outer0.274s, pid41491exit1,
      reaped/PGIDempty/no timeout/hashstable; raw receipt и stdout прочитаны.
- [x] Перенесены exact10existing lines из2120; REST close() текущей базы сохранён.
- [x] Независимое Astra whole SOURCE_GO exact productione5447b1 и harness47a1835b.
- [x] Main fresh GREEN:3methods/14subcases PASS/0.131s, outer0.246s;
      child44380exit0/reaped/PGIDempty/no timeout/interruption/hashstable.
      Raw GREEN_RECEIPT.json/GREEN_STDOUT.log прочитаны; FINISH отправлен Main.
      Это actual Python suffix gate, не полная Flask/HTTP приёмка.
- [ ] Зависимые REST/Ubuntu-parity гейты в согласованных отдельных окнах.

Запуски этой карточки: один causalRED и один freshGREEN в разных Main окнах. Старые56GREEN не квалифицируют новую базу.
Даже новый localGREEN не заменит live wiring/hostedCI/release qualification.

## Maintenance boundary

Согласие владельца на legacy risk сохранено: после окончания диктовки,
согласованная STT-only пауза Gateway, затем Ear с откатом; клон/модели/OrbStack
защищены. Фактического STT pause ACK нет. Gateway подтвердил текущий source-only
scope и не выдаёт pause ACK. Выкат не начинался. Старый22a3 bundle не считается
свежеквалифицированным кандидатом для новой композиции.

## Исторический intake

Frozen100 historical plans: автоматический full-text scan100/100 и metadata
review100/100; полное чтение моделью17/100 в сохранённом registry, full behavior
verification0. Missing references могут означать intentional deletion/placeholder,
а unchecked старой карточки не доказывает текущий bug. Registry и text merge
receipt: ear-diagnostics-audit-20261029/{HISTORICAL_PLANS_REGISTRY.json,
PR_COMPOSITION_MAP.json} в локальном visualization artifact root этого чата.
Central Sentry peerreceipt: metadata17/17 unresolved backend,0agent;
releasekrab-ear@2.4.0 без gitSHA, latest Oct27. Это не current runtime qualification,
не17 независимо подтверждённых багов и не полный человеческий review новых событий.
