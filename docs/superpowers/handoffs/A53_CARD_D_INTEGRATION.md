# A53 Card D — Integration (isolated IPC E2E + parity/audit/build + exact-SHA CI)

База: свежий `origin/codex/krab-ear-v2`. Предусловия: A+B+C приняты и
по-отдельности GREEN. Контракт SHA256
`9bb260fe303b081a4883f9e4da8d7e3bffeebc8774898f69ed92acf30c5def5a`
(FINAL GO); спека §7–§8. Только synthetic профили + temp outputs + отдельный
временный backend; production/history/Keychain/удаление копий/ротация/рестарт
прод-процесса запрещены.

## Scope (входит)

- Synthetic profile + injectable/fake IPC transport + temp outputs: полный
  dispatcher→authorizer→manager путь с текстовым маркером, который появляется
  ТОЛЬКО в разрешённом fixture output.
- Isolated реальный backend IPC + Swift writer harness без запуска production
  app: real socket 0600, real epoch/generation/seq/high-water, revoke-before/
  after границы.
- Проверки API keys и actual writes (не mock-only): запуск/запись/deny/partial
  считаются по файлам.
- Финальные ворота: targeted тесты обоих языков, Linux/Python3.12 parity
  изменённых tests, `audit-all`, Swift build при ресурсах, затем exact-SHA CI
  (устный PASS на новый SHA не переносить).

## Не входит

- Production E2E / encrypted-history E2E / live-диктовка / MLX / TTS; включение
  шифрования; удаление/перенос plaintext-копий; inventario-расширение на весь
  home; второй production агент.

## Файлы

- Тесты/харнесс: `KrabEar/tests/test_plaintext_export_integration.py` (isolated
  IPC fixture, synthetic profile, temp dirs, barriers — не sleep), Swift harness
  `.../PlaintextExportIntegrationHarness.swift` (fake→real transport swap без
  production app).
- Доки: отчёт приёмки в handoff (counts/metadata only, без transcript text,
  settings values, PII).

## Шаги

1. Fake-transport integration: synthetic profile, grant→preflight→receipt→write;
   revoke до validation → 0 writes; validation→revoke→deliver → ровно 1
   запланированный write; повтор receipt/seq → 0 дополнительных.
2. Isolated real IPC: отдельный временный backend (свежий epoch) + Swift harness;
   epoch replay отклонён; crash (новый app_session) старый token не принимает.
3. Batch/Obsidian через real dispatcher: N=N validations, deny до mkdir, partial
   result точен.
4. Redaction re-gate (15a/15b через real paths) + settings/profile/backup
   serialization (только revision, без grant/receipt/session ID).
5. Parity/audit/build: `pre_merge_py312` всех изменённых tests, `make audit-all`,
   `swift build -c release` в свободное окно; затем exact-SHA CI (CI + krab-ear-ci).

## Behavioral RED→GREEN (полные тексты из контракта, зона D)

- п.8: детерминированные barriers: revoke до validation → 0 writes; validation
  затем revoke затем deliver reply → ровно 1 запланированный write. Повтор
  receipt/callback/seq → 0 дополнительных writes.
- п.14: исполняемые Swift tests используют fake transport и counting writer, а не
  только source contains. Python service fixtures обязательно `close()`; не
  запускать all-tests/ML/GPU ради gate.
- п.15 (isolated IPC E2E): в isolated IPC E2E проверить real
  dispatcher→authorizer→manager; synthetic текст-маркер появляется ТОЛЬКО в
  разрешённом fixture output. Это не production/encrypted-history E2E.
- п.15a/15b re-gate: sentinel-секреты через ACTUAL handle_request и socket error
  paths (malformed params/JSON, unknown method с auth, unauthorized signing,
  mocked RuntimeError, exception с token) — ни один sentinel в logs/traceback/
  fake-Sentry/diagnostics; payload не логируется; normal serialization без
  grant/receipt/session ID; Swift logs без sentinel.

Каждый `BackendService(...)` ОБЯЗАН `service.close()` в `tearDown`. Дочерние
fixture-процессы terminate/join в finally (только свои PID). Секреты из env/
файлов, не печатать и не коммитить; в отчёте — metadata/counts only.

## Команды (исполнителю карточки, не выполнять здесь)

```bash
PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python -m pytest KrabEar/tests/test_plaintext_export_integration.py KrabEar/tests/test_plaintext_export_authorization.py KrabEar/tests/test_plaintext_export_sinks.py -v
scripts/pre_merge_py312_check.sh KrabEar/tests/test_plaintext_export_integration.py
scripts/pre_merge_py312_check.sh KrabEar/tests/test_plaintext_export_authorization.py
scripts/pre_merge_py312_check.sh KrabEar/tests/test_plaintext_export_sinks.py
make audit-all
cd native/KrabEarAgent && swift build -c release
# затем exact-SHA CI по регламенту (CI + krab-ear-ci), без переноса PASS на новый SHA
```

## DoD

- пп.8/14/15/15a/15b GREEN в isolated E2E; маркер только в разрешённом output;
  barriers детерминированы (не sleep).
- `pre_merge_py312` GREEN для всех изменённых tests; `audit-all` без новых
  нарушений; Swift build GREEN (или честно зафиксирована причина пропуска по
  ресурсам с повтором в свободное окно).
- Whole-diff карта sinks сверена (новые/переименованные writers проверены, не
  allowlist); no swallowed denial; grant bypass через lower-level writer
  отсутствует; токены вне логов.

## Gate

Без whole-diff независимого Astra High review и применимого isolated E2E —
source BLOCK, даже при unit GREEN. Затем exact-SHA CI; это не deploy/restart/
encryption-activation GO.
