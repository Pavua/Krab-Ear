# A53 Card B — Python sinks (все файловые plaintext-производные)

База: свежий `origin/codex/krab-ear-v2`. Предусловие: карточка A принята (API
authorizer стабилен). Контракт SHA256
`9bb260fe303b081a4883f9e4da8d7e3bffeebc8774898f69ed92acf30c5def5a`
(FINAL GO); спека §7.5–7.7. Нельзя начинать до принятого A API; один владелец
`service.py` (карточки A/B/C не параллельно с перекрывающимися файлами).

## Scope (входит)

- `KrabEar/backend/history_service.py`: export_history:1449 (mkdir/write
  1449–1452), selected:1830, `_finalize_srt_export`:1998, JSON:2166, CSV:2293 —
  auth до mkdir/temp/write, сохранить render-only варианты; Obsidian:4658 +
  HTML:5989 и alias `generate_html_report` (оба alias при `save_to_file` к одному
  gate; render-only без file consent); batch_export:5623 + `_export_csv_to_dir`:
  5748 (проверка до bundle mkdir, отдельная validation каждого файла; context
  прокинуть делегатам).
- `KrabEar/backend/service.py` timeline handlers export_timeline_svg/json/ical:
  5566/5620/5669 — gate ДО `_resolve_timeline_export_dir`:5433 (он делает mkdir).
- `KrabEar/backend/obsidian_sync.py` sync:319/361/398 + `handle_sync`:547 —
  инъекция общего authorizer; отсутствие authorizer = deny; `force` не меняет
  policy.
- `KrabEar/backend/export_scheduler.py` check_and_export:413 → `_do_export`:164 —
  таймер не получает чужой grant; ON запрещён до mkdir/pruning, включая direct
  `_do_export`.
- `KrabEar/backend/sharing_manager.py` `_fetch_items`:609 → `prepare_share`:215 →
  `_persist_package`:736/751 — gate direct prepare/persist; grant не разрешает
  внешнюю публикацию (только файловый sink по происхождению из history).
- Явный authorizer context до writer на каждом sink; fresh authorization
  непосредственно перед первой связанной mutation каждого файла.

## Не входит / сохранить как есть

- `handle_export_history_markdown`:1560–1724 оставить render/clipboard без file
  consent (не файловый sink, TranscriptWriter не вызывает); privacy gate прежний.
- Auto-callers `recording_core_service.py:3615/3627` и `:3875/3883` сохраняют
  ON-block через `_should_write_plaintext_md:4428`; grant его не отменяет; auto
  recorder/import `.md`/archives/versions/backup остаются заблокированы при
  valid session grant.
- Не переиспользовать негейтированный `TranscriptWriter.write_transcript:214–244`
  для manual route без context: новый manual route обязан передать authorizer
  context и проверить его до первой mutation; не утверждать, что leaf уже
  безопасен.
- Границы вне narrow scope не расширять молча: CallAssist VG payload,
  Import operational report, glossary/settings/logger/plist writers.
- Не менять authorizer API из карточки A; не трогать Swift (C) и интеграцию (D).

## Файлы

- Править: `KrabEar/backend/history_service.py`,
  `KrabEar/backend/obsidian_sync.py`, `KrabEar/backend/export_scheduler.py`,
  `KrabEar/backend/sharing_manager.py`, `KrabEar/backend/service.py`
  (только timeline handlers + прокидка context), по необходимости
  `KrabEar/backend/recording_core_service.py` (только сохранение caller gates,
  без новых manual routes).
- Тесты: расширить/добавить `KrabEar/tests/test_plaintext_export_*sinks*.py`
  (parametrized по route+alias).

## Шаги

1. В каждый файловый sink вставить auth до первой mutation (mkdir/temp/open(write)/
   copy/prune): snapshot под StateStore._lock того же профиля → authorizer lock;
   deny до любого mkdir (batch/Obsidian до bundle/vault mkdir).
2. Batch/Obsidian: N файлов = N validations; deny-до-mkdir, затем повторные
   проверки по файлам; отзыв посередине → явный partial result без rollback уже
   разрешённых.
3. Direct manager paths (`sync`/`prepare_share`/`_persist_package`/`_do_export`)
   гейтить напрямую; injected authorizer отсутствует/кидает → deny, не OFF
   fallback.
4. Timeline: gate до `_resolve_timeline_export_dir:5433`.
5. Sharing: файловый sink гейтить, внешнюю share-link policy не расширять.
6. Scheduler: чужой grant не заимствовать; direct `_do_export` тоже гейтить.
7. Проверить отсутствие grant bypass через lower-level writer (whole-diff
   самопроверка перед review).

## Behavioral RED→GREEN (полные тексты из контракта, зона B)

- п.1: параметризовать КАЖДЫЙ файловый history export route+alias с его
  file-writing params, formats/selected/batch, timeline, local share, direct
  sync, direct scheduler: ON без context → причина отказа, нет
  mkdir/temp/open(write)/copy/prune и новых файлов в tmp fixture.
- п.2: `confirm=True`, `force=True`, `save_to_file=True`, malformed/missing/
  wrong-session/wrong-epoch token и неизвестный sink_kind не обходят gate;
  privacy ON блокирует даже корректный grant.
- п.3: direct manager tests вызывают sync/prepare_share/_persist_package/
  _do_export с synthetic данными, минуя dispatcher; injected authorizer
  отсутствует/кидает → deny, не OFF fallback.
- п.9: batch/Obsidian: остановить между файлами, revoke, продолжить → первый
  допустим, остальные не созданы; partial result точен. Отдельно deny до
  bundle/vault mkdir при начальном отсутствии grant.
- п.12: таймер scheduler + любой активный чужой grant → ON deny до mkdir/prune;
  auto recorder/import `.md`/archives/versions/backup остаются заблокированы при
  valid session grant.
- п.13 (sinks-сторона): KNOWN_OFF+privacy false сохраняет export; privacy
  true/UNKNOWN deny; `export_history_markdown` render/clipboard при ON не требует
  grant и не выдаёт его; privacy gate прежний; schema parity read/render-only
  ответов и errors без секретов.

Каждый `BackendService(...)` в тесте ОБЯЗАН `service.close()` в `tearDown`.
Только synthetic временные профили и tmp outputs; живые данные/прод не трогать.

## Команды (исполнителю карточки, не выполнять здесь)

```bash
PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python -m pytest KrabEar/tests/test_plaintext_export_sinks.py -v
scripts/pre_merge_py312_check.sh KrabEar/tests/test_plaintext_export_sinks.py
make audit-all
```

## DoD

- Все тесты зоны B GREEN; при ON без context — 0 файлов/каталогов (проверять
  отсутствие mkdir/temp/write/copy/prune, не только return-код).
- Batch/Obsidian partial result точен; scheduler/auto writers заблокированы при
  ON независимо от session grant; `confirm`/`force`/`save_to_file` не обходят.
- Schema parity render-only ответов сохранена; секретов в errors нет.
- Diff ограничен файлами раздела «Файлы»; authorizer API не менялся.

## Gate

Без whole-diff независимого Astra High review — BLOCK (плюс проверка новой карты
sinks: новые/переименованные writers обязан найти review, таблица не вечный
allowlist).
