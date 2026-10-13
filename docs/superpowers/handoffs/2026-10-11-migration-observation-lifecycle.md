# Migration observation lifecycle — source checkpoint перед публикацией

## Проверенная база

Remote колея `origin/codex/krab-ear-v2=16d2b22678474c871cfad9eb4c09c75f4b7d0cd5`
после свежего fetch. Owned managed worktree
`/Users/pablito/.codex/worktrees/ear-migration-lifecycle-20261011/Krab Ear`,
branch `codex/ear-migration-lifecycle-20261011`, BASE
`5d0ed47fa5d0602eda9ef23171b47f5e453c3407`. Stack включает незамерженные
#2084/#2085; предыдущий PR2085 сохранён, его source/CI receipt не доказывает
новую карточку. После интеграции parents сверить ancestry и новый CI при rebase.

## Задача и доказательства

Close в HistoryPanelController отменяет текущий SSE, но pending IPC/retry/terminal
и confirmation closures не инвалидируются. Один production Foundation
coordinator должен владеть lifecycle и polling, panel — реальными IPC/AppKit/SSE
adapters. Реальные held callbacks + close→reopen A/B controls требуются; простого
проверяющего epoch helper и source-text assertion недостаточно.

[Спека](../specs/2026-10-11-migration-observation-lifecycle.md) /
[план](../plans/2026-10-11-migration-observation-lifecycle.md).
Ignored ledger `.superpowers/sdd/2026-10-11-migration-observation-lifecycle/`.
RED после уточнения fidelity: snapshot без новых entry guards, close сохраняет
старый polling state; compile exit0, 8/8 controls PASS, полный suite14/44 PASS,
30 observable FAIL exit1 (28 late A effects, stale refresh и inactive entry).
Отдельный pending-start refresh на epoch-guarded версии:44/45 PASS, единственный
ожидаемый FAIL; это reachable через повторный show. Source-contract show-refresh
также RED→GREEN, его не считаем behavioral/AppKit proof.

Final standalone45/45PASS; headless affected outcomes/contracts20/20PASS;
малый current module и XCTest branch typecheck exit0, flake8/diff-check PASS.
Logs: extraction-fidelity-red.log, pending-start-red.log, final-green.log,
outcomes-final-green.log и compiler logs в ledger. Первые snapshot/44-GREEN
сохранены как промежуточная история и не заменяют финальный receipt.
Первый guarded parity exit1: pytest timeout30 в setup conftest:833 импортировал
backend→torchmetrics до тела harness. Это validation failure, не lifecycle RED
или Ubuntu result. Один повтор после amended source: process-local/per-test
pytest300, compiler150/run30 остаются; venv/globalconfig/originalwrapper не менялись.
Повтор guarded Python3.12 parity exit0/PASS/no new worker leaks, venv reuse и
MLX absence подтверждены; py312-final-green.log. Source commit
`d237a376d2e26dfcb35a5fed94b6dabe622d83ee` содержит8owned source/test/CI paths.
Finishing sourcefix `c43a5a4d0ab1b5f83c3422fdfd4708817bbd0d10` меняет3изних;
finalsource manifest8/8 независимо сверён с git blobs и рабочими файлами.
Astra task review: Needs fixes — P2 finishing-phase refresh может скрыть свежий
backup_cleanup_failed/failed outcome; fix round1 у исходного implementer,
дополнительный suite48checks: properRED2/48 → GREEN48/48; actual held
finishing refresh скрывал backupCleanupFailed, repeated show создавал новые IPC.
Fix добавляет finishing producer/completion guard и terminal revision invalidate;
always-held prior-refresh positive control отдельно сохраняет warning.
Scoped Astra re-review: finding ADDRESSED, spec+quality Approved; новых
defects в3path fix diff нет. Whole-card approval и exact-head
CI — следующие gates. После публикации точный head/PR/CI receipt
смотреть в PR body/checks этой ветки и ignored ledger. Snapshot документа
не считается final approval. Обновлённые48/20/typecheck/guardedPy312 gates
после finishing fix снова PASS; review-finishing-* logs сохранены.

## Границы

Owner разрешил source-продолжение и межсессионный обмен данными; Main/VG получили
краткий kickoff. Нет merge/deploy/restart, реальной истории, Keychain, activation,
migration, purge, live IPC/SSE/AppKit E2E или соседних правок. Закрытие окна
останавливает observation, не backend migration. UI layout/тексты сохраняются.
Source→binary/live wiring этим headless тестом не аттестуется.

Ресурсный снимок10:14CEST: pressure2, swap~23.3GiB, load317. Локально только
малые serial harness/selected tests; full package build и full backend suites
через hosted CI. Sol6.1High implementation; Astra — финальный lifecycle gate.

## Final-review integration fix

Astra Ultra whole-card review: one REAL P2, Escape выполняет orderOut без
windowWillClose, оставляя observation active и прежний epoch при reopen.
Root разрешил дополнительный keyboard adapter path, прежний UI hide сохранён,
Fix `260a37f361c040765b156d438373fe96e693bd93`: cleanup вызывается перед
прежним orderOut. Actual headless adapter regression RED1/21→GREEN21/21;
unchanged production callback48/48 PASS. Finalsource manifest9/9 независимо
сверён с gitblobs/workingfiles. Новая scoped verification/CI требуются;
draftPR2086 oldhead a74d7195 ещё не final approved.
GlossarySuggestions orderOut принадлежит отдельному sheet controller, его
не меняем. Full live key-event/AppKit acceptance не входит в source proof.

Escape boundary — source contract отдельной реальной handler ветки; он не
выдаётся за живое нажатие клавиши. Py312/compiler/typecheck source inputs
coordinator/harness неизменны с c43a, прежний48 parity receipt сохраняется;
Escape actor/package integration проверяется новым hosted CI. Ledger сохраняет
initial8path и final9path seals, task/final-review finding history и оба RED.
