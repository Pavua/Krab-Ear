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

Аутентификация REST: опциональный Bearer (`REST_API_AUTH_ENABLED`); VG передаёт `KRAB_EAR_REST_API_KEY`.

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
