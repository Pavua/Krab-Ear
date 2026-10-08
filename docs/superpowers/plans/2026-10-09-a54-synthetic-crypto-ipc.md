# A5.4 synthetic crypto IPC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Доказать actual AES-GCM persistence/restart/fail-closed через настоящий IPC на synthetic профиле.
**Architecture:** Расширить существующий D child optional `crypto_key: bytes | None`; default None сохраняет plaintext seed и provider-deny. При synthetic key заменить только crypto_keystore.get_or_create_history_key, оставив production build_history_crypto/AESGCM/StateStore/dispatcher и все внешние traps.
**Tech Stack:** Python3.12, pytest, spawn, multiprocessing Pipe, Unix IPC, existing cryptography.
**Spec:** ../specs/2026-09-24-a5-history-at-rest-design.md §8–9; 2026-10-08-a54-release-acceptance.md §3.
**База:** origin/codex/krab-ear-v2 `9fee1ef4c2f5540636fd4faae1e53c93142054a5`; отдельный WT ear-a54-crypto-20261009,branch codex/ear-a54-crypto-ipc-20261009.

## Global Constraints

- Только test fixtures/новый test/docs; production source не менять без отдельного подтверждённого RED.
- Нет real Keychain/history/HOME/audio/ML/network/REST/live-agent/build/restart/activation. Synthetic child только в своём /tmp профиле, service.close() обязателен.
- Старые D guards/secret scanners сохраняются. Новый ключ в памяти parent/child, без env/files/logs в обычных сценариях; actual-sink scanner positive controls намеренно помещают только synthetic key probe в owned JSON/audit/log-capture/LocalTransport и обязаны finally удалить только свой probe, вернуть scanner clean и проверить отсутствие key в сохраняемых файлах; добавить hex/base64/repr представления synthetic key в secret registry, не печатать их.
- `git add` явными путями; no stash/reset/clean shared. Не трогать Main/Gateway/processes. Не устанавливать зависимости.
- Тесты последовательно existing py312 isolated harness; никаких parallel ML/full pytest. Full exact CI отдельно, ресурсоёмкий Swift build остаётся HOLD.

## Review Focus

- Default crypto_key=None: D seed/provider-deny/guard positive controls не ослаблены.
- Reopen encrypted profile: plaintext seed и boot read не должны маскировать failure до IPC ready.
- Wrong key/tamper: read/compact fail-closed и protected journals/settings bytes остаются неизменны; append безопасного audit-события отказа проверяется отдельно; восстановление original fixture/key возвращает запись.
- Ciphertext != просто ENC1 label: round-trip проверяет production crypto, случайный ключ и nonempty stored entries; synthetic marker не остаётся в journals.
- Consent/epoch: encrypted export без capability запрещён; после grant output разрешён; после restart old capability недействительна.

### Task 1: Расширить существующий isolated fixture, добавить настоящую crypto приёмку

**Files:** Modify KrabEar/tests/_plaintext_export_integration_backend.py; Modify KrabEar/tests/test_plaintext_export_integration.py; Create KrabEar/tests/test_history_encryption_integration.py.
**Interfaces:** IsolatedBackend(root, *, crypto_key=None); run_child(connection, root_string, source_string, crypto_key=None). Existing methods call/control/close сохраняются. Новые fixture-only control могут возвращать boolean/count crypto metadata, без key bytes.

- [x] Step1 RED: написать новый encrypted contract до расширения helper; зафиксировать failure existing unexpected crypto_key или provider-deny/plaintext boundary, не считать это production bug.

```python
import os
import tempfile
from pathlib import Path
from test_plaintext_export_integration import IsolatedBackend

def test_actual_crypto_socket_write_read_restart():
    with tempfile.TemporaryDirectory(prefix="a54c-", dir="/tmp") as directory:
        root = Path(directory)
        key = os.urandom(32)
        backend = IsolatedBackend(root, crypto_key=key)
        try:
            backend.start()
            response = backend.call("add_history_item", text="A54_SYNTHETIC_CRYPTO", paste_status="ok")
            assert response["ok"] is True
            assert backend.call("get_history_page", limit=10)["ok"] is True
            raw = (root / "profile/history.ndjson").read_bytes()
            assert raw and b"A54_SYNTHETIC_CRYPTO" not in raw
            assert all(line.startswith(b"ENC1:") for line in raw.splitlines() if line)
        finally:
            backend.close()
        reopened = IsolatedBackend(root, crypto_key=key)
        try:
            reopened.start()
            result = reopened.call("get_history_page", limit=10)
            assert result["ok"] is True
        finally:
            reopened.close()
```

Run: `EAR_A53_FIXTURE_ROOT=/private/tmp/a54-red-owned PYTHONPATH="$PWD/KrabEar:$PWD/KrabEar/tests" PYTHONDONTWRITEBYTECODE=1 /private/tmp/ear-a53-ci-repair-20261008/venv/bin/python /private/tmp/ear-a53-ci-repair-20261008/isolated_noaudio_pytest.py KrabEar/tests/test_history_encryption_integration.py -q`. Expected RED fixture crypto mode unavailable. Если failure другой, сначала исправить тестовую предпосылку.

- [x] Step2 GREEN: прокинуть optional key в child; при None оставить default code; при key сохранить _run_security trap, patch get_or_create_history_key, не patch build_history_crypto. Fresh synthetic profile сразу ON, seed только default mode; при encrypted reopen не читать history до ready. Без production setters/internal crypto injection. Runtime constructor может сам читать corrupt journal — зафиксировать доступный IPC fail-closed outcome, не заглушать production error.

