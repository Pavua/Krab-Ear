# Combined runtime release — 2026-10-14

Checkpoint 04:01 UTC (06:01 CEST). Это наблюдённая установка, один локальный
voice E2E и завершённые15min/16snapshots стабильности. Старые source-only/HOLD
checkpoints в NOW описывают прежние снимки.

## Source и CI

- #2084–#2088 ранее интегрированы; #2089 — source reconciliation.
- [#2090](https://github.com/Pavua/Krab-Ear/pull/2090): passive history key lookup.
- [#2091](https://github.com/Pavua/Krab-Ear/pull/2091): partial PCM cleanup и
  stale watchdog, способный убить следующий запрос того же subprocess.
- [#2092](https://github.com/Pavua/Krab-Ear/pull/2092): fresh crypto preflight
  под EX для live ∪ prospective journals; отсутствие replacement key при
  ENC1/нечитаемом состоянии; prepare до mutation; публикация crypto candidate
  после успешного settings replace. Passive lookup сохраняет SH.

Final merge **`26542ccdc50ee1a8e062c32b93da24a2e16c61cb`**; merge tree совпадает
с reviewed head `4d67365ba62aa23878541da2d9ac9083c52aa46b`. Independent Astra
whole-card GO и scoped test-fixture GO получены до интеграции.

| Gate | Exact SHA / результат |
| --- | --- |
| #2092 head | `4d67365b`, 3 workflow SUCCESS; 24 checks SUCCESS / 3 SKIPPED |
| Final backend | [38504270511](https://github.com/Pavua/Krab-Ear/actions/runs/38504270511), `26542ccd`, SUCCESS |
| Final CI | [38504270497](https://github.com/Pavua/Krab-Ear/actions/runs/38504270497), `26542ccd`, SUCCESS |
| Final post-merge jobs | 19 SUCCESS / 2 SKIPPED |

Локальные source regressions: Python3.12.7/3.14.8 по 1238 PASS +142 subtests
на исходной фикстуре 33 файлов. Затем усиленный guard четырёх child-bearing
файлов: по185 PASS +90 subtests; финальная изолированная IPC-фикстура — по13
PASS; terminal-status fixture — по5 PASS. Это разные запуски, их нельзя
складывать в один suite receipt. Финальный hosted CI подтверждает final head.

## Установка и runtime

Owner явно разрешил E2E/merge/deploy. Получены новые Main/VG quiet ACK до
04:10 UTC; собственные Ear guards проверялись отдельно. OrbStack/VPN и
жизненный цикл соседних проектов не менялись.

Pinned release worktree — `$HOME/.codex/worktrees/ear-release-20261014/Krab Ear`,
detached/locked/clean на final SHA. У обоих Python jobs фактически проверены
loaded entrypoint и PYTHONPATH на этот root; interpreter, cwd и log paths
сохранены. Python in-memory module SHA этим не аттестован.

Native build source — `3c8a05bc67f51ef156034f4a37adf65f1807cec6`; с final source
совпали все301 native input hashes/modes, 6 cached artifact SHA, 3 workspace
pins, без изменений `native/`/`scripts/`. Проверены полные manifests нового
app, старого live app и backup, strict/deep codesign, прежний designated
requirement, binary/dSYM UUID и одна файловая система. Сборку не повторяли,
новую signing identity/TCC не вводили.

Две полные последовательности idle 0/20/40/60s: до Swift shutdown и после
него (wake word был включён). Перед каждым bootout сохранён новый PID/start/
descendants/PGID baseline. Старые jobs/process groups и endpoints полностью
drained. Оба plist установлены при остановленных службах; целое app заменено
Darwin RENAME_SWAP с fsync. Whole old app, backup и оба before plist сохранены.

| Роль | Новый PID | Start CEST | Runs | Наблюдённая готовность |
| --- | --- | --- | --- | --- |
| Backend | 70875 | 05:41:22 | 1 | IPC ping; новые GigaAM ready и engine warmup markers |
| REST | 71046 | 05:42:09 | 1 | HTTP200/statusok; sole5005 listener принадлежит этому PID |
| Swift | 71349 | 05:42:46 | 1 | sole canonical app; новый passive Variant B marker |

Поколения стабильны на двух снимках с интервалом25s. Swift startup AX trusted
**true**; это не доказательство настоящей вставки в приложение пользователя.
История **23274**, `get_history_page(limit=1,cursor=null)` даёт одну запись,
restore warning отсутствует. Текст записей не выводился.
Encryption/migrating/restore_pending/recording/meeting — exact False.

## Узкий voice E2E

03:44:47 UTC: один POST на localhost5005 через фактические REST interpreter/
environment/cwd, существующий локальный synthetic RU WAV. `voice_gateway_call`,
`lang_hint=ru`, deadline8s, persist_history=false, diarize=false, без retry.
**HTTP200 за2.062s**, statusok, непустой текст (проверено boolean), adapter
GigaAM, subprocess transport, languageRU, пустой history_id, count23274 unchanged.
Старый baseline9d дал HTTP504/8.641s. Новый успешный запрос не устанавливает
корневую причину прежнего таймаута и не доказывает sustained reliability.

15min stability monitor03:45:48–04:00:48UTC: **16/16 snapshots PASS**,
900.233s, PID/start/runs всех трёх jobs неизменны, strict flags и history count
сохранены. Final source/app identity и health повторно проверены04:01UTC.
Final Sentry04:01:30UTC API200/complete: backend0recent/agent5прежних,
lastSeen после новых bootstrap отсутствует. Новый Ear ingress этим не доказан.
Main/VG отправлен фактический FINISH тихого окна; root release CLI завершены.
Receipt directory: `$HOME/.codex/ear-release-staging/20261014/`:
`release-state.json`, `apply-26542-journal.jsonl`, `maintenance-ready-26542.json`,
`synthetic-new-26542-receipt.json`, `stability-26542.jsonl`,
`sentry-postcutover-final-26542.json`, `release-finish-26542.json`.

## Открыто и следующий шаг

До cutover Sentry: backend0 unresolved с lastSeen за24h; Swift5, все последние
events — App Hanging старого runtime. Причина UNKNOWN, issues не закрывались.
Свежий org API/quota и отсутствие новых issues не доказывают новый Ear ingress.

Default REST Whisper/MLX timeout/failfast70 остаётся отдельной диагностикой;
actual Whisper inference не запускался из-за непроверенного no-download
контракта. RU call-profile PASS не является PSTN/ES/Whisper приёмкой.
Real encryption activation/migration/purge/key rotation не выполнялись;
сохранённые некорректные/mixed keys этим source fix не восстанавливаются.

Следующий безопасный шаг — отдельная root-cause карточка AppHang/default
Whisper; перед activation
нужна отдельная UI/Keychain/Timeline live приёмка. Для docs/CI достаточно
Sol6.1 Medium, для диагностики High; Astra Ultra — точечно на новый рискованный
whole-diff/release gate. Docs-only merge не требует runtime restart.
