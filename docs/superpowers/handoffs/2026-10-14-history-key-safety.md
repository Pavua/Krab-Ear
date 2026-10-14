# History key safety — source checkpoint, 2026-10-14

## Состояние и база

Owned branch `codex/ear-key-safety-20261014` основана на reviewed #2091 head
`19f5deaf60c92c5b771f89d376e0b9e32e5dbf3e`, уже включённом merge-коммитом
`86a0b1978086bb4e2d9bbbf7505a264cd2abf928` в `codex/krab-ear-v2`.
Shared checkout с чужим WIP не менялся.

[Спека](../specs/2026-10-14-history-key-safety.md) и
[план](../plans/2026-10-14-history-key-safety.md) определяют границы карточки.
Worker владеет четырьмя backend-модулями и tests; root — docs/Git/release.

## Поведение

Единый EX guard разрешает key creation только при доказанном отсутствии
`ENC1` во всех десяти управляемых журналах. Restore добавляет prospective
journals к live registry. Unknown bytes/readability требуют lookup-only.
Passive reads не повышают SH до EX.

Central settings prepare проверяет CAS, exact bool, repair permission,
validation и serialized size до свежего activation preflight. Warm cache
не заменяет provider lookup. Unrelated already-ON edits не обращаются
к provider; dedicated repeated ON проверяется снова. Dedicated supplied
non-bool отклоняется до provider, missing enabled сохраняет default False.

Legacy restore выполняет один preflight до copy2 и применяет ту же prepared
тройку `(settings, serialized, crypto_candidate)` после копирования, под тем
же EX. Guard ON→OFF сохраняет prospective-only semantics.

## Независимый review и дополнительный P2

Early frozen production patch
`f415507d506565e89facec5e1e27007fbaea108b2d2f8aa2b75f6885ce58f77d`
получил BLOCK: fresh crypto попадал в cache до settings commit. Missing
settings → явный repair ON → failed atomic write → append создавал ENC1,
а последующий OFF repair уже блокировался. Proper RED: 3 FAIL и 2 controls
PASS. Этот patch не публиковался и runtime не достигал.

Исправленный frozen production patch
`c3d4fc32ba0169aff50945710158debfd2e32278102d1afd54d6c77811ef3817`
получил **Astra Ultra production GO**, seal совпал до/после review.
Кандидат остаётся local до возврата `atomic_write_text`: `os.replace` —
последняя операция helper. Затем cache устанавливается до directory fsync.
Его поздний отказ означает visible policy/cache APPLIED, durability UNKNOWN;
автоматического удаления ключа или отката видимой policy нет.

## Локальная проверка

Исходная версия тестовой фикстуры, Python 3.12.7 и 3.14.8: все
**33/33 выбранных файла**, по **1238 PASS +
142 subtests** на каждой версии; 0 skips, 3 Swift integration cases deselected.
Новые regressions — 48 реальных Store/AES и 12 настоящих временных Unix IPC
cases; synthetic provider и fake Engine исключают реальный Keychain/ML.
Каждый файл выполнялся последовательно отдельным процессом с temp HOME/data,
запретом внешних writes/network/provider и CPU import fence.

Основной full33 runner имеет SHA `5ccadd8e582091c95546b159c93730929e3dd2345aa90366480d4c8fe5356278`.
Четыре файла, создающие дочерние процессы, дополнительно проверены на обеих
версиях под усиленным runner `6ff77c1216ecff0253eec6e5a90692e220762b150a544033a1580a7153302e2e`:
по **185 PASS + 90 subtests**, 0 skips, 3 deselected. Этот дополнительный
guard разрешает девять exact child targets с owned temporary arguments;
остальные 29 файлов не содержат найденных spawn markers. Полный набор из 33
файлов под усиленным runner не заявляется.

Critical flake8 всех 23 owned Python paths, `git diff --check`, reverse patch
check и `make audit-all` (14 gates) — PASS. Proper initial RED: 34 FAIL /
3 controls PASS на обеих версиях; actual IPC baseline RED: 5 FAIL / 1 control
PASS; дополнительный cache-before-commit P2 воспроизведён как 3 FAIL /
2 controls PASS до исправления. Исходные 23 source/test paths сохранены в
`implementation.patch`, SHA `521772ad3b846e8105238a0de8496d4b00695c5e84ebdd5ee7da5cf252a5c8e1`.

Ignored raw logs/seals находятся в `.superpowers/sdd/2026-10-14-key-safety/`
owned worktree и не коммитятся. Original `verification-original-full33.json`
имеет SHA
`a3da7d5ba5dfd79dc261a8e498afb735c96a2d5e02b5fffa404d2d407d896b29`.

## Whole-card review: изоляция IPC-фикстуры

Финальный whole-card review нашёл test-only P2: общий `$HOME/key-safety.sock`
и readiness по `path.exists()` позволяли принять чужой listener. Две настоящие
фикстуры с разными profiles воспроизвели историю **1/1 вместо 1/0**.
Исправление использует уникальный короткий temporary socket root, собственный
`SocketOwnershipClaim`, `LISTENING` + живой thread и cleanup stop/join/release/
tempdir/`service.close()`. Production не менялся.

Финальный IPC-файл: **13/13 PASS** на Python 3.12.7 и 3.14.8, 0 skips/deselected,
critical lint/diff-check PASS. Остальные 22 owned paths и 32 выбранных test paths
байт-идентичны прежнему проверенному состоянию; полный33 заново не запускался.
`implementation-v3.patch` SHA
`06ac5d7bb7a585e7a5ebdce1bb95b791b1c8fc2cdd16a33b80c80390cd47e094`,
narrow delta SHA `74cf3cf2f93d127dbcc3cad8eae48543e6730dfa71c2796b5094a4d444f1b36d`.
`verification-v3.json` SHA
`e3c70c134e45678bc7f937d5f66ad35dff3eea2afdba3d655b1ab0b1bea8368a`
явно разделяет original full33 и финальный IPC13. В нём также исправлен только
описательный historical repair RED count: raw log даёт 2 FAIL / 1 PASS.
Повторный scoped review и новый hosted CI на этом checkpoint ещё требуются.

## Release и пределы доказательств

Реальная encryption OFF. Temp profiles/provider stubs, actual AES и isolated
Unix IPC не доказывают production Keychain acceptance. Карточка не исправляет
уже потерянные/смешанные ключи и не доказывает соответствие найденного ключа
каждому старому `ENC1`. Неиспользуемый созданный ключ после отказа disk commit
допустим: общей транзакции Keychain+filesystem нет.

У #2091 оба post-merge workflow точного `86a0b197` SUCCESS:
[CI](https://github.com/Pavua/Krab-Ear/actions/runs/38493755193) и
[backend](https://github.com/Pavua/Krab-Ear/actions/runs/38493755224),
все 21 jobs terminal: 19 SUCCESS / 2 SKIPPED. Это не CI новой crypto-карточки.

Владелец разрешил merge/deploy/E2E. Подготовленный release `86a0b197` не
применён; включение этой карточки в единый cutover требует whole-card GO,
exact-head CI, обоих workflow собственного final merge SHA, нового bundle,
native/package parity и свежего Main/VG quiet ACK. До этого loaded release
остаётся `9d7a0aac`; его in-memory Python SHA не подтверждён.

Причины прежних RU GigaAM HTTP504, default REST MLX timeout/failfast70 и
AGENT-19 AppHanging остаются UNKNOWN. Real activation/migration/purge/rotation,
соседние lifecycle и OrbStack/VPN не меняются. Sol 6.1 High — implementation/CI;
Astra Ultra — final whole-card security/release decision.
