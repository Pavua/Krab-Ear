# REST Flask — профиль изоляции настоящего request pipeline

**Goal:** квалифицировать actual REST import и ранние auth/privacy HTTP-отказы
в disposable процессе, не подменяя внутренние handlers/constructors.
**Architecture:** настоящий standalone REST→Flask test_client; внешний provider
недоступен, I/O только scratch, все thread/process/network starts запрещены.
**Tech Stack:** существующий Python3.12.7/Flask/stdlib audit guard, без установки.

База c9e62600; owned ear-rest-contract-integration-1029. Source-only preparation,
attempt1 STOP before HTTP; нового PASS нет. Без runtime/daemon/model/audio device/credentials/maintenance.
Перед исполнением strong SOURCE_GO + fresh Main START, не старое GREEN окно.

## Карта побочных эффектов actual import

| Узел | Поведение исходника | Профиль |
| --- | --- | --- |
| core.config | читает HOME settings/.secrets и cwd.env | scratch HOME/cwd; env allowlist до child; guard forbids nonscratch config |
| REST globals | actual AudioEngine(skip_gigaam_warmup=True), StateStore, Transcriber, PhoneticVocabService, Translator, TTSService | реальные constructors; ML/audio imports unavailable, no internal constructor mocks |
| token propagation | читает canonical settings либо ownstore | оба пути обязаны указывать scratch, токенов в fixture нет |
| rest logging | process-local StreamHandler/root level | disposable child stdout, не daemon logs |
| metrics/eventbus | process-local singleton/locks | реальные, не shared daemon |
| TEMP_DIR | создаёт DATA_DIR/temp_uploads | scratch only |
| create_app | Api/blueprints/CORS/limiter/Sock register | настоящая Flask фабрика+test_client, сервер/socket не запускается |
| reaper | start только run paths, не import | не вызывать run/start; любой Thread.start в первом профиле FAIL |
| atexit | _rest_engine_cleanup зарегистрирован | явно выполнить actual scratch cleanup и unregister до receipt |

Переносится существующий Boundary из backend_lifecycle_probe (reviewed ранее),
но он запрещал backend.rest_server. Единственное module expansion — разрешить
actual REST import; Thread.start теперь запрещён полностью. Никаких подавленных
scheduler loops/подмен внутренних конструкторов. Предыдущий lifecycle PASS не
доказывает, что новый full REST import пройдёт этот профиль.

## Шаг A — import/auth/privacy prerequisite

Prepared rest_flask_import_probe.py. Actual standalone globals, create_app и
POST /v1/stt/transcribe; три early-return случая: missing Bearer401,
invalid disposable Bearer401 и privacy403 при отсутствии Bearer. Реальные
require_api_key/_privacy_gate/StateStore/RestAuth. Никаких транскрибаций,
StaticDeps/моков движка/сетевого сервера; guard запрещает любые worker starts.
Результат A не является приёмкой engine error503 или upload/executor pipeline.

Exact parent: rest_flask_probe_parent.py; запуск интерпретатором
/opt/homebrew/anaconda3/bin/python3 (существующийPython3.12.7). Distribution
metadata Flask/smorest/sock/limiter/cors/marshmallow/pydantic-settings проверены
без app import. /opt/homebrew/bin/python3.12 этих зависимостей не имеет;
не подменять missing-deps mocks и не устанавливать новые пакеты автоматически.
Parent создаёт scratch/home/tmp, запускает тот же Python3.12-I-B с cleanenv,
PYTHONTZPATH='', KRAB_EAR_DATA_DIR=scratchhome/Library/Application Support/KrabEar,
KRAB_EAR_MLX_WHISPER_WORKER=0 и отключёнными реальными featureflags из full
lifecycle parent (LLM/GigaAM/REST-in-process/bridge/backup/recap/watchdogs/seed/
semantic/wakeword); BLAS1. OS deny-network и inner I/O/import/thread guards
до app imports. ONE child ownPGID45s+cleanup5, raw receipt/stdout, hashstable,
reaped/PGIDempty. Parent требует ровно один parsed child receipt с actual
import=True/http_cases=3/violations=[]/worker_starts=0 и ожидаемым scope;
одного exit0 недостаточно. Hashstable —12productionfiles+3fixturefiles,
не весь import closure. Python thread enumeration не является OS/native thread
census; BLAS1 и provider starts forbidden — отдельные ограничения.
Cleanup в outer finally, SIGINT временно игнорируется только при own cleanup,
communication/cleanup failures дают fail-closed receipt/rc1. Аварийные ветки
самого parent ещё не воспроизводились отдельными тестами.
При side-effect denial не расширять профиль автоматически.
Первый независимый Astra SOURCE_GO был получен для exact parent3e7786a506f69c202c2aee0c62196d434ac5bde76b53a1f75ebdf3ebed29065f,
probe1b6708305ac7a717601dfc463d55a490f6fec40042b88aaba713a9640568c556,
boundary1a43da7a48e90a71fc42779f30ceb021e6ea168d04517807aaa164e4337f3192.
После этого attempt1 остановлен thread guard (см. ниже). Текущий follow-up
с RATE_LIMIT_ENABLED=false требует нового GO/START; не использовать старое окно.
GO относился только к этапу A, не к этапу B/аварийным веткам parent/runtime.

