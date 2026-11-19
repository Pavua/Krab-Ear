# Внешние интерфейсы Krab Ear (контракт для соседних проектов)

Обновлено: 2026-09-17. Гард: `KrabEar/tests/test_external_surface_contract.py`.
Правило: переименование или удаление любого пункта ниже — это **ломающее изменение**.
Сначала предупредить потребителя (сессии Krab Main / Voice Gateway), потом менять.

## IPC (unix-socket `~/Library/Application Support/KrabEar/krabear.sock`, JSON-RPC построчно)

| Метод | Потребитель | Ключи ответа, которые читает потребитель |
|---|---|---|
| `ping` | Краб: health probe, KrabEarClient | `ok`, `result.status` (`"ok"`) |
| `synthesize_speech` | Краб: локальный TTS в `voice_engine` | `result.wav_bytes_b64` |
| `get_history_page` | Краб: MCP voice-инструменты (сейчас ошибочно зовут `get_history`) | `result.items` |

## REST (`http://127.0.0.1:5005`)

| Маршрут | Потребитель | Ключи ответа |
|---|---|---|
| `GET /health` | Краб: панель наблюдения (сейчас ошибочно `:18000`), smoke-раннер Ear | HTTP 200 |
| `POST /v1/stt/transcribe` | Краб: голосовые заметки, video studio; VG: STT звонков (`request_profile=voice_gateway_call`) | `text`, `segments` |
| `POST /v1/tts/synthesize` | VG: локальный TTS | `wav_bytes_b64` |

Явный `deadline_sec` для `POST /v1/stt/transcribe` включает подготовку аудио, очередь STT и транскрибацию. Обычный запрос с явным `deadline_sec` до 25 секунд включительно возвращает текст без разметки спикеров. Если диаризация запрошена явно или включена настройками, в успешном ответе есть `diarization.enabled=false` и `diarization.skipped="request_deadline"`. Для файловой транскрипции с разметкой можно опустить дедлайн или указать больше 25 секунд. Профиль `voice_gateway_call` всегда работает без диаризации.

Аутентификация REST (с 2026-11-15, по умолчанию ВКЛЮЧЕНА): защищённые маршруты требуют `Authorization: Bearer <ключ>`. Ключ выдаёт backend в `~/Library/Application Support/KrabEar/rest_api_key` (0600) либо задаётся явно (`KRAB_EAR_REST_API_KEY`, `REST_API_AUTH_ENABLED`); VG читает `KRAB_EAR_REST_API_KEY`. Без ключа `401`. Открыты: `GET /health`, `/info`, `/api/docs`. Опт-аут: env `KRAB_EAR_REST_API_ALLOW_ANONYMOUS=true`. Чужой заголовок `Host` (не loopback и не из `REST_ALLOWED_HOSTS`) даёт `403`. Бриф потребителям: `krab-handoffs/2026-11-15/EAR_REST_AUTH_BRIEF_FOR_VG_AND_KRAB.md`.

## Общие файлы-замки (не API, но стык)

| Файл | Кто пишет | Смысл |
|---|---|---|
| `~/.openclaw/lm_studio_brain.lock` | Ear (`owner="krab_ear"`, TTL 30 с), Краб (`owner="krab"`, TTL 120 с) | Сериализация большой модели в LM Studio |
| `~/Library/Application Support/KrabEar/mlx_inter_process.lock` | Ear (все MLX-пути) | Межпроцессная сериализация GPU. Сторонний процесс, запускающий MLX на этой машине, обязан брать этот flock |

## Известные расхождения у потребителей (17.09, брифы отправлены)

- Краб зовёт IPC `get_history` — метода нет; нужен `get_history_page`.
- Краб проверяет здоровье Ear на `:18000/health` — правильно `:5005/health`.
- Краб `mcp_client.py` зовёт `:5005/screenshot` — такого маршрута у Ear нет.
- Краб `perceptor.py` запускает mlx_whisper без `mlx_inter_process.lock`.
