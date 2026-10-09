# A53 Card A — Authorizer + typed PolicySnapshot + central commit

База: свежий `origin/codex/krab-ear-v2` (не старый SHA handoff вслепую).
Контракт: `A53_STRONG_MODEL_HANDOFF.md` SHA256
`9bb260fe303b081a4883f9e4da8d7e3bffeebc8774898f69ed92acf30c5def5a`
(FINAL GO, `COUNTER_REVIEW_A53.md`); спека §7
`docs/superpowers/specs/2026-09-24-a5-history-at-rest-design.md` с
подразделами 7.1–7.7. Порядок README п.3: сначала §7-уточнения (сделано),
затем карточки по одной. Эта карточка предназначена для реализации и проверки
кода зоны A в пределах Scope и списка файлов ниже.

## Scope (входит)

- Новый `KrabEar/backend/plaintext_export_authorization.py`: typed
  `PolicySnapshot` (`KNOWN_OFF | KNOWN_ON | UNKNOWN`), отдельно privacy bool,
  fingerprint, reason; grant/receipt store только RAM; lock ordering
  StateStore → authorizer, никогда наоборот.
- `state_store.py`: typed snapshot reader
  `_read_plaintext_policy_snapshot_unlocked`, internal
  `_plaintext_export_policy_revision` (`uuid4().hex`), центральный settings
  commit (`save_settings` + `_save_settings_unlocked`: A5 write guards, затем
  flags + новая revision одним atomic replace temp+fsync+rename).
- `settings_service.py`: startup normalization и все supported writes через
  центральный commit; explicit initialization только для достоверно нового
  профиля (`data_dir` создан этим startup с `exist_ok=False`, без
  восстановленного состояния).
- `history_service.py:5456` settings-only branch: заменить `copy2(settings)` на
  validated commit под тем же StateStore lock, без вложения
  `save_settings`/`history_flock` нового FD (`state_store.py:253`).
  Запрет encrypted snapshot restore settings (`encrypted_snapshot.py:3034`)
  сохранить.
- `service.py` init/IPC wiring: создание authorizer ДО scheduler/manager
  threads; имена `get_plaintext_export_policy` /
  `grant_plaintext_export_session` / `revoke_plaintext_export_session` /
  `validate_plaintext_export` + namespace `plaintext_export` в export IPC;
  reasons `plaintext_confirmation_required` / `plaintext_session_expired` /
  `plaintext_policy_unavailable` / `privacy_mode_active`.
- Точечная secret redaction: `service.py:3233` handle_request,
  `ipc_server.py:389`, `observability.py:182` + `include_local_variables=False:402`;
  её regression tests. Новый `test_plaintext_export_authorization.py`; IPC
  reference/spec.

## Не входит (запреты)

- Не менять A5.2 `read_history_encryption_flag` и не наследовать его
  fresh-profile OFF fallback в authorizer.
- Не копировать A5.2 mock/no-store OFF fallback.
- Не изобретать другую модель доверия, policy fallback, единый флаг-разрешение
  на весь backend. Отдельный peer-auth/bootstrap дизайн не входит и не
  обещается (будущий BLOCK, не часть A5.3).
- Не трогать Python sinks карточки B, Swift карточки C, интеграцию D.

## Файлы

- Создать: `KrabEar/backend/plaintext_export_authorization.py`,
  `KrabEar/tests/test_plaintext_export_authorization.py`.
- Править: `KrabEar/backend/state_store.py`, `KrabEar/backend/settings_service.py`,
  `KrabEar/backend/history_service.py` (только settings-branch :5456),
  `KrabEar/backend/service.py` (init + IPC wiring),
  `KrabEar/backend/ipc_server.py`, `KrabEar/backend/observability.py`,
  `docs/IPC_API_REFERENCE.md` (имена + namespace + reasons).

## Шаги

1. Typed snapshot + UNKNOWN-триггеры (§7.1): missing file, не-object/битый JSON,
   duplicate policy keys, нечитаемый/нерегулярный файл, отсутствие любого
   privacy/encryption ключа, не-exact-bool, missing/invalid revision,
   ошибка/timeout lock → UNKNOWN, revoke grants/receipts, рост generation.
2. `_read_plaintext_policy_snapshot_unlocked`: `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`,
   fstat regular, ≤16 MiB, fstat до/после + lstat совпадают по
   dev/ino/size/mtime_ns/ctime_ns, иначе UNKNOWN без retry по старому snapshot.
3. Fingerprint `(profile identity, st_dev, st_ino, st_size, st_mtime_ns,
   st_ctime_ns, SHA256(raw_bytes), internal_revision)`; JSON flags+revision ИЗ
   ЭТИХ bytes; mismatch → revoke ДО validation, update remembered
   snapshot/generation; valid+старый context → `plaintext_session_expired`;
   UNKNOWN → `plaintext_policy_unavailable`; same-bool replacement тоже отзывает.
4. Central commit + revision (§7.3): каждая поддержанная запись генерирует новый
   `uuid4().hex`, входное/backup значение игнорировать; import/restore/reset/
   recovery через этот commit; `:5456` — validated commit без вложенных FD.
5. Explicit init + legacy migration (§7.2): новый профиль только при
   `exist_ok=False`-создании; legacy оба-valid-bool без revision — обычная
   startup migration под store lock без grant; legacy missing/invalid — UNKNOWN
   до validated repair; SH→EX upgrade запрещён.
