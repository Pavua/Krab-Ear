# A5 — точка продолжения 24.09.2026

## Что принято владельцем

- Scope сейчас: история и её внутренние журналы; общий аудит остальных PII позже.
- Plaintext-файлы/Obsidian при encryption ON — только после подтверждения.
- Подтверждение запоминается до конца сессии и включает Quick Capture → Obsidian.
- Старые plaintext-артефакты: инвентарь, затем отдельное решение владельца.
- Активация `history_encryption_enabled`, реальные ключи/история и live migration
  не входят в текущую source-разработку.

## Артефакты

- Спека: `docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md`.
- Исполнимая первая карточка: `docs/superpowers/plans/2026-09-24-a5-journal-codec.md`.
- Worktree: `/Users/pablito/.codex/worktrees/ear-a5-history-completion/Krab Ear`.
- Ветка: `codex/ear-a5-history-completion`.
- База: `5ebfb8375392e45bd4d12da908cd8e7b47a642b0`, exact remote повторно сверена.
- Source-коммиты A5.1: `ef8783a732459bfddcd527f8458354b6896e7d4c` (codec всех
  десяти журналов), `c3f947d40b5282311c9520639584e9cdc65c7b96` (compaction и
  durable purged ledger). Docs baseline: `2252beefcc81c3079dd28923723e5b45f87f60c8`.
- Только synthetic tests во временных профилях; build, IPC, Keychain и runtime
  probes не запускались. Локальный gate завершён, следующий шаг — PR/CI;
  deploy/activation для A5.1 не выполнены.

## Независимый review и self-review

Reviewer прочитал код и спеку. Итог спеки APPROVE после исправления:

1. Сохранённый по решению владельца plaintext — перечисленное исключение,
   а не безусловное «вся история зашифрована».
2. Restore сохраняет объединение текущих tombstones/permanent purged IDs;
   старый snapshot не может воскресить удалённое.
3. Swift preflight разрешает одну операцию; межпроцессный race и same-UID IPC
   threat boundary названы явно, без обещания мгновенной crash-revocation.
4. Совместимый rollback должен понимать codec A5.1 и recovery A5.2.

По карточке reviewer нашёл fsync-retry hole: ID может быть уже записан,
но не durable; повтор компактации пропускает append и ошибочно очищает
tombstones. В карточку добавлены обязательный ledger fsync даже при пустом
новом append-наборе и fault-injection тест «write → fsync failure → retry».
Эта правка теперь реализована и проверена RED/GREEN, включая retry fsync.
Все четыре Python code-block синтаксически проверены `ast.parse`.

Свежий независимый whole-diff reviewer (Astra High) прочитал все шесть файлов
на `5ebfb837..c3f947d4` и production-consumers helpers: APPROVE source-only
A5.1, Critical/Important/Minor отсутствуют. Review статический, не runtime proof.

## Проверки A5.1

- Task 1: RED 20 failed / 1 passed → GREEN 21 passed; зависимые файлы отдельно:
  previous A5 13, StateStore 68, fsync 5 passed.
- Task 2: RED 6 failed / 22 passed → GREEN 28 passed; зависимые файлы отдельно:
  integrity 12, fsync 5, TOCTOU 8, recording_merger 9, previous A5 13 passed.
- `make audit-all`, `git diff --check` — PASS. Flake8 всех трёх изменённых
  Python-файлов с точными параметрами `.github/workflows/krabear-ci.yml` PASS.
  Исходные F401 (`json`, `call`) legacy fsync-test разрешены test-only CI ignores;
  при более строгом запуске без этих ignores предупреждения сохраняются.
- Python 3.14 dev interpreter, process-group wrapper, timeout 30s/test;
  pytest-timeout работает. Есть исходное optional torchcodec/FFmpeg warning.
