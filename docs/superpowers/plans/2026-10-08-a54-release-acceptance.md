# A5.4 — план release и приёмки, 2026-10-08

Это source-only подготовка проверяемых этапов после
[A5.3 source acceptance](../handoffs/2026-10-08-a53-source-merged.md).
План не разрешает production lifecycle, реальную историю/Keychain, encryption
activation, удаление/ротацию, Main/Gateway или отправку внешних сообщений.
Он не свидетельствует о готовом release bundle или проверенном live runtime.

## Состояние и обязательные доказательства

| Этап | Состояние на дату плана | Условие закрытия |
| --- | --- | --- |
| A5.3 source integration | PASS, 7/7 merged | Exact `9fee1ef4` и оба post-merge CI; ссылки в source report |
| Подготовка кандидата/rollback | Не выполнена | Свежие runtime metadata, совместимые old/new roots, проверенный private bundle |
| Production cutover | Не разрешён, не выполнен | Конкретный bundle/review, окно простоя и явный owner scope |
| Live UI при OFF | Не выполнена | Реальные затронутые handlers и безопасные owner-approved fixtures |
| Encrypted acceptance | Не доказана | Отдельный crypto fixture E2E, затем отдельно разрешённая live qualification |
| Activation/старые копии | Не разрешены | Recovery, inventory, совместимость и отдельное решение владельца |

Источник контракта — [спецификация A5, §8–9](../specs/2026-09-24-a5-history-at-rest-design.md).
Исходные тесты policy ON используют plaintext fixtures с запрещённым crypto
provider. Не переименовывать их в encrypted-history acceptance.

## 1. Подготовить конкретный release, прежде чем запрашивать cutover

Следующая карточка должна явно перечислить допустимые metadata-only runtime
пробы и staging, запрещённые источники и единственного lifecycle-оператора.
Текущая карточка документации их не выполняет. Исторические PID и release SHA
не являются исходными значениями нового переключения.

1. Сверить свежую remote базу, exact-SHA review/CI и target tree. Если candidate
   меняется, получить проверки именно новой дельты; docs PASS не заменяет code gate.
2. Read-only metadata в разрешённом scope: фактически loaded launchd jobs,
   PID/module origin, release/config SHA, interpreter, data-dir/socket/log paths;
   не печатать env, plist, токены, transcript/settings values. Проверить текущий
   режим Swift supervisor; active/unknown вместо подтверждённого passive — HOLD.
3. Подготовить immutable clean detached old/new release roots и проверенный
   staging bundle существующим `scripts/prepare_release_cutover.py` по
   [safe-cutover runbook](2026-09-29-safe-release-cutover.md). Сохранить interpreter,
   данные, endpoints и прочую конфигурацию; private byte-exact backups 0700/0600.
   Bundle может содержать секреты: не прикладывать его к PR и внешним агентам.
4. Установить совместимость rollback: старый код читает ожидаемые формат/настройки,
   rollback не зависит от удаления или активации encryption. Проверить signing
   identity и соответствие разрешённому TCC requirement; не сбрасывать permissions
   вслепую. Не строить/не подписывать live `.app` под текущей карточкой.
5. Сверить RAM/swap, свободное место и соседние тяжёлые процессы перед build.
   Подготовить конкретное окно, downtime, operator, old/new SHA и процедуру отката;
   получить независимый Astra whole-diff/release gate и явное owner разрешение
   на перечисленные lifecycle действия. Прежний SOURCE PASS не даёт release GO.

Результат этапа: приватный проверенный bundle + redacted evidence card.
Нельзя заявлять его готовность без фактического stage/verify.

## 2. Разрешённый cutover — отдельное окно

Применять существующий runbook, без второго lifecycle-скрипта.
Обычный same-path backend restart — только `scripts/safe_backend_restart.command`;
при смене launchd путей требуется reviewed bootout → подтверждение исчезновения
старых jobs/PID → bootstrap: `kickstart` не перечитывает изменённый plist.

