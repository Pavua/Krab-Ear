# Workflow and tooling notes

> Moved verbatim from `CLAUDE.md` on 2026-11-22 (context-size cleanup, owner-approved). No text was edited or dropped; `CLAUDE.md` keeps a pointer. Original is also in git history.

### CI runner boundary (public Ear / private device gate)

**Исторический контекст 2026-07-24:** repository-level runner `krab-ear-m4max`
в public Ear был введён при account-wide billing block. Это больше не является
приемлемой execution boundary: returning contributor может изменить workflow и
исполнить код на личном Mac, а fork approval не создаёт sandbox.

**Текущая policy после cutover:** public PR/push jobs используют только standard
GitHub-hosted labels из allowlist (`ubuntu-*`, `macos-*`, `windows-*`), без
списков меток, expressions или job-level reusable workflows. `public-runner-
isolation-guard` — regression lint; он не заменяет physical boundary.

Device-specific MLX/Metal gate живёт только в private `Pavua/Krab-CI-Control`
и принимает trusted exact SHA из `codex/krab-ear-v2`. До merge public hosted
workflow public runner обязан быть остановлен, удалён из `Pavua/Krab-Ear`, а
API должен вернуть `total_count=0`. Нельзя снова регистрировать runner в public
repo как recovery shortcut.

Private runner recovery: `scripts/krab_ear_runner_health_check.py` +
`ai.krab.ear.runner-health` проверяют `krab-ear-m4max-private`; source path
будущего service — `~/actions-runner-krab-ci-control`. GITHUB_TOKEN и Telegram
credentials остаются общими владельца и не копируются в controller repository.

### Пульт наблюдения (02.09.2026)

`scripts/status_dashboard.py` — живое состояние экосистемы: прод-процессы,
загрузка машины, движки и задания воркеров ПО ТРЁМ ПРОЕКТАМ, открытые PR,
смерженное за сутки, живые сессии Claude.

```bash
python3 scripts/status_dashboard.py            # текст в терминал
python3 scripts/status_dashboard.py --json     # машинный вид
python3 scripts/status_dashboard.py --serve 8777
```

Постоянная служба: LaunchAgent `ai.krab.ear.dashboard` (RunAtLoad + KeepAlive),
`http://127.0.0.1:8777`. 🔴 Сервер, а не статический файл: данные пересобираются
на КАЖДЫЙ запрос, страница сама обновляется — снимок требовал ручной пересборки
и владелец сразу упёрся в это. Порт **8080 занят панелью главного Краба** из
его release-worktree; трогать оттуда нельзя, ссылка добавляется их стороной.

Воркеры читаются из `<проект>/.remember/tmp/workers/out_*.log`; завершение —
строка `DONE rc=<код>`. 🔴 Класть логи воркеров именно туда, а НЕ в `/tmp`:
перезагрузка машины 02.09 стёрла шесть непрогейченных отчётов. И «ноль байт» ≠
смерть — `cursor-agent` буферизует вывод до конца, поэтому «работает»
определяется по отсутствию маркера `DONE`, а не по размеру файла.

### Sub-agent model selection (cost-conscious)

Используй Agent tool с явным `model` параметром — **default opus сжигает quota** (user установил правило 2026-04-17 после 5h quota hit).

| Model | Use for | % of tasks |
|-------|---------|------------|
| `haiku` | Research, docs, diagnostics, simple edits, memory updates, file reads, grep | ~80% |
| `sonnet` | Implementation PRs, Gemini apply, rebase с conflict resolution, tests, medium refactors | ~18% |
| `opus` | Критический debugging (cascading compiler errors), architectural decisions, когда Sonnet уже failed | ~2% |

Параллелизм > глубина: **многих Haiku параллельно** лучше чем одного Opus linear (5-10× throughput при comparable cost).

### Free-breadth workforce + Claude-gate (🔴 2026-06-17, consolidated Main Krab + Krab Ear)

Claude (Opus/Sonnet) дорогой/быстро кончается → ТОЛЬКО для: брифов, **гейтинга** находок, синтеза, архитектурных решений, единственного критичного HIGH. Весь breadth/исполнение → не-Claude работники. Харнесс: `scripts/draft_audit.py` (fan-out adversarial-аудита одного модуля на free-провайдера; ключи в переменные, НЕ печатать).

**Роутинг в `scripts/draft_audit.py`:** **github** (`gh auth token`) — для security. **hf** (`hf_token` из settings.json (runtime-файл вне репо)) режет security (403), только нейтральные задачи. Free-ключи хранить в `lens_keys.env` (chmod 600, не коммитить). **nvidia** (`NVIDIA_API_KEY`) логирует I/O → строго code-review ONLY, запрещены приватные данные. Реестр воркеров: `~/.openclaw/krab_runtime_state/free_workers.md`. ⟶ [CLAUDE_ARCHIVE §L774]