- Python 3.12.11 без MLX: 143 PASS, семь файлов отдельными процессами
  (28+13+68+5+12+8+9), timeout 30s/test. Собственный минимальный venv
  `/private/tmp/krab-ear-a5-parity.ND2yDn/venv`; `mlx`/`mlx_whisper` отсутствуют
  по find_spec, conftest и hardware/network guards не обходились. Первичная
  collection потребовала pydantic-settings: установлен только в этом venv.
  Это targeted missing-MLX gate, НЕ полный Ubuntu/backend-import gate;
  полный набор проверяет hosted CI. Общие py312/A1/A2 venv не менялись.
- Логи: `.superpowers/sdd/2026-09-24-a5-journal-codec/` (ignored worktree data).

### Первый hosted CI и точечная коррекция

- Push-run `krab-ear-ci` 36027294864 на `b3022cd9` упал на
  `test_krab_ear_runner_health_check.py`: при импорте скрипта отсутствовал
  `httpx`; в этом же job были `Unknown config option: timeout` и
  `timeout_method` из-за отсутствия `pytest-timeout`. Оба пакета отсутствовали
  в `KrabEar/requirements.txt` базы. Другие A5.1 tests в логе не падали.
- В PR добавлены обе прямые зависимости. Runner test в собственном Python 3.12
  без `httpx` воспроизвёл точный RED (1 failed), после установки declarative
  dependency — GREEN (1 passed). Pytest-timeout уже стоял в собственном venv;
  штатный CI должен подтвердить его наличие в полной установке requirements.
- Новая CI-проверка после push нужна для исправленного SHA. Ни один runner
  вручную не перезапускался, failed run не rerun-ился.
- Следующий PR run `krab-ear-ci` 36030838569 на `da46b2d1` подтвердил отсутствие
  старого `httpx`-сбоя, но `pytest-timeout` включил прежний default 30s для
  всех тестов. Под hosted-нагрузкой 30s превысили real-repo cherry-pick audit,
  dead-extracted audit (в `setUpClass`) и `test_backend_service.py::
  BackendServiceTestCase::test_integration_1000_cycles`. Ни один A5.1 тест
  не обозначен failing. Merge HOLD.
- В `.github/workflows/krabear-ci.yml` выставлен явный CI cap 75s на test
  для chunk и per-file isolate при неизменном внешнем per-file 90s. Локально
  под `--timeout=75`: scanner real-repo 1 PASS за 16.98s, dead-extracted
  RealRepoSmokeTests 6 PASS за 15.69s (Python3.12 без MLX); 1000 циклов
  1 PASS за 25.70s (dev Python3.14, fake recorder; optional torchcodec warning).
  Это не доказывает Ubuntu timing; требуется новый exact-head hosted CI.

## Ресурсное окно

Snapshot 24.09.2026 ~15:51 UTC: активен CI главного Краба,
`/Users/pablito/actions-runner-krab/_work/Krab-openclaw/Krab-openclaw`.
Runner.Worker PID 86805 → pytest 87068 → дочерний pytest 97747;
swap used 31254.50 MiB. Последующий `memory_pressure -Q` показал 56% free:
один swap snapshot не доказывает текущую нехватку памяти. Разрешены только
короткие последовательные synthetic test-files без GPU/full suite/build.
Чужие процессы не трогались. Это исторический snapshot: перед тяжёлой работой
перепроверить процессы/ресурсы, не считать PID постоянными.

## Следующий шаг

1. Повторно сверить exact remote/base, собственный worktree status и ресурсы.
2. Создать source-only PR и дождаться exact-head hosted CI; локальный
   targeted missing-MLX Python 3.12 gate уже пройден.
3. Не читать/копировать живую историю или Keychain для «проверки».
4. Exact CI перед merge. Whole-diff
   review и audit-all уже пройдены. Не пересоздавать `/tmp/py312`
   автоматически: штатный parity script умеет удалять/rebuild shared venv.
5. После A5.1 остаются A5.2 transaction/backup/derived copies и A5.3 Swift/session
   exports. Полный A5, deployment и activation этим source-блоком не закрыты.

Исполнение экономное: основной агент последовательно; один ограниченный
независимый reviewer на финальный diff. Sol Medium подходит для A5.1 по готовой
карточке; Astra High — для сложной миграции A5.2 и финального security gate.
