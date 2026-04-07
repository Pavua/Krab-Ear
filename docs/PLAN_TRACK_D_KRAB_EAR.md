# Track D — Krab Ear (STT + diarization сервис :5005)

> **Project:** Krab Ear (`/Users/pablito/Antigravity_AGENTS/Krab Ear`)
> **Master plan:** `~/.claude/plans/parallel-enchanting-chipmunk.md` (Plan ID `parallel-enchanting-chipmunk`)
> **Created:** 2026-04-08
> **Scope:** Diarization activation + Repo cleanup + Integration wiring с main Krab
> **Self-contained:** да, можно работать без оглядки на Track B / Track C

---

## Что в этом файле

Только секции трека D (Krab Ear) из master plan. Если тебе нужна информация про:

- **Backup, repo hygiene, integration contract, conflict-avoidance, observability** → master plan, разделы A.1–A.5
- **Main Krab (translator routing, Mercadona)** → `Краб/docs/PLAN_TRACK_B_MAIN_KRAB.md`
- **Voice Gateway (refactor, auto-summary, iOS)** → `Krab Voice Gateway/docs/PLAN_TRACK_C_VOICE_GATEWAY.md`
- **Cross-cutting (USER2/USER3, .gitignore, memory discipline)** → master plan, раздел E

Master plan меняется при изменении shared foundation. Этот файл — при изменении трека D.

---

## Состояние трека на 08.04.2026

| Параметр | Значение |
|----------|----------|
| Phase | E4 → E5 |
| Готовность | E4 done, диаризация implemented но не активирована |
| Открытые блокеры | **HF_TOKEN не задан**, **GitHub repo `Pavua/Krab-Ear` не существует (404)**, integration с main Krab use 1 из 40 IPC команд |
| Тесты | 14 тест файлов (mlx + pyannote + e2e voice loop) |
| Runtime | LaunchAgent `ai.krab.ear.rest`, port 5005 |

**КРИТИЧНО (перед началом любой работы):**
1. Создать GitHub repo `Pavua/Krab-Ear` и запушить 12 локальных коммитов (см. master A.1 шаг 1). Если диск умрёт — потеряется вся E4 работа.
2. Удалить или архивировать `Krab Ear copy/` (uninitialized дубликат от 12 февраля, см. master A.1 шаг 4).

---

## D.1. Diarization Activation (1 сессия, ~30 мин чистого времени)

**Цель:** активировать pyannote/speaker-diarization-3.1 которая уже implemented но недоступна без HF_TOKEN.

### Шаги

**1. Получить HF_TOKEN**
- https://huggingface.co/settings/tokens → создать read-only token
- Принять license для `pyannote/speaker-diarization-3.1`

**2. Установить env var**
```bash
# Persistent через .env
echo "KRAB_EAR_HF_TOKEN=hf_xxxxxxxxxxxx" >> "/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/.env"
```
⚠️ **`.env` НЕ должен попасть в git** — добавить в `.gitignore` (см. D.2).

**3. Pre-download модели** (опционально, ускорит первое использование)
```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
.venv_krab_ear/bin/python -c "
from pyannote.audio import Pipeline
import os
p = Pipeline.from_pretrained('pyannote/speaker-diarization-3.1', token=os.environ['HF_TOKEN'])
print('Model loaded:', p)
"
```

**4. Перезапустить Krab Ear LaunchAgent**
```bash
launchctl unload ~/Library/LaunchAgents/ai.krab.ear.rest.plist
launchctl load ~/Library/LaunchAgents/ai.krab.ear.rest.plist
```

**5. Verify**
```bash
curl http://127.0.0.1:5005/v1/readiness | jq '.diarization'
# Ожидаем: {"has_token": true, "model_cached": true, "ready": true}
```

**6. Smoke test**
```bash
curl -F "file=@test_audio.wav" -F "diarize=true" http://127.0.0.1:5005/v1/stt/transcribe | jq '.segments[].speaker'
# Ожидаем: ["SPEAKER_00", "SPEAKER_01", ...]
```

