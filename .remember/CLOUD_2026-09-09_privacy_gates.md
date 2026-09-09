# Cloud handoff 2026-09-09 — privacy fail-closed (Krab Ear)

Сверка `gh pr view` + `git fetch origin` **2026-09-09 ~20:28Z** (эта Cloud-сессия).
Не live smoke. Runtime **не** деплоили, **не** kickstart, Ear **не** safe-restart.
PID backend/агента 05.09 в `docs/NOW.md` **не** перепроверяли — это snapshot, не свежие.

**Cursor Cloud Ubuntu cannot deploy to the owner Mac.** Нет `launchctl`, нет доступа к
прод-launchd / Unix-сокету Ear. Source SHA ≠ задеплоенный код. Не выдумывать live PID.

База: `origin/codex/krab-ear-v2` = **`1c8d6502a76e`** squash **#2014**.

`.remember/next_session.md` в этом Cloud-клоне **нет** — не создавать PID-файл с выдуманными
процессами. Этот файл — source-checkpoint.

Предыдущий handoff на этой ветке (#2009) **устарел** (писал OPEN #2008 и базу `#2007`).

## PR (Pavua/Krab-Ear → `codex/krab-ear-v2`)

| PR | Состояние | squash SHA | Что в коде |
|---|---|---|---|
| [#2005](https://github.com/Pavua/Krab-Ear/pull/2005) scoring | **MERGED** 2026-09-09T04:56:52Z | `5e24ee6ff91c` | `TextScoringService` fail-closed + wiring `settings_svc` |
| [#2006](https://github.com/Pavua/Krab-Ear/pull/2006) history | **MERGED** 2026-09-09T05:19:36Z | `5c88af5b42ab` | History fail-closed |
| [#2007](https://github.com/Pavua/Krab-Ear/pull/2007) REST STT/TTS | **MERGED** 2026-09-09T05:57:29Z | `033090ed99f0` | Ordinary REST STT/TTS + persist/WS `/v1/stream`. **Call-profile не трогали** |
| [#2008](https://github.com/Pavua/Krab-Ear/pull/2008) siblings | **MERGED** 2026-09-09T20:15:05Z | `2e61984b0a52` | collection/speaker + `TextProcessingService`; wiring `settings_get` в `service.py` |
| [#2009](https://github.com/Pavua/Krab-Ear/pull/2009) | **OPEN draft** (этот docs PR) | rebase на `1c8d6502` | только `.remember` + короткий note в `docs/NOW.md` |
| [#2010](https://github.com/Pavua/Krab-Ear/pull/2010) health | **MERGED** 2026-09-09T20:26:12Z | `68c0e7823e9e` | health diagnostics fail-closed |
| [#2011](https://github.com/Pavua/Krab-Ear/pull/2011) transcript versioning | **MERGED** 2026-09-09T20:26:31Z | `04a1c92786b4` | transcript versioning fail-closed |
| [#2012](https://github.com/Pavua/Krab-Ear/pull/2012) dedup/replay/chain | **MERGED** 2026-09-09T20:26:48Z | `55ed54d42559` | dedup / event replay / recording chain |
| [#2013](https://github.com/Pavua/Krab-Ear/pull/2013) error reporter | **MERGED** 2026-09-09T20:27:27Z | `124fa869c0a1` | ErrorReporter ingest/report redaction |
| [#2014](https://github.com/Pavua/Krab-Ear/pull/2014) auto glossary | **MERGED** 2026-09-09T20:27:43Z | `1c8d6502a76e` | AutoGlossary IO/`get_cached` fail-closed |

Другой OPEN (не эта волна): [#2004](https://github.com/Pavua/Krab-Ear/pull/2004) brain-lease TTL.
Новее privacy-PR после #2014 на сверке **нет**.

Source-merge ≠ runtime на Mac владельца.

## Remaining fail-open (известное)

- **`BackendService._get_runtime_setting` IO/lock fail-open** — отдельная карточка. Канон:
  **Grok Extra High**, Fast **OFF**. **Не** править `service.py` из Cloud/docs-PR.
- Не полный privacy-аудит всех transcript-bearing путей. Волна 2005–2014 закрыла
  известные siblings; новые гейты всё ещё могут быть fail-open.
- REST **call-profile / GigaAM** (`voice_gateway_call`) **не** в этой волне (#2007 ordinary only).
- mlx lock timeout на инференсе (C3 в NOW) — **не** privacy и **не** делали.

## Не делать

- Не kickstart / не `safe_backend_restart` без владельца. Cloud **не умеет** деплой на Mac.
- Не включать `KRAB_STT_EAR_CALL_PROFILE_ENABLED` / `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED`.
- Не `git add -A`. Не `audit/*`. Не CLAUDE.md целиком. Не #1875.
- Не трогать `service.py` из docs-PR. Не выдумывать live PID.

## Регламент моделей / режимов (владелец 2026-09-09)

- Координатор чата: **Grok 4.6**, Fast **OFF**. Extra High только на развилку/аудит. **High** на доки/handoff (как этот PR).
- Privacy siblings (Python+pytest, не `service.py`): **Grok High**, Fast **OFF**.
- Мелочь / wiring / тест: Composer 2.5 **medium**, Fast **OFF**.
- **Grok Extra High**, Fast **OFF**, только: VG `app/main.py` overlap (#315) **или** generic
  `service.py` `_get_runtime_setting`.
- Cloud + Multitask: включать когда ≥2 disjoint репо/файла; выключать на один крошечный PR.
- Не Fable / Opus Extra High default. Не Fast **ON**.