6. Epoch/session/capability (§7.4): epoch 32 bytes/process, app_session UUID,
   capability `token_urlsafe(32)`, только RAM; grant bound
   profile/service+app_session+epoch+generation+fingerprint; authorizer до
   scheduler/manager threads; privacy true всегда deny.
7. IPC wiring + redaction (§7.6–7.7): имена/namespace/reasons; trusted-client
   consent честно (same-UID self-grant возможен, headless без context → deny);
   bearer/receipt/app-session secret вне logs/diagnostics/profile/settings/
   backups; revision не secret; `operation_seq` монотонный + high-water.
8. Документация: IPC reference, §7 уже обновлена — сверить расхождения.

## Behavioral RED→GREEN (полные тексты из контракта, зона A)

- п.4: отдельный тест текущей trust boundary фиксирует возможность same-UID
  вызвать grant RPC напрямую; НЕ писать ложный acceptance «headless impersonation
  denied». Supported script-path обязан не вызывать grant автоматически. Свежий
  BackendService имеет новый epoch; replay старого grant/receipt отклонён; новый
  app_session_id после simulated crash не принимает старый token.
  Read-history/show/settings-status не создают grants.
- п.5: через каждый supported update path (set/import/restore/reset/direct save)
  проверить изменение revision и отзыв, включая ON→OFF→ON без export между
  переходами; входной/backup revision никогда не сохраняется.
- п.5a (обязательная cross-process fixture): multiprocessing spawn, A/B
  независимые StateStore одного TemporaryDirectory без ENC1 (чтобы OFF был
  допустим). A issue grant при ON; Pipe/Event сообщает ready; B save OFF, затем
  ON, оба через поддержанный commit, сообщает done; первая validation A старого
  grant → session_expired и 0 writes. Никаких общих Python store/mock и sleep.
- п.5b: отдельный process B атомарно заменяет settings теми же flags и той же
  revision/bytes; A ловит новый fingerprint и отказывает старому grant. Повторить
  поддержанную settings restore/recovery с новым revision. Synthetic fixture, не
  работа с реальной историей.
- п.5c: удержать store lock в B, запустить validation A, затем commit/release
  через barrier: A обязана прочитать новый consistent snapshot либо bounded
  timeout→UNKNOWN; не старый cache. Deadlock/inverted locks → timeout test
  failure; children terminate/join в finally только свои fixture PID.
- п.6: удалить settings отдельно из valid OFF и valid ON;
  corrupt/non-object/duplicate keys/non-bool/missing flag/revision/unreadable/
  nonregular/oversize/unstable snapshot/provider exception → typed UNKNOWN,
  issue+validate deny+revoke. Вернуть прежние bytes → старый grant всё равно
  deny. Fresh explicit initialization и valid legacy revision migration → known
  state; incomplete legacy остаётся UNKNOWN до validated repair.
- п.13 (частично, authorizer-сторона): KNOWN_OFF+privacy false сохраняет export;
  privacy true/UNKNOWN deny. Сохранить schema parity read/render-only ответов и
  errors без секретов.
- п.15a (частично, redaction): sentinel-секреты разных значений прогнать через
  ACTUAL handle_request и socket connection error paths: malformed params/JSON,
  unknown method с auth в params, unauthorized signing, mocked handler
  RuntimeError и неожиданный exception с token в exception text. Capture log
  records + formatted traceback, fake Sentry transport через before_send,
  diagnostics serialization: ни один sentinel не появляется; request payload не
  логируется; ошибки не echo token. Никаких внешних Sentry событий.
- п.15b (частично): проверить normal settings/profile/backup serialization
  содержит только internal revision, не grant/receipt/session ID.
- Unit authorizer: enum KNOWN_OFF/KNOWN_ON/UNKNOWN, lock ordering
  StateStore→authorizer (инверсия ловится timeout-тестом), manager без store —
  inject callback, отсутствие authorizer = deny.

Каждый `BackendService(...)` в тесте ОБЯЗАН `service.close()` в `tearDown`.

## Команды исполнителю карточки

```bash
/Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python --version  # 3.14.6
PYTHONPATH="$PWD/KrabEar" /Users/pablito/Antigravity_AGENTS/Krab\ Ear/.venv_krab_ear/bin/python -m pytest KrabEar/tests/test_plaintext_export_authorization.py -v
scripts/pre_merge_py312_check.sh KrabEar/tests/test_plaintext_export_authorization.py
make audit-all
```

## DoD

- Все тесты зоны A GREEN локально + `pre_merge_py312` GREEN + `audit-all` без
  новых нарушений; redaction-sentinel отсутствуют в captured logs/traceback/
  fake-Sentry/diagnostics.
- Cross-process fixtures 5a–5c детерминированы (barriers/events, не sleep),
  дети join/cleanup, 0 writes при deny/session_expired/plaintext_policy_unavailable.
- Восстановление settings bytes не воскрешает grant; ON→OFF→ON отзывает.
- Diff ограничен файлами раздела «Файлы»; sinks B / Swift C не тронуты.

## Gate

Без whole-diff независимого Astra High review — BLOCK (не мержить, не объявлять
encryption GO; устный PASS на новый SHA не переносить).