**🔴 Уроки:** (1) Security-аудиты проводить ТОЛЬКО на `github/mistral/openrouter` (cerebras/groq/hf режут 403-фильтром). (2) Free-модели — только скауты: каждую находку обязательно верифицировать против реального кода (Claude-гейт), никогда не шипить без проверки. (3) Приватные данные (`mlx-whisper`, голос) — ТОЛЬКО провайдерам без обучения на данных. (4) Anti-rebuild: всегда грепать точное имя перед постройкой. ⟶ [CLAUDE_ARCHIVE §L776]

**Гейт (Krab Ear = Python-backend + Swift-agent, НЕ чисто Xcode):** Python → `pytest -p no:cacheprovider` + flake8 CI-cmd (W293 не расслаблен) + ubuntu-parity `scripts/pre_merge_py312_check.sh` + chunk-repro для rest-тестов + `make audit-all` для новых `core/pipeline/`; Swift → `swift build -c release`. CI-страж-поллер УБИВАЮТ → `gh run view <id> --json conclusion` напрямую.

### Gemini 3.1 Pro для дизайна (strict rule)

Визуальный дизайн (цвета, шрифты, layout, themes, design tokens) делается **ТОЛЬКО** через Gemini 3.1 Pro, и **ТОЛЬКО** через `agy` (Antigravity CLI) на оплаченной подписке user **Google AI Pro** — НЕ через `gemini` CLI (free OAuth) и НЕ через прямой API-ключ (старый ключ revoked 2026-04-20).
- **Канал (валидирован 2026-06-08):** `agy` → `/opt/homebrew/Caskroom/antigravity-cli/.../antigravity` (v1.0.6). Модель дизайна — `Gemini 3.1 Pro (High)` (список: `agy models`).
- **Инвокация (агентный кодер — сам читает brief, правит Swift, гоняет `swift build`):**
  ```bash
  agy -p "$(cat docs/design-briefs/<brief>.md)\n\nВЫПОЛНИ это ТЗ..." \
    --model "Gemini 3.1 Pro (High)" --dangerously-skip-permissions \
    --add-dir "$(pwd)" --print-timeout 40m < /dev/null > /tmp/krab-ear-gemini/run.log 2>&1
  ```
  - 🔴 **GOTCHA:** в `run_in_background` обязателен `< /dev/null` — иначе `agy -p` виснет на чтении stdin до EOF (симптом: ELAPSED большой, CPU time ~0, 0 правок). Foreground smoke проходит без него.
  - Квота **за запросы** (не токены) → давать крупные пакетные задачи, 1 brief = 1 запрос.
- **Workflow:** Claude пишет brief в `docs/design-briefs/` (что нельзя ломать + что улучшить) → agy исполняет → **Claude ОБЯЗАТЕЛЬНО ревьюит дифф** (`git diff` grep на `runModal`/`sectionId`/переименования контролов/wiring/хардкод-числа) + сам `swift build -c release` → commit с `Co-Authored-By: Gemini 3.1 Pro (Antigravity)` + bundle-binary parity (`Krab Ear.app/Contents/MacOS/KrabEarAgent` + `native/runtime/KrabEarAgent` + codesign) → push.
- Claude НЕ делает визуал сам. Граница: "стало выглядеть иначе" → agy/Gemini; "стало себя вести иначе" → Claude/Sonnet.
- Behavior код (Auto Layout mechanics, ThemeButton tracking areas, state machines, cross-object проводка) — ОК для Claude. В Privacy-индикаторе (2026-06-08) Claude дал agy точную карту проводки в брифе, но рисование замка оставил agy.

### TCC permissions troubleshooting

