# Приёмка работ Krab Ear — 2026-09-29

## Проверенная база и границы

Приёмка начата с `31568b1fc1038496fe646ca3cf5e84c3bcf24951`
(`origin/codex/krab-ear-v2`). Общий checkout оставлен на
`chore/agent-parity-0928` с чужими untracked-файлами без изменений.
Использован отдельный worktree `ear-acceptance-0929`, ветка
`codex/ear-purge-key-boundary-0929`.

Три независимые дорожки: handoff/Git/CI и lock-контракт; adversarial-проверка
A5.2/purge; runtime/бинарный parity и безопасные live probes. Координатор
повторно проверяет итоговый diff, дефекты и локальные gates.

## Git и CI до исправления

Открытых PR при начале приёмки нет. #2060, #2062, #2063, #2064, #2065 смержены.
#2062 меняет только два бинарных артефакта при сравнении с первым merge-parent.

- Release `84deb513`: [CI](https://github.com/Pavua/Krab-Ear/actions/runs/36389588297),
  [backend CI](https://github.com/Pavua/Krab-Ear/actions/runs/36389588450) — success.
- Base `31568b1f`: [CI](https://github.com/Pavua/Krab-Ear/actions/runs/36397518482),
  [backend CI](https://github.com/Pavua/Krab-Ear/actions/runs/36397518418) — success.
- Private MLX 27/28.09: отказ admission (`activity_unknown` /
  `other_runner_worker`), exit 75, до определения SHA и запуска тестов.
  Последний успешный private MLX проверял `a0d10163`, не A5.2.

`gh run list --branch` оказался неполным источником; выше проверены runs
через GitHub API по точному `head_sha`.

## Runtime: снимок 29.09 ~02:23 CEST

Backend 36853, REST 36856 и GigaAM 37081 используют
`84deb513ad657bab5ddcdb69094ab81b3afc8d96`. Release checkout чистый;
backend/REST launchd runs=1, never exited. Ping ~1 ms, diagnostics ~40 ms,
REST health HTTP 200. Recording=false, meeting.active=false, wake running,
not wedged; три audio device. `restore_pending=false`, encryption OFF,
key_present=false. Brain preload/enforcement, cloud/LLM rewrite и semantic
search OFF.

Один Swift-агент 88227. App/runtime/build имеют общий Mach-O UUID
`18B7995A-0BA3-377A-B2D7-F10DAA39DD36` и одинаковую полезную нагрузку
(8 553 600 bytes, SHA256 prefix `a07b6b9e`); подписи установленных копий valid.
Различие полного SHA256 объясняется подписью, не различием исполняемого кода.

Sentry: 92 accepted / 0 rate_limited за 24 ч на уровне организации.
[KRAB-EAR-BACKEND-1V](https://po-zm.sentry.io/issues/138897914/): четыре
таймаута `get_meeting_report` по 180 с, последнее событие 28.09 07:52:25Z.
Release tag `krab-ear@2.4.0` не устанавливает точный Git SHA; причинность
к A5.2 не доказана. Agent unresolved за 24 ч — 0, последний event 25.09:
свежий ingress именно агента не подтверждён.

## Находки при приёмке

1. **P1: purge/key boundary.** При успешном compact late append мог оставить
   ENC1 в `history.ndjson`, затем purge удалял ключ и сообщал `complete=true`.
   Late delete+compact мог аналогично заново зашифровать deletion ledger.
   Сбой unlink также не препятствовал уничтожению ключа. Воспроизведено на
   реальных StateStore/HistoryService в tmp с fake Keychain.
2. **P1: restore marker durability.** Перед первой live replace синхронизирован
   staging, но не его родитель `data_dir`; появление recovery-каталога не было
   закреплено на диске. Fault injection показала замену живых журналов до
   первого отказавшего parent fsync.
3. **Purge scope остаётся неполным:** synthetic `.secrets.bak` и
   `auto_glossary.json.bak.*` переживают purge с `complete=true`. Настоящие
   файлы этих семейств существуют; проверены только имена, типы, размеры и
   режимы доступа. Никакие значения секретов/истории не читались этой пробой.
4. **FIFO:** `StateStore.__init__` проходит, первое чтение ledger зависает.
   Временный дочерний процесс остановлен собственным timeout. Старый claim
   о зависании именно `Path.touch()` опровергнут; нужен отдельный bounded guard.

Lock-контракт принят в проверенных путях: SH→EX громко отклоняется,
same-object reentry сохраняет FD, helper/store cross-nesting даёт bounded
timeout, реальная contention не пропускает save_settings. Версии транскриптов
и policy reader соблюдают порядок lock. Claim о записи save_settings вне
лока опровергнут; это не доказательство всех возможных внешних load/modify/save
последовательностей.

## Исправления и независимый source gate

Purge теперь выполняет под единым exclusive flock: сбор union текущего
ledger, tombstones и всех ID истории; atomic plaintext ledger с fsync файла
и родителя; удаление девяти прочих журналов; fsync и проверку исчезновения;
сброс кэшей; попытку shred. При отказе до shred ключ сохраняется. Это
частичная операция: уже удалённые побочные данные не откатываются.

Darwin timeout Keychain больше не считается доказанным shred. Независимое
ревью выявило ещё один случай: delete успевает завершиться, подтверждение
зависает. Криптокэш теперь сбрасывается в `finally` любой попытки удаления
ключа после успешной очистки, чтобы следующий writer не использовал старый
ключ, уже отсутствующий в Keychain. При отказе до попытки shred кэш сохраняется.

Restore/recovery синхронизирует `data_dir` после записи COMMITTING и до первой
замены живого журнала. Отказ этой синхронизации не меняет живые файлы.

Новые регрессии: восемь purge-кейсов и четыре restore/recovery-кейса с
подтверждённым RED→GREEN. Независимый итоговый source review: **PASS**;
повторная проба delete→confirmation timeout→новая запись→свежий StateStore
прошла. Malformed late record и final parent fsync failure после unlink
сохраняют ключ и сообщают partial failure.

SHA256 принятого production diff относительно `31568b1f`:
`8d49e2de67d63b34e05fa5666370073ffab989d14fcf68ad1180f52c05dd136e`.
Область: `history_service.py` и `encrypted_snapshot.py`.

## Итоговые локальные gates

Все запуски последовательные, отдельный pytest-процесс на файл с cleanup
только собственной process group. Python 3.12.7, MLX отсутствует, сетевые и
Keychain guards из conftest включены. Для Linux-контракта использована
`KRAB_A52C1_SIMULATE_NON_DARWIN=1`; это симуляция на macOS, не реальная Ubuntu VM.

| Проверка | Результат |
|---|---:|
| A5.2c1 macOS + non-Darwin | 100 + 100 passed |
| A5.2a / b1 / b2 / b3 | 41 / 69 / 157 / 40 passed |
| 11 связанных purge-наборов | 219 passed |
| Purge coverage / IPC docs parity | 32 / 5 passed |
| StateStore encryption / journal encryption | 13 / 28 passed |
| Итого: 21 запуск, 20 файлов | **804 passed** |
| `make audit-all` на итоговом source | PASS |
| Ruff + CI-equivalent flake8 по изменённым Python-файлам | PASS |
| `git diff --check` | PASS |

Два старых теста требовали существующий пустой файл после purge; заменены
проверками отсутствия/пустоты плюс реального чтения истории. Restore-staging
тест усилен настоящей записью-canary и повторным recovery без resurrection.
IPC docs parity поймал устаревшее описание потери tombstones; контракт
обновлён, гейт не ослаблен. Промежуточные failures устранены; таблица отражает
последние прогоны каждого файла, без повторного подсчёта неуспешных запусков.

**Вердикт:** два исправленных source-блокера и локальные gates приняты.
CI новой ветки должен подтвердить точный commit после публикации PR;
source-review и тесты не являются разрешением на deploy/активацию.

## Ограничения приёмки

Full live STT/MLX/E2E не выполнялся: swap достиг ~27.7 ГБ, параллельно работал
чужой test gate. Выполнен read-only IPC/REST/runtime smoke; он не доказывает
качество распознавания. Обычные E2E могут менять профиль, переключать privacy,
восстанавливать агент или регистрировать внешний webhook; они не запускались
против production.

Реальная история, резервные копии, Keychain и флаги не менялись. Новый код не
задеплоен. Активация шифрования остаётся HOLD до закрытия defects и отдельного
решения владельца по старым открытым копиям/токенам. Проверка внешних копий
(Time Machine/iCloud) и отзыв токенов не выполнялись.

Рекомендация: Astra High для финального security-gate; Sol Medium для
последующих механических CI/docs-проверок. Доступность моделей и High/Medium
подтверждена локальным каталогом Codex от 29.09.
