# HANDOFF: Main Krab — роутинг моделей + панель :8080 (Q2)

**Дата:** 2026-09-05 17:56 (владелец)  
**Источник:** сессия Krab Ear [`75742213-503f-4612-85a1-49c952ee1004`](75742213-503f-4612-85a1-49c952ee1004)  
**Параллельная сессия Main Krab:** [`038cb35a-36b8-4363-b4ce-d753f0524b6d`](038cb35a-36b8-4363-b4ce-d753f0524b6d) · ветка семейного роутинга [`45793687`](45793687)  
**Статус:** executable brief — **не трогать Krab Ear**

---

## 0. Границы (обязательно)

| Зона | Можно | Нельзя |
|---|---|---|
| **Main Krab** (`/Users/pablito/Antigravity_AGENTS/Краб`) | Роутинг, `ModelManager`, owner panel :8080, тесты | Голый `launchctl kickstart -k` под активной диктовкой Ear |
| **Worktree** `Krab_family_groups_cloud_routing` | Продолжать ветку `codex/family-groups-cloud-routing` @ `4f53400` | Мержить в прод без CI + smoke владельца |
| **Krab Ear** | Только **читать** IPC/lease для holdoff | Любые правки Swift/Python/worktree Ear |
| **Krab Ear dashboard** | — | Порт **8777** — это Ear, не этот бриф |
| **Video Studio** | — | `video_studio_*`, ветка `codex/video-studio-runner` (сосед в полёте) |

