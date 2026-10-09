# A5.4 — synthetic encrypted-history IPC, 09.10.2026 CEST

## Результат и состав

Local109 checks; independent Astra adversarial recheck PASS, исходный fixture
scanner P2 закрыт. Production sources не менялись. Три test файла:
`_plaintext_export_integration_backend.py`, `test_plaintext_export_integration.py`,
`test_history_encryption_integration.py` в `KrabEar/tests/`.
Source commit `a0cc704eb21090bdfef6fcb32f05b7d2821682f1`, branch
`codex/ear-a54-crypto-ipc-20261009`, base docs merge
`c106199f25d0be63d6d548cf9f3836277ebcff0e` (source candidate `9fee1ef4`).
PR exact head/CI смотреть отдельно; результаты base CI не переносить на PR.
[Implementation plan](../plans/2026-10-09-a54-synthetic-crypto-ipc.md).

## Что действительно выполняется

Optional memory-only synthetic32B key заменяет только
`crypto_keystore.get_or_create_history_key`; actual production
`build_history_crypto`, HistoryCrypto/AES-GCM, StateStore, BackendService,
authorizer/dispatcher и Unix IPC выполняются. Default D plaintext/provider-deny
сохранён; Keychain/subprocess/network/audio/ML traps действуют, service.close()
обязателен. Python isolation bounded; это не OS sandbox.

| Контракт | Наблюдаемое доказательство |
|---|---|
| Persistence/restart | ENC1 без synthetic marker в protected journals; same ID/text/status/annotation после restart |
| Wrong key/tag tamper | IPC ready; read/compact error; protected journals/settings byte-exact; audit append только проверенной metadata |
| Recovery fixture | Возврат original bytes/correct key восстанавливает read/compact |
| Export/epoch | Denied без grant, allowed после grant, old capability denied после restart |
| Scanner | Actual owned JSON file, Capture log, LocalTransport event и private AuditLogger; escaped/nested/NDJSON positives rejected, noise/cleanup clean |

Первоначальный RED: unsupported optional fixture crypto_key, а не production bug.
Отдельный scanner RED: JSON-escaped repr(key) ошибочно clean. Исправление:
structural original records/events + bounded JSON/NDJSON decode, depth32,
nodes4096, aggregate1MiB bytes/chars; overflow suspect. Synthetic probe cleanup
только owned files/capture/events, без truncation service audit. Реальные секреты
не использовались; positive probes намеренно помещают synthetic key в свои sinks.

## Проверки

- Final actual crypto/scanner10 + complete D27, включая три standalone Swift:
  свежие после scanner repair;72 ранее прошедших unchanged unit checks
  (history_crypto18, StateStore13, failclosed13, journals28). Итого109.
- Existing Python3.12 isolated_noaudio runner, sequential owned profiles;
  новые dependencies не установлены. Full1091-file local suite не запускался
  при resource pressure; exact public CI требуется отдельно.
- Pyflakes и git diff --check PASS. Ruff unavailable/no install.
  Flake8: два baseline E306 против трёх в base, новых findings нет.
- Astra source-delta recheck PASS; не release/package acceptance.

Private source freeze: `/private/tmp/a54-crypto-source-freeze-repaired.json`;
tracked delta SHA256 `85a1386bd876dad218a5b62dffe4b793b01b910990096b1002dcce8455d8638b`.
Logs `/private/tmp/a54-scanner-final-green.log`,
`/private/tmp/a54-default-d-scanner-green.log`; source hashes сохранены после
rebase на docs-only merge.

## Открытые release gates

[Release plan](../plans/2026-10-08-a54-release-acceptance.md) сохраняется.
Docs PR2082 merged `c106199f`, основной exact post-merge CI SUCCESS,
krab-ear-ci IN_PROGRESS на снимке00:45CEST. Swift byte-exact rollback PASS:
private staging `swift-rollback-20261008T222810Z`, strict/deep signature verify,
без build/sign/install/restart. Существующий consent binary не имеет пригодного
source→binary provenance; exact source CI artifacts0, reuse HOLD.

Sentry read-only auth/API PASS по отдельному owner разрешению на existing
read_sentry_token() и только SENTRY_AUTH_TOKEN из Main .env. Token не выводился
и не сохранялся. Organization108accepted/0rate_limited24h не доказывает Ear
freshness: latest backend issue07Oct17:48UTC, agent issues empty, ingress UNKNOWN.
Resource22:32–33UTC08Oct: pressure2 во всех четырёх пробах, free87–408MiB,
active swapins/out при уменьшающемся used swap. Full package build/cutover HOLD.
Private receipts `/private/tmp/ear-a54-qualification-20261009/`.

Нет Keychain recovery/SwiftUI/live encrypted-history/deployment/activation
acceptance. Сохранены old root1ebd и Backend3957/REST3924/Swift2800;
in-memory SHA UNKNOWN. Main/Gateway/shared WIP сохранены.
Scouts по разрешённым brief-only inputs: OpenCode
`opencode/muse-spark-1.3-contributor-free` cost0, agy
`gemini-3.8-flash-medium` quota, tool events0; suggestions сверены с кодом,
неподтверждённые SQLite/token предложения отвергнуты. Private runtime/Sentry
receipts внешним scouts не отправлялись.
