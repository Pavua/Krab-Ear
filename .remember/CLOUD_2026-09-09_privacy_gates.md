# Cloud handoff 2026-09-09 вечер — privacy fail-closed (Krab Ear)

Сверка `gh pr view` + `git fetch origin` **2026-09-09 вечер** (эта Cloud-сессия).
Не live smoke. Runtime **не** деплоили, **не** kickstart, Ear **не** safe-restart.
PID backend/агента 05.09 в `docs/NOW.md` **не** перепроверяли — это snapshot, не свежие.

База: `origin/codex/krab-ear-v2` = **`033090ed`** squash **#2007**.

Предыдущий handoff на этой ветке (#2009) **устарел**: писал, что #2006/#2007 ещё OPEN.

## PR (Pavua/Krab-Ear → `codex/krab-ear-v2`)

| PR | Состояние | SHA | Что в коде |
|---|---|---|---|
| [#2005](https://github.com/Pavua/Krab-Ear/pull/2005) | **MERGED** 2026-09-09T04:56:52Z squash `5e24ee6` | `5e24ee6ff91c` | `TextScoringService` fail-closed + wiring `settings_svc` в `service.py` |
| [#2006](https://github.com/Pavua/Krab-Ear/pull/2006) | **MERGED** 2026-09-09T05:19:36Z squash `5c88af5` | `5c88af5b42ab` | History fail-closed |
| [#2007](https://github.com/Pavua/Krab-Ear/pull/2007) | **MERGED** 2026-09-09T05:57:29Z squash `033090ed` | `033090ed99f0` | Ordinary REST STT/TTS + persist/WS `/v1/stream` helper. **Call-profile не трогали** |
| [#2008](https://github.com/Pavua/Krab-Ear/pull/2008) | **OPEN draft** | `b87a8a5f` | collection/speaker LIVE; `TextProcessingService` optional `settings_svc`. **`service.py` wiring text_processing — follow-up** (в этом PR нет) |
| [#2009](https://github.com/Pavua/Krab-Ear/pull/2009) | **OPEN draft** (этот docs PR) | rebase на `033090ed` | только `.remember` + `docs/NOW.md` |

Другой OPEN (не эта сессия): [#2004](https://github.com/Pavua/Krab-Ear/pull/2004) brain-lease TTL.

CI на вечерней сверке: #2008 `backend-tests` **SUCCESS** (не merge-gate без владельца). Перепроверять `gh pr checks` перед merge.

## Follow-up (source)

- #2008: прокинуть `settings_svc` в `TextProcessingService` из `service.py` (как #2005 для scoring), не «заодно» с чужим diff.
- `_get_runtime_setting` fail-open на IO — аудит отметил; отдельная карточка.
- mlx lock timeout на инференсе (C3 в NOW) — **не** делали.
- Не полный privacy-аудит. Не rest call-profile / GigaAM.

## Не делать

- Не kickstart / не `safe_backend_restart` без владельца.
- Не включать `KRAB_STT_EAR_CALL_PROFILE_ENABLED` / `KRAB_SCREENING_AUTO_LANGUAGE_ENABLED`.
- Не merge #2008 без гейта владельца. Не `git add -A`. Не `audit/*`. Не CLAUDE.md целиком.
- Не чекаутить `audit/*`, не #1875.
- Не трогать `service.py` из docs-PR.

## Регламент моделей / режимов (владелец 2026-09-09)

- Координатор чата: **Grok 4.6**, Fast **OFF**. Extra High только на развилку/аудит. **High** на доки/handoff (как этот PR).
- Мелочь / wiring / тест: Composer 2.5 **medium**, Fast **OFF**.
- Карточка Python+pytest: **Grok High**, Fast **OFF**.
- VG `app/main.py` / overlap: **Grok Extra High**, Fast **OFF**.
- Cloud + Multitask: включать когда ≥2 disjoint репо/файла; выключать на один крошечный PR.
- Контекст координатора не забивается диффами воркеров; токены воркеров считаются отдельно.
- Не Fable / Opus Extra High default. Не Fast **ON**.