## Шаг B — STT errors через upload/real executor

Не реализован/не исполнялся. Actual route создаёт ThreadPoolExecutor(1),
исполняет stt_budget.call_in_scope→Transcriber→AudioEngine. Fake executor,
fake AudioEngine/Transcriber или AST-only handler не закрывают этот gate.
Нужен reviewed exact allowlist для одного настоящего executor thread с join
и без MLX/GigaAM/provider workers. Это отдельное расширение A, не разрешённый
скрытый start. Только внешние provider boundaries допускают искусственный
результат/ошибку; internal retry/fallback/privacy/status wiring сохраняется.

Перед B: source-map фактического STTRouter/WhisperAdapter provider entrypoint,
normalization/soundfile и fallback/LLM flags. Synthetic WAV только в scratch;
нет записей с микрофона/истории/реальных токенов. Для ordinary/call-auto error
проверить503 stablecode, privacy403priority, auth401, legitimate silence200,
nohistory/nosuccessmetrics наerror и uploadcleanup; настоящий call-RU IPC вне
покрытия без отдельного локального IPC fixture. Shared daemon не использовать.
Full Flask означает in-process test-client wiring, не TCP/live/audio E2E.

## Покрытие исторических планов

Frozen100: scan100/metadata100/fulltext17/behavior0 по сохранённому registry.
Эта новая карточка не увеличивает количество прочитанных исторических планов.
Никакого whole-audit GO;2119composition/recovery остаются отдельной очередью.

## Attempt1 — остановлен защитой, не PASS

Main freshSTART выполнен на reviewed3e7786a/1b670830: child9770exit1,
2.144s/reaped/PGIDempty/no timeout/hash15stable. RawIMPORT_RECEIPT.json и
IMPORT_STDOUT.log прочитаны, FINISH/RUN0 отправлены. Импорт дошёл до
module create_app→limiter.init_app→limits MemoryStorage.__init__→timer.start;
Boundary запретил Python thread. Ни один из3HTTP cases не был выполнен;
child success receipt отсутствует, parent верно вернулFAIL. Это не продуктовый
RED для STT/privacy и не доказательство невозможности full import.

Source follow-up: installed FlaskLimiter.init_app возвращает до создания
storage при enabled=False. В Ear enabled=settings.RATE_LIMIT_ENABLED;
штатный KRAB_EAR_RATE_LIMIT_ENABLED=false documentedtestconfig. Новый профиль
выключает этот флаг в clean childenv и assertFalse до RESTimport; никакие
threads не разрешаются/не подменяются, real auth/privacy route сохраняется.
Rate-limit behavior приёмкой не покрывается. Требуются новое source review и
новый Main START; attempt1 не перезаписывать, автоматического retry не было.

## Attempt2 — stage A PASS, bounded prerequisite complete

Новый MainSTART после свежего AstraGO exact e10dbffa/c65466ca/Boundary1a43.
Actual full REST import выполнен; Flask POST вернул401 missingBearer,
401 disposable invalidBearer,403 privacy безBearer. Childreceipt ровно один,
строго валиден; violations0/worker_starts0. Child17660exit0/reaped/PGIDempty,
no timeout/interruption/communication or cleanup error, elapsed2.244s,
15explicit hashes stable. Raw parentreceipt IMPORT_RECEIPT.json и
IMPORT_STDOUT.log прочитаны до MainFINISH; EarRUN0/windowreleased.

Локальные artifacts: visualizationroot этого чата /ear-diagnostics-audit-20261029/
rest-flask-import-attempt2/. Attempt1 сохранён отдельно, не переименован в RED.
Staticflake8 двух fixturefiles PASS, diffcheckPASS. Parent после теста исправлен
только форматированием: finala255891c654123619118cd3209ee4271556b383ecf440aba5e6a0da870e5062c.
Exacttested e10 восстановлен и его hash проверен, AST equals final; artifact
TESTED_PARENT.py и FORMAT_EQUIVALENCE.json. Повторного прогона не было;
хеш успешного запуска именно e10, не finala255.

Stage A source/profile/execution100%. Это не STT503/upload/executor/provider,
rate-limit/429/CORS, TCP/live/audio/model E2E или реальный server drain.
Production sources в этой карточке не менялись. Stage B остаётся source prerequisite,
отдельный review/newSTART; на этом checkpoint новых запусков не запрашивать.
Sol6.1Medium подходит для следующей source-карты; ownership/concurrency whole-gate
— сильное независимое review. Модели пользователя не переключались.
