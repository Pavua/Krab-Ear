# Горизонт 2–4 недели — Krab Ear

Дата: 2026-09-05 вечер. Правка владельца **17:41** вложена. Статус: план, не карточка волны. **Код не менять по этому файлу.** Не рестартовать бэкенд. Не включать `memory_conductor_enforce*`. Main Krab — только чтение фактов роутинга.

База: `origin/codex/krab-ear-v2` @ `330bca9b` (#1999). Живые ключи сняты с `~/Library/Application Support/KrabEar/settings.json` и `Краб/.env` / `~/.openclaw/krab_runtime_state/` (только не-секреты).

Уже в этой сессии, **не предлагать заново:** #1997 (сенсор давления + SIGKILL≠mlx.oom, enforce OFF), #1998 (визуал Call Observer / CD / оверлей), **#1999** (C1 brain-holdoff + cloud при Studio down; `cloud_rewriter_enabled` OFF).

Разделение труда: **agy / Gemini 3.1 Pro High** — крупные визуальные пакеты. **Cursor/Grok** — план, гейт диффа, merge, корни. Не жечь xhigh на lint.

---

## 0. Факты, от которых пляшем

### Две разные LM Studio-модели (Ear) + третья у Краба

| Роль | Ключ / env | Живое значение | Кто зовёт |
|---|---|---|---|
| Brain Ear (15+ ГБ, lease/unload) | `llm_brain_model` | `qwen/qwen3.6-27b` | `MemoryConductor`, `RecordingCoreService` unload/preload, OOM-тост |
| Rewriter / summary Ear | `llm_model` | `gigachat3.1-10b-a1.8b-mlx-oq8` | `LLMRewriter`, `ActionItemsExtractor` (`settings.LLM_MODEL`), `summarize_item` |
| Локальный preferred Краба | `LOCAL_PREFERRED_MODEL` | `lm-studio-local/gemma-4-26b-a4b-it@4bit` | `ModelManager.ensure_model_loaded` на каждом local-запросе |
| Cloud primary Краба | `MODEL` + owner panel | `antigravity-cli/gemini-3.8-flash-high` | OpenClaw default / `MODEL_CLOUD_PRIORITY_LIST` первый |
| VA UI Ear | `conversation_brain` | `auto` | Swift-селекторы; **не пишется** в `llm_brain_model` |

Это **не** одна модель. QuickEdit **не** рерайтер и **не** 27B: `QuickEditOverlay.swift` — оверлей правки перед вставкой, таймаут `quick_edit_timeout_sec=5` → вставка исходного STT. Живое: `quick_edit_enabled=True`, `llm_rewrite_enabled=False`. LLM в QuickEdit нет.

`NOW.md` запрещает `gigachat3.1-10b` на длинном русском. Сейчас не бьёт диктовку (rewrite OFF). Ударит `summarize_item`, если Studio ответит 200.

Call Assist summary — VG `/v1/sessions/{id}/summary`, не Ear-brain. Meeting `ITEMS_LLM` — `ActionItemsExtractor` на `LLM_MODEL` (gigachat), не 27B. Встреча берёт lease (TTL 45 с, renew 15 с) — сериализация GPU, не загрузка мозга.

### Lease-контракт (репо Главного Краба не править)

Файл: `~/.openclaw/lm_studio_brain.lock`. Код Ear: `KrabEar/backend/brain_lease.py`. Owner: `"krab_ear"` | `"krab"`. TTL логический. `acquire` чужого неистёкшего → `False`. `release` чужого — no-op. FS error → fail-open `True`.

### Живые флаги памяти (не включать enforce)

```
memory_conductor_enabled=True
memory_conductor_enforce*=все False
mlx_oom_auto_unload_enabled=True          # боевой обход shadow
llm_brain_unload_on_recording=True        # легаси-выгрузка 27B на старте записи
llm_brain_preload_on_stop=False           # автозагрузка 27B после стопа УЖЕ выкл
llm_brain_lease_enabled=True
llm_brain_lease_ttl_sec=30.0
llm_idle_keepalive_enabled=False
rewriter_warmup_on_startup=True           # гейтится rewrite OFF
llm_autoload_timeout_sec=90.0             # таймаут self-heal rewriter
cloud_rewriter_enabled=False              # живое; privacy_mode_enabled=False
cloud_rewriter_provider=openai / gpt-4o-mini
gigaam_idle_unload_sec=3600
quality_profile=max
```

На стороне Краба (чтение `.env`, не менять):

```
MODEL=antigravity-cli/gemini-3.8-flash-high
MODEL_CLOUD_PRIORITY_LIST=flash-high, gemini-3.1-pro-high, claude-sonnet-4-6, google/gemini-2.5-flash
LOCAL_PREFERRED_MODEL=lm-studio-local/gemma-4-26b-a4b-it@4bit
RESTORE_PREFERRED_ON_IDLE_UNLOAD=0
KRAB_REASONING_LEVEL=medium
LM_STUDIO_NATIVE_REASONING_MODE=medium
OPENCLAW_REASONING_EFFORT=medium
KRAB_COGNITION_MODE=depth                 # governor ПРИМЕНЯЕТ depth, не shadow
KRAB_REASONING_AUTOSCALE не задан        # модуль default "0" = выкл; governor всё равно зовёт L0
GEMINI_CHAT_MODEL_GROUP=google/gemini-3-flash-preview   # вестигиальный ключ, живой primary — agy flash-high
```

---

## 0b. Кто автогружает GPU (реальная помеха владельца)

Владелец **уже** выгружает 15+ ГБ вручную, когда замечает во время Cursor/нашей работы. Модель **сама возвращается**. Драка autoload — рычаг, не jetsam `brain`.

### Карта загрузчиков

| Кто | Что грузит | Когда живое | Вердикт |
|---|---|---|---|
| **Krab `ModelManager.ensure_model_loaded`** (`openclaw_client.py` ~5600, ~7082) | запрошенный local id, иначе `LOCAL_PREFERRED_MODEL` = **gemma-26B**; при провале — до 3 fallback-кандидатов | **каждый** local-роут (группы = `casual_chat_low_priority` → local) | **Главный автозагрузчик.** После ручной выгрузки следующее сообщение в группе поднимает gemma (или что Studio отдаст как candidate). Studio down → `local_autoload_failed_switching_to_cloud` → первый cloud = **flash-high** |
| Krab `RESTORE_PREFERRED_ON_IDLE_UNLOAD` | `LOCAL_PREFERRED_MODEL` после idle-unload *чужой* модели | **0 / False** | Не виновник сейчас |
| Ear `load_model_async(llm_brain_model)` | `qwen/qwen3.6-27b` | только стоп записи + `llm_brain_preload_on_stop` | Живое **False** — Ear 27B сейчас **не** грузит |
| Ear `LLMRewriter` self-heal (`llm_rewriter.py` ~798) | `self._model` = **gigachat 10B**, не 27B | HTTP 400 «No models loaded» внутри `rewrite()` | Диктовка: rewrite OFF → не стреляет. Если кто-то включит rewrite / warmup — **украдёт слот** под 10B |
| Ear `summarize()` | POST на `llm_model`, **без** self-heal | `summarize_item` | 400/коннект → extractive fallback, **не** `lms load`. GPU не крадёт. Cloud-пути нет |
| Ear `rewriter_warmup_on_startup` | rewriter | гейт `llm_warmup_needed()` = rewrite ИЛИ punctuation-pass | Оба живые False |
| Ear recording sequence кондуктора | unload 27B + load rewriter | `enforce_recording_sequence` | **False**, не исполняется |
| Ear `ActionItemsExtractor` | POST `LLM_MODEL` (gigachat) | живая встреча ITEMS_LLM | Может JIT-грузить 10B в Studio, не 27B |
| LM Studio JIT | любая id из `/v1/chat/completions` | если клиент назвал выгруженную модель | Вторичный: Краб, назвав `lm-studio-local/…`, провоцирует Studio поднять её |

**Вывод:** Ear сегодня **не** возвращает 27B после ручной выгрузки. Возвращает **Krab `ensure_model_loaded`** (обычно gemma-26B — тот же класс «15+ ГБ на GPU») либо cloud-фоллбек в **flash-high**. Ощущение «мозг сам встал» = local autoload Краба, не `llm_autoload` Ear.

`llm_autoload_timeout_sec` в Ear — только таймаут self-heal **рерайтера**, не флаг «грузи 27B».

### Что Ear обязан НЕ делать (чтобы не красть GPU)

1. **Никогда** `load_model_async` / `load_model_sync` на `llm_brain_model` во время диктовки и после намеренной выгрузки владельца. Preload-on-stop оставить False. Не включать recording-sequence enforce.
2. Self-heal rewriter: если Studio пустой — **не** `lms load`. Либо сырой STT, либо `cloud_rewriter` (privacy побеждает). Не грузить ни 27B, ни gigachat «чтобы починить 400».
3. На стопе записи: **не** `acquire_brain_lease`, если модель не грузим (сейчас acquire идёт *до* проверки preload — 30 с держит lock впустую).
4. Во время записи: не слать в Studio completion, которое Studio JIT-загрузит. Meeting ITEMS_LLM / summarize — либо skip, либо cloud, либо extractive.
5. Выгрузка 27B на старте записи (`llm_brain_unload_on_recording=True`) — **не** чинит автозагрузку: Краб следующим групповым сообщением поднимет gemma. Не включать `enforce_brain` «чтобы выкинуть». Владелец выгружает сам.

Предложение Крабу (только бриф, не патч из Ear): `ensure_model_loaded` пропускать, если (а) идёт запись Ear, (б) владелец только что выгрузил и не просил local, (в) группа уходит в cloud-cheap. Тогда GPU остаётся свободным под Cursor.

---

## 0c. Cloud, когда LM Studio нет

Политика владельца: Studio недоступен → **в облако**. `privacy_mode` всегда побеждает.

### Ear `cloud_rewriter` — жив ли на QuickEdit / summary?

| Путь | Cloud? | Живое |
|---|---|---|
| Диктовка rewrite (`engine.py` ~1785) | Да, **только после провала локального rewrite**, и только если `_cloud_rewrite_allowed()` = `cloud_rewriter_enabled` AND NOT privacy | rewrite OFF + cloud OFF → **мёртвый**. Даже включённый cloud не сработает, пока rewrite не пойдёт в local-fail |
| QuickEdit | Нет LLM вообще | Таймаут → сырой STT |
| `summarize_item` / `summarize()` | Нет. 400 → extractive, без `cloud_rewrite` | Cloud не подключён |
| Call Assist / VG summary | Свой роутер VG | Не Ear |
| Meeting action items | Local `LLM_MODEL` POST | Нет cloud |

Чтобы «Studio down → cloud» заработало в Ear: (1) включить `cloud_rewriter_enabled` (opt-in, privacy wins), (2) добавить cloud-ветку в `handle_summarize_item` / `_generate_summary` после local fail, (3) для диктовки — либо оставить rewrite OFF (сырой STT, GPU свободен), либо пускать cloud **без** предварительного `lms load`. Сейчас флаг выключен — это развилка §4.

### Группы Главного Краба (чтение, не править)

Матрица `routing_policy.py`: отрицательный `chat_id` → `casual_chat_low_priority` → **local**. Owner DM → cloud. Studio down / `ensure_model_loaded` fail → cloud-кандидат из `MODEL_CLOUD_PRIORITY_LIST` → **flash-high**.

`KRAB_COGNITION_MODE=depth` живой. `channel_overrides` в коде: voice=fast, swarm=deep, cron=standard. **Группы нет.** L0 default = **medium** → тир `standard` → модель primary (flash-high) + effort env **medium**. `!think` / «подумай» в `_HIGH_TRIGGERS` → high.

Worktree `Krab_family_groups_cloud_routing` (не прод-main): семейный RIS allowlist как owner-DM cloud; обычные группы — local gemma. Инцидент 05.09: RIS ушёл untrusted → Gemma → timeout → `force_cloud` + полный манифест. В канон `Краб/src` модуля `chat_model_routing.py` **нет**.

---

## 1. Горизонт: три дорожки

Нет назначенной волны в `NOW.md`. Номера W* ниже — **не** живые карточки, пока координатор не положит план в `docs/superpowers/plans/`.

### Дорожка A — Стабильность (Cursor)

| Неделя | Корень | Зачем сейчас |
|---|---|---|
| ~~1~~ | ~~Autoload-off + lease-баги Ear (C1, §4)~~ **#1999 в проде** | Следующий корень — Telnyx UI strip |
| 1 | Вырезать мёртвый Telnyx UI | Адаптеры удалены #1990; CD всё ещё пишет `telnyx_api_key` |
| 1–2 | Таймаут `mlx_lock` на интерактивном пути | W8-бюджеты (#1956) в проде; корень — `with mlx_lock()` без timeout в `_transcribe_model` (~3151). Ветка `feat/stt-timeout-budgets` устарела. Не мержить |
| 3+ | HealthMonitor `timeoutSec: 2` | После памяти. Sticky-hang уже в проде. Ложный hang под flock истории — не первый |
| позже | Схема пустых Swift-флагов | `smart_field_format_enabled` / `streaming_paste_enabled`: код есть, в `DEFAULT_SETTINGS` нет |
| owner | GigaAM `confidence=0.9` (#1985) | Ретрай по уверенности для RU мёртв |

Не трогать: P0 turbo REST, C2/C3 meeting/capture, второй EventBridge, `REST_IN_PROCESS_ENABLED`.

### Дорожка B — Сосуществование GPU (Cursor Ear + бриф Крабу)

Цель: владелец выгрузил большой слот — **никто не поднимает его обратно** во время диктовки и Cursor. 27B (`qwen/qwen3.6-27b`) **никогда** не jetsam'ить кондуктором. Рычаг: autoload-off + cloud-cheap. Карточка Краба — отдельным брифом координатору, не патч из этого репо.

### Дорожка C — Визуал (agy, жирные брифы)

Квота Gemini 3.1 Pro High — пакетами. Мелочь не отдавать. Гейт диффа — Cursor.

---

## 2. Очередь Antigravity (2–3 объёмных брифа)

Готовить **после** соответствующего Cursor-среза.

### Бриф 1 — Глоссарий «Все настройки» (259 ключей)

**Когда:** сразу (не ждёт Telnyx). **Сначала глоссарий, потом перевод.**

- **Что:** IPC-ключ → короткий RU-лейбл + группа. CD и Gemini: лейбл primary, ключ caption. Поиск по ключу и лейблу. Секреты как сейчас.
- **Файлы:** `HistoryPanelController+AllSettings.swift`; опционально `docs/settings-glossary-ru.md`.
- **Нельзя:** менять ключи IPC, `toPayload`, маскирование, `set_settings`, бэкенд, `sectionId`.

### Бриф 2 — «Автозвонки» после вырезания Telnyx (VG-native)

**Когда:** после мержа Cursor-strip Telnyx.

- **Что:** секция и вкладка как клиент Voice Gateway. Убрать copy «Настройте Telnyx API key». Паритет CD/Gemini.
- **Нельзя:** новый IPC, новый провайдер, `runModal`, VG `.env`, полировать Call Observer.

### Бриф 3 — Разговор + селекторы мозга из живого каталога

**Когда:** после дорожки B (чтобы UI не предлагал «держать 27B как рерайтер»).

- **Что:** живой `list_llm_models` вместо хардкода; выкинуть `llama-3.2-3b`. Ритм CD.
- **Нельзя:** выдумывать IPC; писать только в UserDefaults. В `llm_brain_model` — только если политика §4 уже зафиксирована.

Мелочь не в отдельный High-бриф: оверлей после #1998 — `setAudioLevel` масштабирует `recordingDotHalo`, pulse анимирует opacity того же слоя; `effectView.masksToBounds` режет тень.

---

## 3. Очередь Cursor (корни)

Карточка — в `docs/superpowers/plans/` + баны playbook. Worktree от `origin/codex/krab-ear-v2`.

### C1. Autoload-off + не красть GPU — **ЗАДЕПЛОЕНО #1999**

Не «включить enforce». Не выгружать `brain` кондуктором. `cloud_rewriter_enabled` не включали.

Ear-срез (этот репо):

1. Стоп записи: `acquire_brain_lease` только если реально грузим модель. Preload оставить False.
2. Self-heal `rewrite()`: при пустом Studio **не** `load_model_sync`. Fail → сырой текст или cloud (если флаг и не privacy).
3. Не добавлять новых `load_model_*` на `llm_brain_model`.
4. OOM-shadow: снять `mlx_oom_auto_unload_enabled` с brain (не выкидывать 27B по ложному oom).
5. Схема настроек: ключи brain + `cloud_rewriter_*` в `DEFAULT_SETTINGS` / validator (включение флага — отдельное «да», C1 его не включает).

Бриф Крабу (координатор, не этот чекаут):

1. Группы: не наследовать `MODEL=gemini-3.8-flash-high`. Дефолт — §4c.
2. `ensure_model_loaded`: не звать во время записи Ear и после ручной выгрузки, если роут не «явный local».
3. Cloud при Studio down — **flash-low**, не первый элемент priority list (flash-high).
4. `channel_overrides.group = fast` (effort low / thinking off). Эскалация — фраза/команда.
5. **Никогда** не просить Ear `enforce_brain`.

### C2. Telnyx UI strip

Мёртвые поля в `cdBuildCallAutomationSection`. Слайдеры duration/cost/silence — живые, не вырезать. `call_provider` по-прежнему допускает `telnyx`.

### C3. Интерактивный таймаут `mlx_lock`

В проде: `stt_budget.py`, #1956. Не закрыто: `with mlx_lock()` на инференсе. Не rebase `feat/stt-timeout-budgets`.

### C4. QuickEdit — не путать с 27B / cloud

QuickEdit без LLM. Cloud rewriter его не кормит. Если 5 с мало — степпер `quick_edit_timeout_sec`. Rewrite снова включать — не GigaChat 10B и не 27B.

### C5. Схема настроек (дешево)

`DEFAULT_SETTINGS` + validator: `llm_brain_*`, `smart_field_format_enabled`, `streaming_paste_enabled`. Не включает фичи.

### C6. HealthMonitor 2.00 с — позже

Не чинить sticky-hang заново.

### Не делать

- Мержить `feat/stt-timeout-budgets`.
- Включать любой `memory_conductor_enforce_*`.
- `semantic_search_enabled` / SenseVoice / Voxtral на этой машине.
- `history_encryption_enabled`.
- Править Main Krab из этой сессии.

---

## 4. Политика brain / autoload / cloud

### Инвариант (владелец 17:41)

- **Никогда** не auto-evict `brain` (`qwen/qwen3.6-27b`) как политика сосуществования. Кондуктор не jetsam'ит чат-модель Краба.
- Рычаг: **autoload-off** + **cloud fallback**. Владелец выгрузил — слот остаётся пустым, пока он сам не загрузит или явно не попросит local.
- Диктовка и Cursor не должны снова увидеть 15+ ГБ, которые только что сняли.
- Studio нет → облако (дешёвое в группах). `privacy_mode` в Ear всегда побеждает.

### Баги Ear относительно инварианта (не «выгрузи 27B»)

1. **Ложный acquire lease на стопе.** `recording_core_service.py` ~2837: `acquire_brain_lease("krab_ear")` до `llm_brain_preload_on_stop`. Preload False → 30 с lock без модели. Краб не может acquire. Это holding, не eviction.
2. **Self-heal rewriter** готов украсть GPU под gigachat, если rewrite/warmup включат при пустом Studio. Сейчас rewrite OFF — мина на будущее.
3. **`would evict brain` в shadow** во время диктовки (C-INFLIGHT: `is_recording` False до финального STT; TTL 30 с → пустой lease). Пока enforce OFF — только лог. **Не** включать enforce, чтобы «починить» лог.
4. **OOM-путь в shadow всё ещё целится в brain** (`mlx_oom_auto_unload_enabled=True`). Это выгрузка, не загрузка — против «never evict brain». Сузить executor: rewriter/whisper, не `llm_brain_model`.
5. **Выгрузка на старте записи без lease-гейта** (~1365). Владелец и так выгружает вручную. Оставить как есть или гейтить `krab` — не главный рычаг. Главное — не грузить обратно.
6. Recording sequence — не включать.

### Что включать можно / нельзя

| Флаг | Вердикт |
|---|---|
| `memory_conductor_enforce_brain` | **Никогда** |
| `memory_conductor_enforce_recording_sequence` | **Нет** (unload 27B + load rewriter) |
| `memory_conductor_enforce` (глобальный) | **Нет** |
| `llm_brain_preload_on_stop` | Оставить **False** |
| `llm_brain_lease_enabled` | True. На стопе — acquire только если грузим |
| `mlx_oom_auto_unload_enabled` | Снять с brain |
| `llm_brain_unload_on_recording` | Не обязательно выключать; не лечит autoload Краба |
| `cloud_rewriter_enabled` | Развилка §4d. Сейчас False. Privacy всегда wins |
| `memory_conductor_enforce_rewriter` | После shadow, только если rewrite на маленькой модели. Self-heal без `lms load` |
| `memory_conductor_enforce_gigaam` / `whisper` | Осторожно / REST-reaper |

Целевой порядок под давлением (если когда-нибудь enforce по жителям): gigaam idle → whisper REST idle → rewriter. Brain — только ручная кнопка тоста и только если lease не `krab`.

### 4c. Конкретный стек групповых чатов (предложение Крабу)

Живые id из экосистемы (agy catalog + `.env` + `cli_subprocess_bypass.py`):

| Слот | ID | Зачем |
|---|---|---|
| **Дефолт группы** | `antigravity-cli/gemini-3.8-flash-low` | Дешёвый flash, не High. Thinking / effort **off или low** |
| Эскалация 1 (явная) | `antigravity-cli/gemini-3.8-flash-medium` | «подумай», «разбери», `!think deep`, `!reasoning medium` |
| Эскалация 2 (явная) | `antigravity-cli/gemini-3.8-flash-high` или `antigravity-cli/gemini-3.1-pro-high` | Только по команде владельца; Pro — дизайн/сложный разбор, не junk |
| Local, если Studio уже держит лёгкую и владелец **не** выгружал | `lm-studio-local/gemma-4-26b-a4b-it@4bit` + `enable_thinking=false` (уже есть `_apply_mlx_disable_thinking`) | Не вызывать `ensure_model_loaded`, если слот пустой после ручной выгрузки |
| Тяжёлый local | `qwen/qwen3.6-27b` | **Не** дефолт групп. Не autoload. Не Ear-preload |

Правила:

- Junk/обычная группа **не** наследует `MODEL=…flash-high` и не берёт первый элемент `MODEL_CLOUD_PRIORITY_LIST`.
- `channel_overrides.group = fast` (в коде сейчас нет ключа `group`; L0 default medium + `KRAB_REASONING_LEVEL=medium` — вот почему жжётся квота).
- Эскалация только по cue: «подумай» / «проанализируй» / `!think deep` / `!reasoning high`. Короткие «ок/лол/+» остаются flash-low, thinking off.
- Studio down → тот же flash-low, не flash-high и не `lms load` 27B/gemma.
- Owner DM может остаться на pinned primary — это не группа.

`GEMINI_CHAT_MODEL_GROUP=google/gemini-3-flash-preview` в `.env` — старый Google-id; живой каталог agy — `antigravity-cli/gemini-3.8-flash-*`. Предлагать agy-id.

### 4d. Вопросы владельцу (только реальные развилки)

1. **Ear cloud_rewriter.** Сейчас OFF. Studio down на `summarize_item` уже даёт extractive, без GPU. Включать cloud для summary / (если снова будет) rewrite диктовки? Privacy по-прежнему блокирует. Альтернатива: оставить OFF — сырой STT + extractive, GPU свободен. QuickEdit облака не получит в любом случае.
2. **Семейный RIS + чаты @SergeyRG** — **РЕШЕНО 17:56:** как личка, `antigravity-cli/gemini-3.8-flash-high`, high reasoning. **Q2 передан** параллельной сессии Main Krab → [`ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md`](../ANTIGRAVITY_HANDOFF/2026-09-05-krab-8080-model-routing.md) (панель :8080 + purpose map). Ear эту ось не реализует.
3. **Общий holdoff с Крабом** — **РЕШЕНО 17:56:** оба рычага (cloud-low под contested + не `ensure_model_loaded`). Ear C1 в проде (#1999); Краб — в handoff §5.

Не спрашивать: включать ли `enforce_brain` — нет. Выгружать ли 27B на каждой диктовке — владелец уже выгружает сам; рычаг — не грузить обратно.

---

## 5. Не делать (баны `NOW.md` + playbook)

- Не чекаутить `audit/*`, не мержить PR #1875 (`krab_ru` hard-negatives).
- Не строить заново C2 Live Meeting / C3 Quick Capture.
- Не «чинить» HealthMonitor sticky-hang.
- Не включать `REST_IN_PROCESS_ENABLED`.
- Не запускать собранный `KrabEarAgent` из воркера. Не голый `launchctl kickstart -k` под запись.
- Не `git add -A`. Не коммитить `wake_word_models/hard_negatives_raw/tts_phrases.json`.
- Не удалять remote `audit/*` пачкой. Не трогать Main Krab runtime и VG `.env`.
- Не второй EventBridge. Не wake word на SSE. Не дообучать `krab_ru`.
- База только `origin/codex/krab-ear-v2`. Визуал Swift — только agy + Gemini 3.1 Pro High.
- Не включать `memory_conductor_enforce*` с этого документа.
- Не мержить устаревшую `feat/stt-timeout-budgets`.
- Не реализовывать этот план в том же ходе, что написание.

---

## Приложение: W8 и NOW drift

| Утверждение NOW | Факт на 2026-09-05 |
|---|---|
| W8 бюджеты «в работе», ветка `feat/stt-timeout-budgets` | #1956 смержен 27.08; `stt_budget.py` в HEAD |
| Корень зависания не закрыт бюджетами | Верно: `mlx_lock()` без timeout на инференсе |
| `llm_brain_model` пустой / нет UI | JSON заполнен `qwen/qwen3.6-27b`; UI селектора нет |
| Три флага «пустые» | Swift-реализации paste есть; бэкенд-схемы нет |
| (новое) Ear `llm_autoload` поднимает 27B | Нет. Self-heal грузит `llm_model` (gigachat) и сейчас не жив. Автозагрузка 15+ ГБ — Krab `ensure_model_loaded` → gemma-26B |

`NOW.md` этим файлом не обновлять. Drift — крошечный коммит координатора, не волна.
