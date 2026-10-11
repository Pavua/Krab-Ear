# A5.4 — Timeline fail-closed и synthetic crypto IPC, 2026-10-11

## Кандидат и исходная волна

Работа в `codex/ear-failclosed-followup-20261011`, отдельный managed worktree,
поверх `5e30937619d91aaf6bf4afa0a01385caf877f352` из
[PR #2084](https://github.com/Pavua/Krab-Ear/pull/2084).
Исходная ветка уже опубликована: 15 commits / 33 files поверх `16d2b226`,
27 exact-head CI checks SUCCESS; independent Astra Ultra whole-diff PASS без
новых P0–P2. Daily `a0311e19` повторяет уже включённый source, cherry-pick нет.
Shared checkout `chore/agent-parity-0928` и чужой WIP сохранены.

Followup [PR #2085](https://github.com/Pavua/Krab-Ear/pull/2085) DRAFT;
проверенный source/test commit
`f1b86d10bf507f1074674401a35c5a80ce91f524`. Отдельный docs commit содержит этот
receipt, поэтому CI head проверяется по PR, а не приравнивается к source commit.
На момент публикации новый CI pending; финальный exact-head receipt сохраняется
в PR body/checks. Base `codex/krab-ear-v2` запускает оба workflows; незамерженный
#2084 входит в общий diff, после его merge diff сузится.

## Исправленное поведение

Все три Timeline handlers (`export_timeline_svg`, `export_timeline_json`,
`export_timeline_ical`) перестали превращать ошибку чтения истории в `[]`.
Неверный ключ и повреждённый GCM tag теперь дают sanitised верхнеуровневый
IPC `internal_error` до создания каталога/файла; `result` отсутствует.
Настоящий пустой журнал по-прежнему экспортируется штатно с `blocks=0`.
Authorizer и повторная policy/grant проверка непосредственно перед записью
сохранены. Swift production-flow не менялся; комментарии и IPC reference
исправлены под фактическую форму ответа.

## Проверки и текущие gates

Правильный RED до изменения `service.py`: все 6 actual-AES-GCM случаев
wrong-key/tag × 3 формата вернули `accepted=true`, `blocks=0`, `files=1`.
Protected history/settings и isolation/scanner assertions до RED проходили.
После минимального source fix те же 6 случаев, 3 true-empty controls и
6 Timeline dispatch/source-contract checks GREEN.

Полный новый файл: **30 PASS**, Python 3.12.7, macOS, 286.13 секунды:
18 production Swift coordinator → IPCClient → Unix IPC и 12 direct Python →
Unix IPC. Реальные BackendService, StateStore и AES-GCM; заменён только
источник случайного RAM-only fixture key. Проверены все три формата,
grant/decline/privacy, живой prepared ticket после revoke/privacy/restart,
сохранность прежних exports, true-empty и wrong-key/tag.
После stale refusal отдельный production `get_history_page` подтверждает
сохранность записи, actual decrypt и неизменность количества выданных grants.

Файлы/stdout/stderr/events/audit проверяются на actual auth/key representations
до разбора содержимого и сравнения bytes. Swift summary использует только
фиксированные Failure enum labels; `transport` не проходит как ожидаемый
`privacy`/`expired`. Scoped spec/quality review PASS.
Default crypto provider-deny, Keychain/network/native-ML/filesystem guards,
positive controls и закрытие owned BackendService/children сохранены.

Selected-file Python 3.12 parity: **83 PASS + 27 subtests**, exit0: новый файл30,
legacy plaintext integration27, history crypto10, Timeline/scheduler16+27subtests.
Новых worker leaks нет; venv переиспользован без install/rebuild/purge.
Independent final Astra Ultra gate **PASS** на неизменной дельте6files;
предположение о pytest inline-subscript diagnostic leakage опровергнуто
exact sentinel probe с положительным funcargs control, лишней правки нет.
`make audit-all`: все **14 gates PASS**, exit0. Public-runner lint и полная
native build проверяются новым exact-head CI; локальный Python3.12 venv не
содержит flake8, дополнительные зависимости не устанавливались.
Полной сборки приложения локально нет: компилировались только три исходника
native integration harness. У публичного Linux CI Darwin-сценарии будут skip;
это не замена локальным 18 Swift-проверкам.

## Границы и следующий шаг

Нет merge/deploy, production restart, реальной истории/Keychain,
activation/migration/purge/rotation или соседних mutations.
Release `9d7a0aac` от 10.10 остаётся историческим checkpoint; текущие
PID/health/loaded module SHA эта source-карточка не аттестует.
Timeline ON live consent-sheet и Keychain recovery требуют отдельного scope.

Отдельно остаются P3 migration close/late-callback SSE race и прежние
sharing corruption-after-start / scheduler transient-read RMW ограничения.
Эта карточка их не закрывает. Локальная source-карточка **100%**; далее
проверить CI точного нового head #2085 и отдельный source fix P3 SSE race.
Для механики Sol 6.1 High; для security
whole-diff gate Astra Ultra. [Карточка](../plans/2026-10-11-a54-timeline-crypto-ipc.md).
