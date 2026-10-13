# Ear source integration status — 2026-10-14

Проверено 14.10.2026 CEST: source stack #2084–#2088 интегрирован в
`codex/krab-ear-v2`; final source SHA
`3c8a05bc67f51ef156034f4a37adf65f1807cec6`, оба post-merge workflow SUCCESS.
Этот receipt закрывает расхождение верхнего NOW с GitHub; runtime и живая
UI/Keychain приёмка остаются отдельными.

## GitHub и Git receipt

| PR | Merge SHA | Merged at, UTC 13.10.2026 |
|---|---|---|
| [#2084](https://github.com/Pavua/Krab-Ear/pull/2084) | `8af5ecac1984c7c1779b24479e5ee7e921228c62` | 01:34:59 |
| [#2085](https://github.com/Pavua/Krab-Ear/pull/2085) | `96fbeeed9e4af6da05185e7a9b7430aa51ab9a80` | 01:35:37 |
| [#2086](https://github.com/Pavua/Krab-Ear/pull/2086) | `18a46e7298495a61957531da8f31a8ecc9e419e9` | 01:36:02 |
| [#2087](https://github.com/Pavua/Krab-Ear/pull/2087) | `0d12f0e44a8e6983979e13e5be997c558703cc46` | 01:36:27 |
| [#2088](https://github.com/Pavua/Krab-Ear/pull/2088) | `3c8a05bc67f51ef156034f4a37adf65f1807cec6` | 01:36:54 |

- [CI 38332970422](https://github.com/Pavua/Krab-Ear/actions/runs/38332970422): completed/success, head SHA = final SHA выше.
- [krab-ear-ci 38332970357](https://github.com/Pavua/Krab-Ear/actions/runs/38332970357): completed/success, тот же exact SHA.
- Check-runs: 21 всего, **19 SUCCESS / 2 SKIPPED**. `Swift agent build` и `swift-build` пропущены по docs-фильтру; новый Swift build этим receipt не подтверждается.
- `git diff` reviewed `7e49e4ced72b695834ced3c2872ebac8973f3d01` → final SHA для `KrabEar/` и `native/` пустой. Python/Swift деревья совпадают с reviewed stack; подробности прежних проверок сохранены в [историческом NOW](../../NOW.md) и [RMW-карточке](../plans/2026-10-11-rmw-roots.md).
- Открытых PR в `Pavua/Krab-Ear` на момент проверки: **0**. Это снимок GitHub, последующие PR требуют свежей сверки.

## Scope и границы доказательств

Изменены только NOW, [plan](../plans/2026-10-14-source-integration-status.md) и
этот handoff. Исторические записи NOW сохранены и явно обозначены historical.
Production source, runtime, shared checkout и чужой WIP этой карточкой не меняются.

Release-конфигурация указывает на `9d7a0aac`; current loaded Python SHA —
**UNKNOWN**. Source merge и успешный post-merge CI не аттестуют deployment
`3c8a05bc`, текущую живую диктовку или UI/Keychain acceptance. Текущие alerts
о Swift требуют отдельной диагностики; их причина этим документом не установлена.

Merge/deploy/restart, encryption activation, Keychain, реальные
history/shares/schedules и purge не входят в эту карточку. OrbStack, VPN и
соседние проекты не затрагиваются. Ресурсные снимки и PID других сессий сюда
не переносятся.

Следующий безопасный source-блок: review жизненного цикла ключа и границы
activation по существующим механизмам и контрактам. Живая UI/Keychain приёмка и решение об activation
требуют отдельного owner scope; source/CI receipt их не заменяет.

## Повторная read-only проверка

Запускать из Git-копии Ear с имеющимся `gh`-доступом; команды ничего не меняют.

```bash
git rev-parse --show-toplevel
git branch --show-current
git status --short
git rev-parse HEAD
git rev-parse origin/codex/krab-ear-v2

for pr in 2084 2085 2086 2087 2088; do
  gh pr view "$pr" --repo Pavua/Krab-Ear --json number,state,mergedAt,mergeCommit,url
done
gh pr list --repo Pavua/Krab-Ear --state open --limit 100 --json number,url

gh run view 38332970422 --repo Pavua/Krab-Ear --json headSha,status,conclusion,url,workflowName
gh run view 38332970357 --repo Pavua/Krab-Ear --json headSha,status,conclusion,url,workflowName
gh api repos/Pavua/Krab-Ear/commits/3c8a05bc67f51ef156034f4a37adf65f1807cec6/check-runs --jq '{total_count, conclusions: ([.check_runs[].conclusion] | group_by(.) | map({conclusion: .[0], count: length})), skipped: [.check_runs[] | select(.conclusion == "skipped") | .name]}'

git diff --exit-code 7e49e4ced72b695834ced3c2872ebac8973f3d01 3c8a05bc67f51ef156034f4a37adf65f1807cec6 -- KrabEar native
```

Ожидаемый receipt: все пять PR MERGED с SHA/date из таблицы; открытый список
`[]`; оба runs completed/success на exact final SHA; 19 success/2 skipped;
пустой source diff с exit 0. Remote-tracking ref — локальный снимок: перед
новой source-мутацией координатор отдельно сверяет свежую удалённую базу.

Проверено для этой docs-only карточки: `git diff --check`, whitespace новых
файлов и все **34 локальные относительные ссылки** — PASS. Behavior-тесты,
build и live E2E не запускались, поскольку меняется только документация.