До остановки: owner maintenance window, отсутствие записи/meeting,
подтверждённый passive Swift supervisor, отдельно согласованное quiet window
REST клиентов (backend idle не покрывает REST STT/TTS/streams).
Idle snapshots на 0/20/40/60 секунд и непосредственно перед stop по runbook;
`--check-only` — снимок, не lock записи. BUSY/UNKNOWN, активный supervisor,
неучтённый REST клиент, изменившийся bundle или невозможный rollback — HOLD.

После: доказать loaded candidate SHA/PID/module origin/config, IPC/REST health
и безопасный smoke без реальных записей, если именно это разрешено.
Откат также требует свежего idle gate. Рестарт Swift — отдельный явно названный
scope; Backend/REST cutover сам по себе его не разрешает.

## 3. Live UI и encryption — разные приёмки

| Проверка | Нужное свидетельство | Что она не доказывает |
| --- | --- | --- |
| Dictation → Codex | Owner-focused поле, фактическая авто-вставка без Cmd-V | Export consent, migration или encrypted storage |
| OFF export UI | Реальные SavePanel/PDF/MeetingReport/QuickCapture handlers, согласованные тестовые данные/выходы | Доступ к real-history и ON encryption |
| Synthetic crypto E2E | Отдельный профиль/случайный тестовый ключ, настоящий crypto provider, read/write/restart/failure paths | Keychain recovery владельца и production acceptance |
| Разрешённая live encrypted qualification | Согласованные данные/ключевой scope, recovery и согласованные outputs; только redacted/boolean отчёт | Автоматическое право мигрировать всё или удалять копии |

Сначала source-карточка проверяет существующее coverage и добавляет лишь
недостающие behavioral tests с RED→GREEN; новый isolated crypto test не должен
наследовать crypto-deny trap D и обещать ON acceptance. Не обращаться к системному
Keychain или пользовательской истории ради synthetic проверки.

**Timeline ON LIMIT:** сейчас нет session context, backend fail-closed.
Для полной ON UI-приёмки нужна отдельная source-карточка wiring и review/E2E.
Если release сознательно сохраняет LIMIT, это отдельное документированное решение
владельца; сохранённый отказ не превращается в PASS функциональности.
Не обходить authorizer, не переносить consent между окнами и не обещать peer-auth.

## 4. Activation и старые plaintext-копии

Включение флага, migration, inventory пользовательских каталогов, recovery,
backup/restore и удаление/ротация требуют конкретного отдельно разрешённого scope.
Перед activation: metadata-only inventory согласованных каталогов, UNKNOWN
не считать пустотой, validated migration/recovery и rollback compatibility.
Не сканировать весь home и не читать диктовки/контакты для inventory.
Сначала безопасная проверка восстановления; отсутствие старых plaintext-копий
не заявлять до согласованного учёта и подтверждённого действия над ними.

## Закрытие карточек и экономия квоты

Каждый выполненный этап фиксирует: scope, UTC timestamp, exact source/release SHA,
CI URLs, PID/module origin только если реально проверены, checks/outcomes,
HOLD/известные LIMIT, rollback и следующее разрешённое действие.
Обновить NOW, журнал волн и handoff; source/local/CI/merge/deploy/live статусы отдельно.

Sol 6.1 High — координация и обычная реализация. OpenCode Muse Spark 1.3 Free
и agy Gemini 3.8 Flash — изолированные routine-задачи после проверки доступности
маршрута; effort по сложности. 5–10 workers только при независимом владении,
ресурсном бюджете и без общего lifecycle. Авторизация передачи контекста
провайдерам должна соответствовать scope. Бесплатные scouts не заменяют
независимый Astra gate для security/whole-diff/release decisions.

Проверка маршрутов 08.10: две read-only подзадачи завершились в настоящем
OpenCode CLI на `opencode/muse-spark-1.3-contributor-free`; export metadata
подтвердили provider/model, cost 0 и отсутствие tool calls. agy scout завершился
на `gemini-3.8-flash-medium` (квота agy, не обещание free). Первая попытка
OpenCode с deny-all tools получила 403; scoped ask-permissions профиль с
edit deny/MCP disabled дал ответ. Concurrent CLI startup также выявил
SQLite `database is locked`; успешная повторная проверка выполнена последовательно.
Это снимок доступности маршрутов, не гарантированная будущая доступность.
