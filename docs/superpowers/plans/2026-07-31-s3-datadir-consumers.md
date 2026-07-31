# S3 / Задача 1 — журнал: кто читает `settings.DATA_DIR` и что изменится

Контекст: `KrabEar/launchagents/ai.krab.ear.backend.plist.template` до этой
правки не нёс `KRAB_EAR_DATA_DIR` в `EnvironmentVariables` — переменную
читает `core/config.py::Settings.DATA_DIR` (Pydantic-Settings, дефолт
`~/.krab_ear_data` при отсутствии env var). Аргумент `--data-dir` доходит
только до `StateStore` через локальную переменную `main()` — эти два канала
раньше случайно совпадали, потому что переменная жила только в
rest-плисте (отдельный юнит с 16-07). После слияния in-process REST под
backend-плист (волна M2) переменная обязана быть здесь же — иначе половина
процесса читает `~/.krab_ear_data`, а история пишется в канонический
`~/Library/Application Support/KrabEar`.

Правка: `KRAB_EAR_DATA_DIR` добавлен в `EnvironmentVariables` backend-плиста,
той же строкой, что и `--data-dir` в `ProgramArguments` (побайтовое равенство
проверяется `KrabEar/tests/test_backend_plist_data_dir_parity_S3.py`).

Полный список потребителей `settings.DATA_DIR` (спека
`docs/superpowers/specs/2026-07-31-s3-rest-flip-design.md`, раздел Р3) — семь
строк в пяти файлах:

| Файл:строка | Что читал ДО правки | Что читает ПОСЛЕ правки | Ожидаемый эффект |
|---|---|---|---|
| `rest_server.py:713` (`store = StateStore(settings.DATA_DIR)`) | `~/.krab_ear_data` (REST жил отдельным юнитом со своей переменной — совпадение было только у живого REST-юнита с 16-07; у backend-плиста переменной не было вовсе) | канонический `~/Library/Application Support/KrabEar` | module-level фантомный store, указывавший не туда где backend слитого процесса ждёт данные, исчезает — один `StateStore` на весь слитый процесс |
| `rest_server.py:783-784` (`TEMP_DIR = settings.DATA_DIR / "temp_uploads"`, `TEMP_DIR.mkdir(...)`) | подкаталог `~/.krab_ear_data/temp_uploads` | подкаталог канонического каталога | загрузки REST (upload) идут туда же, где остальные runtime-данные; каталог теперь физически виден в privacy-purge аудите (см. Задачу 3 волны) |
| `rest_server.py:413` (`RestAuth(data_dir=str(settings.DATA_DIR))`) | Bearer-токен в `~/.krab_ear_data` | Bearer-токен в каноническом каталоге | если владелец настраивал `REST_API_AUTH_ENABLED` на живом REST-юните (у него переменная уже была), токен находится по тому же пути — паритета не нарушает; для НОВОЙ установки токен теперь создаётся/читается там же, где остальные секреты |
| `rest_server.py:481` (`read_bridge_token(settings.DATA_DIR)`) | `~/.krab_ear_data/event_bridge_token` | канонический `.../event_bridge_token` | `EventBridge` (IPC→REST мост) начинает находить токен, который backend реально пишет — до этой правки мост в НЕ-launchd (dev/standalone) сценариях мог смотреть не туда |
| `cloud_stt.py:25` (`store = StateStore(settings.DATA_DIR)`) | `~/.krab_ear_data/settings.json` — **файл сегодня не существует** (проверено при разведке спеки), т.е. модуль читал пустоту → ключи облачных STT-провайдеров всегда пустые, cloud STT фактически недостижим на проде | канонический `settings.json` — **впервые читает реальные значения** (`openai_api_key`/`deepgram_api_key`/`assemblyai_api_key`, если владелец их когда-либо задавал через `set_settings`) | **смена семантики**: если ключи облачных STT-провайдеров были заданы через IPC (`set_settings`) — они впервые становятся видны cloud_stt.py. Приватностный гейт для эту модуля отдельный от `_cloud_rewrite_allowed()` (см. ниже) — не трогается этой задачей |
| `cloud_rewriter.py:141` (`store = StateStore(settings.DATA_DIR)`) | тот же несуществующий `~/.krab_ear_data/settings.json` — облачный rewriter фактически недостижим | канонический `settings.json` — **впервые читает `cloud_rewriter_enabled`/`cloud_rewriter_provider`/`cloud_rewriter_api_key`** | **смена семантики**: если владелец когда-либо включал `cloud_rewriter_enabled` через IPC, эта настройка впервые вступает в силу. `engine._cloud_rewrite_allowed()` (core/engine.py:541) — единственный гейт приватности для облачного rewrite (privacy_mode_enabled ВСЕГДА выигрывает, транскрипт не покидает устройство в privacy-режиме) — этой задачей НЕ изменяется и НЕ обходится; смена канала DATA_DIR не даёт cloud_rewriter новых прав, только позволяет ЕГО СОБСТВЕННЫМ настройкам впервые быть прочитанными |
| `startup_diagnostics.py:279,337,624` (`Path(settings.DATA_DIR)`, свободное место на диске + путь сокета `backend.sock`) | проверки диска/сокета указывали на `~/.krab_ear_data` | проверки указывают на канонический каталог, где реально лежат история/модели/сокет | `get_diagnostics`/стартовые readiness-проверки перестают врать про свободное место не того диска и путь сокета не того каталога |

## Итог по двум явно оговорённым в задаче строкам

- **`cloud_stt.py:25`** — начинает читать канонический `settings.json` вместо
  несуществующего файла. Раньше облачный STT был де-факто мёртвым кодом на
  проде (ключи всегда пустые); теперь он оживает, если владелец когда-либо
  задавал ключ через `set_settings`.
- **`cloud_rewriter.py:141`** — то же самое для облачного rewriter. Приватностный
  гейт `engine._cloud_rewrite_allowed()` НЕ трогается этой задачей и остаётся
  единственной линией защиты: `privacy_mode_enabled=True` по-прежнему
  безусловно запрещает уход транскрипта наружу, независимо от того, что
  теперь читается из правильного `settings.json`.

## Не в объёме этой задачи

Лок-мина синхронизации нескольких экземпляров `StateStore` на одни и те же
файлы внутри одного процесса (`state_store.py:111`, per-thread
`_lock_depth` не защищает МЕЖДУ экземплярами) — отдельная находка I-C,
закрывается инъекцией общего store в `cloud_stt`/`cloud_rewriter` в
Задаче 2 этой волны, не здесь.