Ear **C1 holdoff уже в проде** ([#1999](https://github.com/Pavua/Krab-Ear/pull/1999)): не `lms load` brain на стопе записи, не acquire lease без реальной загрузки. **Краб не должен снова поднимать gemma-26B** на следующем групповом сообщении, пока GPU занят Cursor/диктовкой.

---

## 1. Решение владельца (канон)

1. **Семейный RIS** и **другие чаты, созданные @SergeyRG** — как **личка владельца**: высокие рассуждения, дефолт **`antigravity-cli/gemini-3.8-flash-high`**.
2. **Остальные группы** — дешёвый дефолт **`antigravity-cli/gemini-3.8-flash-low`**, thinking **off/low**; эскалация только по явному «подумай» / `!think` / `!reasoning`.
3. **Панель :8080** (Main Krab owner panel) — UI выбора модели **по назначению** (см. §3), не один глобальный `MODEL`.
4. **GPU contested** (владелец за Mac: Cursor, диктовка Ear): **не** `ensure_model_loaded` / autoload 15+ ГБ. Обычные группы → cloud cheap. **Idle/away** → локальная 15+ ГБ отвечает в группах (и саммари звонков — тот же слот).
5. Вопрос 3 (holdoff) — **да**, как в §5: сигнал + cloud-low, не только смена дефолта.

`cloud_rewriter` в Ear **не включается** (отдельная ось).

---

## 2. Живые ID (проверено READ-ONLY)

| Сущность | ID | Источник |
|---|---|---|
| RIS «главный» | `-1004320211263` | `krab_main.log` 2026-09-01; `chat_model_routing.py` в worktree |
| @SergeyRG (user) | `2234854` | `docs/CHAT_COVERAGE_AUDIT.md`; логи sender=SergeyRG |
| Owner DM (p0lrd) | `312322764` | `chat_model_routing.py` (`OWNER_USER_ID`) |
| Owner panel | `http://127.0.0.1:8080` | `runtime.py` / live prod |
| Ear status dashboard | `http://127.0.0.1:8777` | **не эта задача** |

**Пробел:** в каноне `Краб/src` модуля `chat_model_routing.py` **ещё нет** — есть только worktree `Krab_family_groups_cloud_routing`. Allowlist семьи сейчас = RIS + env `KRAB_FAMILY_CLOUD_CHAT_IDS`. **«Чаты, созданные @SergeyRG»** — расширить: Telegram `getChat` creator / persisted registry / env `KRAB_FAMILY_CLOUD_CHAT_IDS` (owner пополняет из панели).

---

## 3. Таблица роутинга (exact)

Префикс agy-каталога: **`antigravity-cli/`** (не `google/gemini-3-flash-preview` из вестигиального `GEMINI_CHAT_MODEL_GROUP`).

| Purpose key | Когда | Backend | Model ID | Reasoning / effort | `ensure_model_loaded` |
|---|---|---|---|---|---|
| `owner_dm` | ЛС владельца (`312322764`) | cloud | `antigravity-cli/gemini-3.8-flash-high` | **high** (default medium → override) | **never** для local |
| `family_ris` | `chat_id ∈ family allowlist` (мин. `-1004320211263`) | cloud | `antigravity-cli/gemini-3.8-flash-high` | **high** | **never** autoload local |
| `family_sergey_chats` | Группы creator=@SergeyRG (см. §2) | cloud | `antigravity-cli/gemini-3.8-flash-high` | **high** | **never** autoload local |
| `group_default` | Прочие группы (`chat_id < 0`, не family) | см. §5 | см. §5 | **low/off** | см. §5 |
| `group_escalated` | Триггер «подумай» / `!think` / `!reasoning medium\|high` | cloud | `antigravity-cli/gemini-3.8-flash-medium` → при `!think deep` → `high` или `antigravity-cli/gemini-3.1-pro-high` | по триггеру | **never** autoload |
| `idle_local_groups` | Владелец away + GPU free + модель **уже** в RAM | local | `lm-studio-local/gemma-4-26b-a4b-it@4bit` | thinking **off** (`_apply_mlx_disable_thinking`) | только если **already_loaded** |
| `studio_down_fallback` | LM Studio down / local fail | cloud | `antigravity-cli/gemini-3.8-flash-low` | low | **запрещён** `lms load` |

**Запрещено для junk-групп:** наследовать глобальный `MODEL=antigravity-cli/gemini-3.8-flash-high` или первый элемент `MODEL_CLOUD_PRIORITY_LIST` (сейчас тоже high).

**Локальные env (живые, не менять без UI):**

```bash
LOCAL_PREFERRED_MODEL=lm-studio-local/gemma-4-26b-a4b-it@4bit
MODEL=antigravity-cli/gemini-3.8-flash-high          # только owner_dm / pinned primary
RESTORE_PREFERRED_ON_IDLE_UNLOAD=0                  # уже выкл — не включать
KRAB_COGNITION_MODE=depth                           # сейчас жжёт medium на всех — нужен channel override
```

---

## 4. Панель :8080 — требование UI

**Не** Ear 8777. Расширить owner panel (уже есть зачатки):

- `GET /api/models/registry` — `models_admin_router.py`
- `POST /api/model/switch`, `GET /api/model/catalog`, `POST /api/model/apply`
- Per-chat override store: `~/.openclaw/krab_runtime_state/chat_model_overrides.json`

### 4.1. Секция «Модели по назначению»

Минимум **четыре слота** (select + reasoning tier):

| UI label | Purpose key | Default model |
|---|---|---|
| Личка (DM) | `owner_dm` | `antigravity-cli/gemini-3.8-flash-high` |
| Семья / RIS / @SergeyRG | `family_ris` (+ registry Sergey chats) | `antigravity-cli/gemini-3.8-flash-high` |
| Обычные группы (cloud) | `group_default` | `antigravity-cli/gemini-3.8-flash-low` |
| Группы local (idle, уже в RAM) | `idle_local_groups` | `lm-studio-local/gemma-4-26b-a4b-it@4bit` |

Плюс: **эскалация** (`group_escalated`) — medium/high, read-only подсказка про «подумай».

Персист: новый JSON рядом с overrides, напр. `~/.openclaw/krab_runtime_state/model_purpose_map.json` (0600), или расширение существующего settings API — **не** только `.env` (нужен live toggle без рестарта).

### 4.2. Accept criteria UI

- Смена слота → следующий запрос этого класса чата идёт в выбранную модель (smoke: `POST /api/admin/model/test_ping` или лог `routing_decisions.jsonl`).
- RIS `-1004320211263` в smoke не уходит в `casual_chat_low_priority` → local.
- Панель показывает текущий active route + loaded LM models (`GET /api/model/status`).

---

## 5. Holdoff vs Krab Ear (критично)

### 5.1. Симптом

`ModelManager.ensure_model_loaded()` (`openclaw_client.py` ~5600, ~7082) на **каждом** local-route поднимает `LOCAL_PREFERRED_MODEL` (gemma-26B). После ручной выгрузки владельца следующее сообщение в группе снова грузит 15+ ГБ → мешает Cursor + диктовке.

### 5.2. Политика `group_default` при contested GPU

```
IF gpu_contended OR ear_recording OR brain_lease.owner == "krab_ear":
    backend = cloud
    model   = antigravity-cli/gemini-3.8-flash-low
    SKIP ensure_model_loaded entirely
ELIF local_model already in RAM (probe_group_local_ready_without_load):
    backend = local
    model   = lm-studio-local/gemma-4-26b-a4b-it@4bit
ELSE:
    backend = cloud   # ephemeral restricted cloud, NO load
    model   = antigravity-cli/gemini-3.8-flash-low
```

Worktree уже имеет `probe_group_local_ready_without_load` — **использовать**, не дублировать load.

### 5.3. Сигналы `gpu_contended` (приоритет)

1. **Brain lease** `~/.openclaw/lm_studio_brain.lock` owner `krab_ear` — уже читает `self_correction.py` (`brain_lease_ear` gate).
2. **Ear recording** — IPC `get_recording_state` на `~/Library/Application Support/KrabEar/krabear.sock` (или dev `~/.krab_ear_data/backend.sock`); fail-open → считать contested если сокет жив и `recording=true`.
3. **Опционально:** высокий RSS/swap из `coexistence_monitor.log` (грубый proxy, не единственный).
4. **Ручная выгрузка:** timestamp «owner unloaded» в runtime state (кнопка на :8080 «Не грузить local до…») — Ear C1 не трогает Краб, Краб должен уважать.

### 5.4. Анти-паттерны

- Не звать `ensure_model_loaded` для untrusted group, если модель не в RAM.
- Не `acquire_brain_lease("krab")` + load, когда Ear держит lease.
- Не эскалировать junk-группу на `flash-high` при Studio timeout (инцидент RIS `423546c6343c`).

---

## 6. Кодовая база (где править)

### Уже в worktree `codex/family-groups-cloud-routing`

| Файл | Роль |
|---|---|
| `src/core/chat_model_routing.py` | Family allowlist, overrides, `probe_group_local_ready_without_load` |
| `src/userbot/llm_flow.py` | Hook family vs untrusted group |
| `src/core/task_router_bridge.py` | `ChatTrustLevel.OWNER_DM` для family |
| `tests/unit/test_chat_model_routing.py` | Контракт RIS/SergeyRG ids |

### Канон + panel

| Файл | Роль |
|---|---|
| `src/core/routing_policy.py` | `casual_chat_low_priority` → сейчас **local** (корень бага) |
| `src/model_manager.py` | `ensure_model_loaded` — добавить holdoff gate |
| `src/openclaw_client.py` | Call sites ensure_model_loaded |
| `src/core/reasoning_autoscale.py` | `_HIGH_TRIGGERS` («подумай») |
| `src/modules/web_routers/models_admin_router.py` | UI registry + load/unload |
| `src/integrations/cli_subprocess_bypass.py` | Каталог `gemini-3.8-flash-{high,medium,low}` |

### Референс (только читать)

- Ear holdoff spec: `Krab Ear/docs/design-briefs/2026-09-05-horizon-plan.md` §0b–§4
- Ear brain lease: `Krab Ear/KrabEar/backend/brain_lease.py`

---

## 7. План исполнения (порядок)

1. **Worktree** `Krab_family_groups_cloud_routing` — довести family=Sergey chats registry + purpose map в runtime.
2. **`routing_policy` / `task_router`** — junk groups ≠ `flash-high`; family = `owner_dm` trust.
3. **`openclaw_client`** — holdoff перед `ensure_model_loaded`; integrate `probe_group_local_ready_without_load`.
4. **`:8080` UI** — §4 purpose selectors + persist `model_purpose_map.json`.
5. **Tests:** `test_chat_model_routing.py`, `test_model_switch_endpoint.py`, новый `test_ensure_model_loaded_holdoff.py`.
6. **Smoke:** RIS chat dry-run; junk group без load; contested → flash-low only.
7. **PR** в `Krab-openclaw` / `Краб` — **не** cutover prod без владельца (live пока `fb3081a`).

---

## 8. DoD

- [ ] RIS `-1004320211263` → cloud `gemini-3.8-flash-high`, high reasoning, **без** local autoload.
- [ ] Junk group → `gemini-3.8-flash-low` по умолчанию; «подумай» → medium/high.
- [ ] При simulated Ear recording / lease `krab_ear` → **ноль** вызовов `ensure_model_loaded` в логах.
- [ ] :8080 четыре purpose-слота сохраняются и читаются после рестарта.
- [ ] `routing_decisions.jsonl` отражает `reason` (family / group_cheap / holdoff / idle_local).
- [ ] Krab Ear repo **не** изменён.

---

## 9. Соседи и координация

| Агент | Ветка / ID | Не мешать |
|---|---|---|
| **Ear C1** | `feat/brain-holdoff` | Не править `recording_core_service.py`, `brain_lease.py` |
| **VS runner** | `codex/video-studio-runner` | Не трогать video_studio_* |
| **Этот бриф** | `codex/family-groups-cloud-routing` | Owner panel + routing only |

После мержа — короткий отчёт в `Краб/docs/handoff/` или комментарий в PR #family-groups с ссылкой на этот файл.

---

*Coordinated by Krab Ear session 2026-09-05. Ear C1 и Main Krab Q2 — параллельные lane.*