**Preferred solution (PR #235)**: use `Krab Ear Dev Local` self-signed identity → TCC grant survives rebuilds. One-time setup:
```bash
scripts/create_local_signing_identity.command   # creates cert in keychain
make sign                                        # rebuilds + signs with stable identity
```
После этого TCC reset после rebuild не нужен — identity hash постоянный.

**🔴 Амендмент 2026-08-01 (живой инцидент): стабильная identity НЕ гарантия.**
После пересборки бинаря запись Accessibility может «протухнуть» даже под
`Krab Ear Dev Local`: `AXIsProcessTrusted()` возвращает `true`, агент логирует
«Глобальный hotkey активирован», но события клавиш НЕ приходят (все
NSEvent-мониторы молчат — и Right Option, и Cmd+Shift+N). Переключение
тумблера в System Settings НЕ лечит (меняет флаг у той же протухшей записи).
Лечение: `tccutil reset Accessibility com.antigravity.krab-ear` + перезапуск
агента (`open` с АБСОЛЮТНЫМ путём к бандлу) + согласие в системном диалоге.
Два смежных правила: (1) NSEvent-мониторы проверяют разрешение В МОМЕНТ
СОЗДАНИЯ — выданное позже разрешение требует перезапуска агента; (2) признак
«агент не даёт строк в agent.log при нажатии» при `AX trusted: true` на
старте = именно этот класс. Диагностика: `~/Library/Application Support/KrabEar/agent.log`
(строки `Accessibility AX trusted at startup:` и `Глобальный hotkey активирован`).
Попутный гоч: codesign бинаря ВНУТРИ .app подписывает весь бандл и падает на
«detritus», если на КОРНЕ `Krab Ear.app` висит `com.apple.FinderInfo` —
снимать `xattr -d com.apple.FinderInfo "Krab Ear.app"` (обычный `xattr -cr`
до корня не добирается, а com.apple.provenance безвреден и неудаляем).

**Fallback / manual reset** (если stable identity не настроена):

macOS TCC (Accessibility, Microphone) кэширует grants по (bundle-id OR absolute path). После rebuild binary с изменённой hash:
- Старые path-based entries в TCC.db остаются но "смотрят" на stale paths.
- Текущий `com.antigravity.krab-ear` bundle ID **может не совпасть** с историей.
- Симптом: user грантит tumбler, app сразу опять запрашивает permission.

**Quick fix**: `scripts/repair_permissions.command` (PR #234) — автоматизирует шаги 1-4 ниже.

**Diagnostic**: `sqlite3 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" "SELECT client, service, auth_value FROM access WHERE client LIKE '%krab%';"`

**Manual fix workflow** (only do когда user asks):
1. `pkill -9 -f KrabEarAgent`
2. `tccutil reset All com.antigravity.krab-ear`
3. `tccutil reset All com.krabear.agent`  # старый ID
4. `tccutil reset Accessibility <absolute-path-to-old-binaries>` для каждого path-based entry
5. User вручную очищает System Settings → Privacy → Accessibility список (удаляет дубликаты), добавляет ОДИН новый `.app`.
6. Re-toggle ON.

### Parallel PR workflow (session 1 proven pattern)

1 session может merge 11+ PRs с этим подходом:
- **File-level isolation** per sub-agent (each agent owns single file/feature), низкий merge conflict.
- **CI parallelization** (3 min backend-tests каждый PR) не блокирует coordinator work.
- **Merge train**: когда 3+ PRs одновременно конфликтуют — rebase всех параллельно через sub-agents, merge sequentially.
- **Research-first for big decisions**: run 3-4 parallel Haiku research agents BEFORE writing implementation plan (Moshi MLX, SeamlessM4T MLX, qwen3-30b benchmarks — каждый ~5 min, results inform plan).

### Dead handler audit methodology (Wave 65 lesson)

When removing dead IPC handlers, a Swift-side grep alone over-counts by ~4×. Full scope required:
1. **Swift callers**: `grep -r "\"method_name\"" native/` — checks HistoryPanel, main.swift, extension files.
2. **Python test dispatch**: `grep -rn "\"method_name\"\|handle_method_name\|assert_dispatch" KrabEar/tests/` — catches test-only callers that verify the handler exists.
3. **Direct Python calls**: `grep -rn "_handle_method_name" KrabEar/` — catches internal calls (cron jobs, batch calls, etc.).
4. Only remove if ALL three are empty. Wave 65 found 177 candidates via Swift-only grep → only 19 confirmed dead after full scope check. Post-Wave 65 batches continued removal, bringing active count from 306 → 296 as of v2.0.3.

### Voice Assistant Mode (Phase 1 CLOSED, 2026-04-18)

Phase 1 foundation completed and shipped (2026-04-18, 45+ PRs merged). Core components: Moshi engine, SeamlessStreaming, ConversationViewController, voice triggers, qwen3-30b routing, E2E tests all live. Reference spec: `docs/superpowers/specs/2026-04-17-voice-assistant-mode-design.md` (330 lines).

Stack:
- **Engines**: Kyutai Moshi 7B (EN) + SeamlessStreaming 2.5B (RU/ES/multilingual, PyTorch+MPS не MLX).
- **Brain**: `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit` via Krab agent OpenClaw.
- **Orchestration**: Voice Gateway `/v1/sessions/{id}/conversation` WS endpoint.
- **UI**: новый tab "Разговор с AI" в Krab Ear `.app` (`ConversationViewController`). Gemini 3.1 Pro design refresh 2026-06-15 (`ConversationViewController+UI.swift` — статус-бейдж, транскрипт-карточка, токены). Live mic level-meter (`ConversationViewController+LevelMeter.swift` — `MicLevelMeterView`, 20 CALayer-баров с ring-buffer, обновляется из `processAudioSamples` RMS на @MainActor ~12.5/сек; idle-reset из `stopAudioCapture`; НЕ трогает `@Sendable` Core-Audio RT-tap).
- **Triggers**: GUI button + Right Option double-tap (300ms) + Silero wake word "Краб".
- **Brain stack**: Krab agent (Telegram userbot) — общая memory + MCP tools + OpenClaw. Voice assistant = "новый channel" в same brain.

Phase 2 (Live Translation), Phase 3 (Call Automation), Phase 4 (STT adapters SenseVoice/Parakeet) — отдельные sub-projects, roadmap в specs/.
