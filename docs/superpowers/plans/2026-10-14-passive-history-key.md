# Passive history key — узкий source fix, 2026-10-14

**Goal:** пассивное получение history crypto никогда не создаёт Keychain-ключ.
**База:** `3c8a05bc67f51ef156034f4a37adf65f1807cec6`,
`codex/ear-passive-key-20261014`, отдельный owned worktree.

Карточка следует независимому Astra Ultra design GO только для read-side
гарантии. Прямой append при missing key рядом с ENC1 и generic activation
через `set_settings` остаются OPEN; это не полный lifecycle/activation fix.
Исполнитель не выполняет live Keychain/history/settings/purge, runtime, merge/deploy/install.
Ранний неудачный unguarded dependency test мог прочитать живой ключ; limitation
и исправленный harness зафиксированы в handoff, это не принятый PASS.
Исполнитель не коммитит и не пушит; shared checkout и чужой WIP сохраняются.

## Architecture и ownership

- `crypto_keystore.py`: `HistoryKeyMissing(KeystoreUnavailable)` и lookup-only
  `get_history_key()`. Старый `get_or_create_history_key()` ловит только
  `HistoryKeyMissing`, затем сохраняет прежнее noclobber-создание.
- `history_crypto.py`: `build_history_crypto(*, allow_create=True)` сохраняет
  no-arg контракт; passive false выбирает lookup-only provider. Сообщение об
  отказе не обещает plaintext fallback.
- `state_store.py`: `_get_history_crypto(*, allow_create=False)` передаёт режим
  builder; отрицательный результат не мешает повтору после возврата ключа.
  `_maybe_encrypt` и direct migration явно оставляют `allow_create=True`.
  Существующие passive snapshot/recovery/purge-union callers используют default.
- Тесты: существующие `test_crypto_keystore.py`, `test_history_crypto.py`,
  `test_state_store_encryption.py`; только необходимые keyword-адаптеры fixture.
  Формат ENC1, global service/account и dedicated IPC setter не меняются.

## Последовательность и доказательства

- [x] RED: real temporary StateStore + random production AES-GCM; fake только
  `_run_security` (find rc44, add при ошибочном вызове успешно). Existing main
  ENC1 и sidecar-only read отказывают без add и без изменения bytes.
- [x] Missing passive getter → возвращённый old key на том же store: retry
  возвращает crypto и читает прежний fixture, без sticky negative cache.
- [x] Empty ON snapshot getter и pending recovery с missing key: без создания,
  byte-exact settings/journals/transaction state.
- [x] Minimal production fix после правильного behavioral RED.
- [x] Existing key, wrong-key/tag, corrupt/denied/timeout lookup controls.
  Deliberate create/new write и существующие post-purge controls сохранены.
- [x] GREEN: affected unit files последовательно через existing Python 3.14
  и существующий `/private/tmp/py312`, без conftest-heavy полного pytest.
- [x] Настоящий isolated Unix IPC: missing cold read → old key restored на том
  же backend, без add/перезаписи; никакой live profile/Keychain.
- [x] Необходимые fixture keyword adapters, targeted diff-check.
  Guarded selected unit/pytest — 549 на каждой версии; real IPC — ещё 1
  на каждой. Baseline IPC RED: неожиданное создание ключа (1 вместо 0).
- [x] `make -j1 audit-all`: все 14 gates PASS после первоначального freeze;
  результат сохранён в external receipt и PR body.
  Полный backend/Swift CI выполняет root hosted.
- [x] Handoff с точными командами/results и остаточными рисками. Root проводит
  независимый Astra Ultra whole-diff gate; новый SHA требует собственного CI.

Raw RED/GREEN и command ledger находятся в ignored
`.superpowers/sdd/2026-10-14-passive-history-key/`. Полный локальный suite,
build/install/restart, настоящие данные и Keychain не входят в эту карточку.

## Hosted CI follow-up

Два purge-теста из полного CI вызывали passive getter как подготовку нового
encrypted synthetic-профиля. Proper guarded RED воспроизвёл оба отказа
`crypto is None`; fixture `_store_crypto` теперь явно передаёт
`allow_create=True`. Проверки реального purge/ledger не ослабляются,
production read-side контракт остаётся прежним. Полный test-файл проверяется
на обеих существующих Python-версиях: **100/100 + 100/100 PASS**.

Третий CI failure возвращал `MagicMock` из warmup handler. Collection-time
patch может сохраниться в захваченном `backend.service.Transcriber` alias.
CPU-only poison-control воспроизвёл точную assertion (`loaded` отсутствует
в MagicMock). Handler shape test теперь явно инъецирует transcriber/engine,
выключает startup warmup в temp store и проверяет единственный explicit
warmup, полный dict и отсутствие вызова default factory. Poison-control
GREEN и полный файл **10/10 PASS на Python 3.12 и 3.14**. Production STT
не меняется. Новый head требует нового полного hosted CI.

Sol 6.1 High — реализация и проверки; Astra Ultra — final whole-diff gate.
