# Волна fail-closed + честный UI шифрования (2026-10-11)

Ветка `claude/haiku-agents-work-79f6d6` (база `16d2b226`, 15 коммитов),
[PR #2084](https://github.com/Pavua/Krab-Ear/pull/2084) OPEN.
Проверено 11.10 в Codex: exact head `5e30937619d91aaf6bf4afa0a01385caf877f352`,
27 CI checks SUCCESS. Независимый Astra whole-diff gate PASS; отдельная synthetic
Timeline Swift/crypto IPC карточка прошла83tests+27subtests и final delta Astra
PASS, audit-all14gates PASS. Followup
[#2085](https://github.com/Pavua/Krab-Ear/pull/2085) DRAFT; его новый exact-head CI
проверяется отдельно. Merge/deploy/live acceptance не заявлены.
Исполнители: Sonnet 5.5 (воркеры в worktree), Haiku 5.5 (скауты, read-only), Opus 5.5 (ревью всей ветки). Каждую находку гейтил сам владелец сессии против кода.

## Что сделано
| Область | Суть | Коммиты |
|---|---|---|
| Call observer (Swift) | провал IPC `get_settings` = privacy ON (`IPCCallObserverSettings.resolveSettings`) | 069de95c |
| `load_settings` | битый/не-объект `settings.json`, не-bool `privacy_mode_enabled` = privacy ON | e5c8a188, f8c97e00 |
| Чтение журналов | `_read_history_ndjson_unlocked` поднимает OSError открытия (раньше «пусто» → компактация стирала историю) | ff76801f |
| Проба ENC1 | `has_encrypted_history_in`: нечитаемый журнал = «возможно ENC1»; guard ON→OFF в `save_settings` требует ДОКАЗАННОГО ENC1 (`unreadable_is_encrypted=False`) | ff76801f, f8c97e00 |
| DataMigrator | migrate/rollback отказывают при ENC1-остатке даже при флаге OFF | ff76801f |
| Деградация вместо падения | `maybe_compact` не валит старт; `purge_all_data` не блокируется нечитаемым побочным журналом | f8c97e00 |
| RMW | `sharing_manager` (нечитаемый индекс блокирует запись, отзыв fail-closed), `export_scheduler` (`.corrupt-<ns>`, ретеншн 5, purge их чистит). `auto_backup`/`obsidian_sync` — ложные тревоги | a9e5a042 |
| Swift reason-строки | `privacy_mode_active`, `plaintext_confirmation_required` сверены с Python | cc2f7f3f |
| Миграция шифрования | `last_migration_status` в `get_history_encryption_status` (аддитивно), UI читает его первым, любой не-`done` = ошибка; SSE — запасной путь; отмена SSE при закрытии окна; тоггл не утверждает неизвестное состояние | f24c166a, 778c51e4, 383d1566, f2f06594 |
| Таймлайн-экспорт | идёт через `PlaintextExportCoordinator` (consent sheet), таймаут 60 с вместо 5 с | 9069135a, 0d5a56ea |
| Docs | счётчик хендлеров 364 (AST-счёт); бриф для Краба | 48f22b4d, f360c0de |

## Проверено
- Python: ubuntu-parity (py3.12 без mlx) на изменённых файлах — PASS; точечные файлы по областям — зелёные; широкий набор (234 файла) — см. NOW.md.
- Swift: полный пакет 1731 тест, 0 падений (до коммитов таймлайна); таймлайн-классы 28 тестов, 0 падений.
- Аудиты: purge-coverage, fake-store-signatures — чисто. `audit_orphan_imports --strict` красный на параметрах функций — ложная тревога сканера, CI гоняет без `--strict`.

## Открыто (следующим агентам)
1. **Новый exact-head CI и review followup #2085** по
   [карточке](../plans/2026-10-11-a54-timeline-crypto-ipc.md). Push/PR/exact-head CI
   и whole-diff review исходной ветки уже выполнены; повторно создавать эту
   волну не требуется. Synthetic проверки и новый source fix описаны в
   [Timeline handoff](2026-10-11-a54-timeline-crypto-ipc.md).
2. **Живой click-through** consent-sheet для 3 кнопок таймлайна при включённом шифровании (не проверялся в приложении).
3. **Бриф Краба** `docs/superpowers/handoffs/2026-10-11-krab-consumer-drift-brief.md` не передан (сессия Краба недоступна): `get_history` (нет у Ear, есть `get_history_page`), `:18000/health`, `/screenshot`, flock в perceptor.
4. **Дизайн-решение владельца**: нечитаемый побочный журнал теперь даёт `internal_error` на чтениях истории (раньше частичный вид) — оставить или мягкая деградация.
5. **Activation шифрования остаётся HOLD**: тумблер и чекбокс `history_encryption_enabled` в «Все настройки» включают шифрование без подтверждения и без проверки Keychain; `set_settings` обходит `set_history_encryption`; один Keychain-элемент на все профили; повторное создание ключа при отсутствии элемента без проверки ENC1-истории (тихий re-key). Нужен отдельный scope владельца.
6. Мелкое: `_save_meta` в `auto_backup` не атомарен; Keychain-вызов под store-lock до 10 с; `get_diagnostics` опрашивает Keychain каждые 10 с (по коду); тест `test_a52a…` теперь проверяет `privacy_mode_active` + отдельный вариант policy-ветки.

## Контекст окружения
Сегодняшние «hang detected» и задержки `load_settings` до 25 с = перегрузка машины (swap ~97%, load 30–70), не баг Ear. MemoryConductor в теневом режиме (200+ «would evict», выгрузки нет) — включение за владельцем.