### Файлы

| Действие | Путь |
|----------|------|
| Создать | `KrabEar/.env` (gitignored) |
| Изменить (опционально) | `~/Library/LaunchAgents/ai.krab.ear.rest.plist` если нужен env var (или через `.env` достаточно) |

---

## D.2. Repo Cleanup (~20 мин)

После master A.1 (Phase 0 backup), нужно прибраться в Krab Ear.

### Действия

**1. `.gitignore`** (создать или дополнить):
```
.venv_krab_ear/
.venv_krab_ear_clean/
venv_clean/
.env
.env.*
server_log.txt
test_*.wav
data/
.ralphy/
.claude/
.remember/
tests/benchmark_results.json
```

**2. Решить судьбу `poc_diarization/`** (отдельный sub-git):
- Если код уже мигрирован в `KrabEar/core/engine.py` → удалить или архивировать
- Если ещё нужен → оставить, но переименовать в `_ARCHIVE_poc_diarization`

**3. `KrabEar/scripts/`** и **`KrabEar/tests/golden_dataset/`** — добавить в git (это полезные артефакты, не junk)
```bash
git add KrabEar/scripts/ KrabEar/tests/golden_dataset/
git commit -m "chore: track scripts and golden dataset"
```

**4. Закоммитить и запушить** (при условии что master A.1 выполнен и origin живой):
```bash
git add .gitignore
git commit -m "chore: gitignore venvs, runtime artifacts, agent state"
git push origin main
```

---

## D.3. Integration с main Krab — расширить krab_ear_client (1-2 сессии)

**Текущий gap:** Krab Ear экспонирует 40 IPC команд, main Krab вызывает только `ping`.

### Phase D.3.A — basic transcription wiring (приоритет)

В `src/integrations/krab_ear_client.py` (main Krab — это в **другом** репо!) добавить методы:

```python
async def transcribe(self, audio_path: str, *, lang_hint: str = "ru", diarize: bool = False) -> dict:
    """POST /v1/stt/transcribe → {text, confidence, segments?}"""

async def get_capabilities(self) -> dict:
    """IPC get_capabilities → ready/cached state"""

async def get_history_page(self, page: int = 0, limit: int = 50) -> dict:
    """IPC get_history_page → recent transcriptions"""
```

Затем в `src/userbot_bridge.py` (main Krab) voice handler:
- Вместо локального fallback STT → `krab_ear_client.transcribe(...)` если Krab Ear ready
- Сохранить fallback на mlx-whisper subprocess для случая когда Krab Ear не запущен

### Phase D.3.B — call assist wiring (если время есть)

`!call_assist start` команда в Telegram → `krab_ear_client.start_call_assist(session_id, gateway_url='http://127.0.0.1:8090')`. Это связывает Krab Ear ↔ Voice Gateway через main Krab контроллер.

### Файлы (в **main Krab** репо!)

| Действие | Путь |
|----------|------|
| Изменить | `/Users/pablito/Antigravity_AGENTS/Краб/src/integrations/krab_ear_client.py` (расширить, +100 LOC) |
| Изменить | `/Users/pablito/Antigravity_AGENTS/Краб/src/userbot_bridge.py` (voice handler, ~5 LOC) |
| Создать/расширить | `/Users/pablito/Antigravity_AGENTS/Краб/tests/unit/test_krab_ear_client.py` |

⚠️ **Важно:** D.3 правит файлы в main Krab репо, но логически принадлежит треку D потому что это про использование IPC API Krab Ear. Когда работаешь над D.3, открой main Krab репо в Code mode но держи `PLAN_TRACK_D_KRAB_EAR.md` под рукой.

---

## D.4. Phase 5+ exposed commands (опционально)

Если хочется довести до 100% — использовать остальные 35 неиспользуемых IPC команд:

- `set_settings` / `get_settings` — owner panel в `:8080` для конфигурации Krab Ear
- `set_translation_glossary_item` — управление словарём через Telegram команду
- `summarize_text` — для длинных голосовых → краткий summary в Telegram

Это уже Phase 5+ полировка, не критично.

### Полный список IPC команд (для справки)

**Recording:** ping, start_recording, stop_recording, get_recording_state
**History:** get_history_page, search_history, delete_history_item, add_history_item, compact_history, import_history_ndjson, get_history_stats, get_history_overview
**Settings:** get_settings, set_settings, set_translation_glossary_item, remove_translation_glossary_item
**Translation:** translate_text
**Transcription:** transcribe_paths, preview_transcribe_paths
**Capabilities:** get_capabilities, get_readiness
**Call Assist:** start_call_assist, stop_call_assist, get_call_assist_state, call_assist_diagnostics, call_assist_summary, call_assist_quick_phrase, list_call_assist_quick_phrases, call_assist_cost_estimate, call_assist_timeline, call_assist_timeline_stats, call_assist_timeline_summary, call_assist_timeline_export, call_assist_timeline_clear, call_assist_timeline_to_history
**I/O:** list_audio_inputs, set_paste_status
**Misc:** summarize_text

(Полный контракт см. master plan A.2.)

---

## D.5. Critical files map

```
/Users/pablito/Antigravity_AGENTS/Krab Ear/
├── KrabEar/
│   ├── main.py (entry → backend.service.main)
│   ├── backend/
│   │   ├── service.py (BackendService, IPC dispatch ~line 88, 40 commands)
│   │   ├── rest_server.py (Flask :5005, 7 endpoints)
│   │   ├── vg_ws_client.py (E4 — VG WebSocket client, done)
│   │   └── event_bus.py (in-process pub/sub)
│   ├── core/
│   │   ├── engine.py (mlx-whisper + pyannote, line 383-409 = diarization init)
│   │   ├── config.py (KRAB_EAR_* settings, ~20 keys)
│   │   └── audio_engine.py
│   ├── contracts/
│   │   ├── registry.py (event types: SttFinal, TranslationCompleted, ...)
│   │   └── envelope.py (event envelope schema)
│   └── tests/ (14 files, mlx + pyannote + e2e voice loop)
├── docs/ (ROADMAP_ECOSYSTEM, ARCHITECTURE)
└── ~/Library/LaunchAgents/ai.krab.ear.rest.plist
```

---

## D.6. Verification Track D

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
.venv_krab_ear/bin/pytest tests/ -q

# Diarization
curl http://127.0.0.1:5005/v1/readiness | jq '.diarization.ready'

# Integration smoke
curl -F "file=@test_audio.wav" http://127.0.0.1:5005/v1/stt/transcribe | jq '.text'
```

---

## Зависимости от других треков

- **Track B (Main Krab)** — D.3 правит файлы в main Krab репо. Координация: когда работаешь над D.3, не запускай параллельно сессию в треке B на тех же файлах (`src/integrations/krab_ear_client.py`, `src/userbot_bridge.py`). Используй разные ветки: `feat/ear-d3-integration` vs `feat/main-krab-translator`.
- **Track C (Voice Gateway)** — Krab Ear E4 уже умеет коннектиться к VG через WebSocket (`KrabEar/backend/vg_ws_client.py`). Если в треке C меняется WS API — нужно обновить и D. Но это маловероятно: WS API стабилен.

## Conflict-avoidance (memo)

- Не править `~/.openclaw/openclaw.json` (это main Krab state, не Krab Ear)
- Не запускать 2 экземпляра Krab Ear LaunchAgent одновременно
- D.3 = в main Krab репо, своя ветка `feat/ear-d3-integration`
- D.1, D.2, D.4 = в Krab Ear репо, свои ветки `feat/ear-diarization`, `feat/ear-cleanup`

См. master plan A.5 для полного списка правил.

---

## End of Track D
