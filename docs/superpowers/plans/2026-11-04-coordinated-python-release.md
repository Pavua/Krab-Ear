# Координированный Python release Ear — подготовка, HOLD

Scope 04.11: владелец после четырёх отдельных draft/CI разрешений передал через
текущий Gateway поручение merge/deploy готовых исправлений и повторный сложный
звонок после стабильности. Текущим Main/Gateway разрешено доверять как владельцу.
Это не разрешает bypass CI/idle, очистку данных, смену ключей/моделей, остановку
clone/OrbStack/VPN, вмешательство в неизвестные процессы или чужой dirty WIP.

## Кандидат

`codex/ear-release-compose-1104`, база `6e2043fb`, сохраняет ancestry всех четырёх
проверенных веток: #2160 `be9f8a71`, #2161 `2c1d95a5`, #2162 `1acf605c`,
#2163 `c8cee205`. Единственные merge conflicts были в NOW; рабочий код объединён
без изменений. Все шесть production/helper файлов byte-identical исходным
reviewed heads. Карточки отдельных фиксов содержат RED/GREEN и review evidence.

Whole runtime-diff review выявил дополнительную регрессию phonetic seed:
успешное удаление новой curated-записи отменялось следующим чтением. Добавлены
однократная versioned миграция и bounded межпроцессный flock для двух writers
(backend/REST). 54 теста на каждой из Python 3.12/3.14, включая два реальных
процесса; независимый review дал source GO. Подробности в
[карточке](2026-11-04-phonetic-seed-migration.md). Это дополнительное изменение
runtime candidate; исходные шесть production/helper файлов четырёх веток сохранены.

Итоговый release требует отдельного exact combined CI и post-merge CI. Green
одного исходного PR не является приёмкой комбинации. До terminal результатов
merge/cutover HOLD. Дополнительно сильное read-only ревью фактического Python
runtime delta от `5a09e630`, а не только сегодняшних patch.

Python backend и REST переключаются вместе в новый clean locked detached root.
Swift app/бинарники общего dirty checkout не пересобираются и не заменяются.
Не запускать build_and_deploy/run_release_checklist/run_smoke_release: там есть
посторонние build/agent lifecycle действия. Настройки/profile/data-dir/interpreter
сохраняются, plist меняет только entrypoint и PYTHONPATH.

## Свежие runtime gates после CI

1. Exact source/review/post-merge SHA; оба release roots clean/detached/locked.
2. Sentry auth/freshness/ingress и актуальные события, без подмены пустого ответа
   доказательством отсутствия ошибок. Утренние wakeword/shutdown issues не закрыты
   восстановлением cache. При неизвестной обязательной квалификации — HOLD.
3. Loaded launchd/PID/entrypoint/PYTHONPATH соответствуют старому `5a09e630`.
   Swift supervision подтверждённо passive; active/UNKNOWN — HOLD.
4. Текущее quiet/resource окно от Main и владельца; нет диктовки/встречи/finalizing.
   Gateway подтверждает все свои outbound Ear tasks закрыты и новых не будет.
   Общий server drain старого runtime не объявляется доказанным по health/счётчику.
5. Private 0700/0600 bundle `prepare_release_cutover.py` со strict before/after
   hashes и backups. Не печатать/коммитить plist/env/секреты. Старый release,
   venv, cache и backup не удалять.

## Согласованный порядок — только после отдельного live START

Предварительный вариант совместим с штатным Gateway deployer без его обхода:

1. Gateway своим штатным механизмом останавливается при нулевых call/recovery
   tasks. Clone остаётся. Его owner подтверждает прекращение всех outbound Ear
   requests. Main FINISH и запрет новых Ear-запросов действуют на всё окно.
2. Ear четыре раза за >=60s выполняет `safe_backend_restart.command --check-only`;
   дополнительно сверяет tracked native/inflight/unknown work и границы coverage.
   Неполный/UNKNOWN idle или новая работа — HOLD, не автоматический kill.
3. Выполнить existing safe cutover из
   [процедуры](2026-09-29-safe-release-cutover.md): verify before + последняя
   immediate idle-проба, bootout backend/REST с подтверждением исчезновения
   обоих job/PID, atomic replace двух plist, verify after.
4. Bootstrap backend → bounded валидный IPC ping/new PID/exact loaded paths,
   затем bootstrap REST → bounded health/listener/new PID/exact paths.
   Read-only smoke: история без текста/счётчик, профиль, encryption/restore state,
   отсутствие restart loop. Это ещё не live ASR или PSTN acceptance.
5. После Ear GO Gateway поднимает старый проверенный plist, проверяет idle/health,
   затем штатным deployer переключается на новый проверенный SHA с rollback.
   Два старта Gateway и временная недоступность входящих учитываются явно.
   Ear в это время повторно не перезапускается.
6. После общей стабильности и свежего resource admission допустим bounded
   speech check; только затем Gateway единолично диспетчеризует разрешённый
   повторный сложный звонок. Не запускать дублирующий вызов из Ear/Main.

## Отказы и откат

Использовать existing rollback matrix, не безусловный trap/force. До bootout
ошибка оставляет старый runtime. После старта нового backend откат требует
нового idle gate: новая диктовка/UNKNOWN запрещает автоматическое завершение.
При частично заменённых plist не запускать смешанную пару; verify bundle,
восстановить оба before и порядок backend→REST, затем exact SHA/PID/smoke.

Старый `5a09e630` читает новый phonetic JSON, однако его CRUD удаляет metadata
версии seed. Если после отката редактировался словарь, повторное обновление
требует отдельной проверки marker и пользовательских удалений. Автоматически
восстанавливать старый snapshot entries нельзя: это затрёт новые правки.

## Состояние подготовки

Candidate source составлен; точные CI ещё выполняются. Runtime не изменён,
общий live START/quiet окно ещё не выдан. Предыдущий узкий ES speech probe и
первый PSTN-звонок не являются приёмкой этого release.