```python
# Existing IsolatedBackend constructor/start:
def __init__(self, root, *, crypto_key=None):
    self.root = Path(root)
    self.crypto_key = crypto_key
    self.process = self.connection = None
    self.ready = False
# Process args append self.crypto_key.
# Child: keep existing guards, condition only the provider source:
if crypto_key is None:
    stack.enter_context(mock.patch.object(history_crypto, "build_history_crypto", forbidden_provider))
else:
    stack.enter_context(mock.patch.object(crypto_keystore, "get_or_create_history_key", return_value=crypto_key))
```

- [x] Step3: добавить точные behavioral assertions: same id/text после restart, wrong-key и валидный base64 tag tamper → IPC error/read+compact без journal изменения, correct key/original bytes recovery; encrypted export denied без grant/allowed с grant/stale grant после restart denied. Не hardcode incorrect nested schemas: взять production handler и действующие tests. Проверить actual provider call evidence, key registry positive control и zero violations.
- [x] Step4: новый файл + D defaults и связанные encryption файлы последовательно py312; ruff изменённых fixtures/tests, git diff --check, при требовании audit-all. Не повторять 1091-file local suite под high swap; exact public CI выполняет полный backend.

```bash
# Существующий isolated CPU-only runner, отдельный owned root для каждого файла.
# Не запускать stock harness setup, который может пересоздать shared venv.
for test_file in test_history_encryption_integration test_plaintext_export_integration test_history_crypto test_state_store_encryption test_history_encryption_failclosed_a5 test_history_journal_encryption_a5; do
  fixture_root="$(mktemp -d /private/tmp/a54-verify.XXXXXX)" || exit 1
  EAR_A53_FIXTURE_ROOT="$fixture_root" PYTHONPATH="$PWD/KrabEar:$PWD/KrabEar/tests" PYTHONDONTWRITEBYTECODE=1 /private/tmp/ear-a53-ci-repair-20261008/venv/bin/python /private/tmp/ear-a53-ci-repair-20261008/isolated_noaudio_pytest.py "KrabEar/tests/$test_file.py" -q || exit 1
done
ruff check KrabEar/tests/_plaintext_export_integration_backend.py KrabEar/tests/test_plaintext_export_integration.py KrabEar/tests/test_history_encryption_integration.py
git diff --check
```

- [x] Step5: независимый Astra adversarial diff gate PASS; source commit `a0cc704e` готов, documented bounded isolation и границы приёмки.
- [ ] Publication gate: отдельный PR, exact-SHA CI и merge; live deploy этим gate не разрешается. Документировать bounded Python isolation (не OS sandbox), actual crypto IPC PASS отдельно от SwiftUI/Keychain/live activation.

## Local result, 09.10 CEST

109 checks: actual-crypto/scanner10 + complete D27 (including standalone Swift3), and unchanged previously passed history_crypto18/StateStore13/failclosed13/journals28. Initial RED was unavailable optional fixture API, not a production defect. Only three test files changed. Pyflakes/diffcheck PASS; Ruff unavailable/no install. Flake8 has two pre-existing E306 (base has three); no new findings. Exact runtime used existing `/private/tmp/ear-a53-ci-repair-20261008/venv/bin/python` + `isolated_noaudio_pytest.py`, via `/private/tmp/run_a54_crypto.py`, one file per process. Final source freeze receipt `/private/tmp/a54-crypto-source-freeze-repaired.json`; tracked diff SHA256 `85a1386bd876dad218a5b62dffe4b793b01b910990096b1002dcce8455d8638b`. Independent Astra recheck PASS; original scanner P2 closed.

Synthetic key source is replaced; actual build_history_crypto/HistoryCrypto/AESGCM, StateStore and Unix IPC execute. Protected data/settings remain byte-exact on wrong key/tag corruption, audit appends only checked metadata; original fixture bytes/key restore read+compact. Same-key restart retains history/status/annotation; export denies without grant and after stale restart grant. No OS sandbox/Keychain recovery/SwiftUI/live activation acceptance claimed.

## Independent review repair

Первый Astra review обнаружил P2 fixture false-clean: JSON экранирование `repr(key)` обходило literal scanner, при этом прежний registry-only positive control оставался True. Production leak не установлен. Перед PR выполнен RED→GREEN actual escaped payload и structural scan original records/events/audit; валидные JSON/NDJSON в stream/files декодируются bounded. Positive controls обязаны пройти реальные owned file/log Capture/LocalTransport/audit sinks и same inspector, generic boolean/count only; отдельный logger propagate=False предотвращает запись probe в child/service logs. Ordinary scenarios не пишут synthetic key. Нельзя объявлять старые103PASS final scanner acceptance до исправления и нового Astra recheck.

Контекст release: #2082 merged `c106199f25d0be63d6d548cf9f3836277ebcff0e`, exact post-merge CI отдельно pending. Private Swift rollback byte-exact/strict+deep PASS, restart=false. Существующий consent binary имеет совпадающие executable inputs, но source→binary provenance UNKNOWN и CI artifacts отсутствуют: package reuse HOLD. Sentry read-only auth/API PASS, org108accepted/0rate_limited24h; current Ear ingress UNKNOWN (latest backend issue07Oct17:48UTC, agent issues empty). Resource22:32–33UTC all4 pressure2/free87–408MiB/activepaging; full package build/cutover HOLD. Private snapshots `/private/tmp/ear-a54-qualification-20261009/`; они не отправлялись external scouts.

Final independent gate: Astra PASS for repaired synthetic fixture delta; no new reachable blocking findings. Production crypto/IPC/guards unchanged; 10+27 latest tests independently checked in logs,72 unchanged checks retained from receipts. This gate does not qualify real Keychain, SwiftUI, OS sandbox or live activation.
