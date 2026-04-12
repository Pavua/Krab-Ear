# Krab Ear — Руководство пользователя

Krab Ear — локальный голосовой ассистент для macOS. Распознаёт речь офлайн (mlx-whisper), переводит (RU↔ES, EN→RU), ведёт историю транскриптов, вставляет текст в активное приложение.

---

## Установка и первый запуск

### Требования
- macOS 13 (Ventura) или новее
- Apple Silicon (M1/M2/M3/M4) — для GPU-ускорения
- Python 3.11+ (устанавливается автоматически)

### Первый запуск
1. Откройте терминал в папке проекта
2. Дважды кликните **`Start Krab Ear.command`** — скрипт создаст виртуальное окружение, установит зависимости и запустит агент
3. При первом старте появится мастер разрешений (**PermissionWizard**) — выдайте доступ к микрофону и Accessibility (требуется для авто-вставки текста)
4. В строке меню появится иконка Krab Ear

### Запуск вручную
```bash
# Бэкенд (Python)
source .venv_krab_ear/bin/activate
python KrabEar/main.py --data-dir ~/.krab_ear_data

# REST-сервер (опционально, порт 5005)
./start_rest_service.command
```

### Автозапуск при входе
В панели Settings включите **"Запускать при старте системы"** — агент установит launchd plist автоматически.

---

## Основные функции

### Диктовка
1. Нажмите **Right Option** (правый Alt) — начнётся запись, появится плавающий оверлей с live-превью
2. Говорите; текст отображается в реальном времени
3. Снова нажмите **Right Option** — запись остановится, текст будет расшифрован и вставлен в активное приложение

Если курсор не стоит в текстовом поле, текст попадёт только в историю (без вставки).

### Live-перевод
Включите режим перевода в Settings (или выберите в главном окне). Поддерживаемые режимы:

| Режим | Описание |
|---|---|
| `off` | Только транскрипция |
| `ru_to_es` | Русский → Испанский |
| `es_to_ru` | Испанский → Русский |
| `en_to_ru` | Английский → Русский |
| `auto` | Авто-определение языка |
| `bilingual_ru_es` | Двуязычный вывод RU+ES |

При включённом **"Вставлять перевод"** вставляется переведённый текст, оригинал сохраняется в истории.

### Диаризация (определение спикеров)
Включите **"Диаризация"** в настройках. При обработке каждый фрагмент будет помечен идентификатором говорящего (`SPEAKER_00`, `SPEAKER_01`, …). Использует Metal GPU автоматически на Apple Silicon.

### LLM-постобработка
При включённом **LM Studio** (локальная модель qwen3-4b) текст автоматически редактируется после распознавания — убираются оговорки, исправляется пунктуация. Встроенные защиты:
- Chatbot guard: отклоняет ответы, начинающиеся с фраз-ботов
- Length ratio guard: отклоняет вывод < 35% или > 300% длины оригинала

---

## Горячие клавиши

| Сочетание | Действие |
|---|---|
| **Right Option** | Старт / стоп записи |
| **Cmd+H** | Открыть панель истории |
| **Cmd+,** | Открыть настройки |
| **Cmd+S** | Сохранить / экспортировать выделенное |
| **Cmd+C** | Копировать выделенный транскрипт |
| **Cmd+Delete** | Удалить запись из истории |
| **Cmd+F** | Поиск по истории |
| **Cmd+Z** | Отменить последнюю вставку |
| **Esc** | Закрыть панель / оверлей |

---

## Настройки

Откройте Settings (вкладка Settings в главном окне) или нажмите **Cmd+,**.

### Профили качества

| Профиль | Модель | Скорость | Точность |
|---|---|---|---|
| `fast` | whisper-small | ~0.5s | Базовая |
| `balanced` | whisper-large-v3-turbo | ~1–2s | Высокая |
| `max` | whisper-large-v3 | ~3–5s | Максимальная |

### Пресеты

Четыре готовых пресета применяются одним нажатием:

| Пресет | Описание |
|---|---|
| `default` | Сбалансированный режим |
| `meeting` | Оптимизирован для совещаний, диаризация включена |
| `translation` | Авто-определение + перевод |
| `call_recording` | Запись звонков, строгая очистка |

### Ключевые параметры

- **Профиль очистки:** `soft` — мягкая нормализация; `strict` — удаляет паразитные слова, деdup фраз
- **Сетевой режим:** `offline_default` (по умолчанию) / `offline_strict` / `online_opt_in`
- **Авто-вставка:** вставлять текст автоматически после остановки
- **Realtime-превью:** показывать плавающий оверлей во время записи
- **Аудиоустройство:** выбор микрофона из списка входных устройств

---

## Экспорт

### Из GUI
В панели History выберите записи и нажмите кнопку экспорта. Доступные форматы: SRT, Markdown, CSV, JSON, Obsidian.

### Через CLI
```bash
# Markdown (stdout)
python -m KrabEar.cli export --format md

# SRT в файл
python -m KrabEar.cli export --format srt --output subtitles.srt

# Obsidian-совместимый Markdown
python -m KrabEar.cli export --format obsidian --output notes.md
```

### Транскрипты аудиофайлов
При импорте аудио (кнопка Import в GUI) генерируются `.md` файлы в:
```
~/Library/Application Support/KrabEar/transcripts/
```

---

## CLI-инструмент

```bash
python -m KrabEar.cli <команда> [опции]
```

Опциональный флаг `--socket PATH` позволяет указать кастомный путь к Unix-сокету.

### 6 команд

#### `status` — статус бэкенда
```bash
python -m KrabEar.cli status
# Показывает: версию, uptime, статус записи, модель STT, путь к данным
```

#### `history` — последние транскрипты
```bash
python -m KrabEar.cli history
python -m KrabEar.cli history --limit 50
# Показывает: время, текст (100 символов), язык, уверенность
```

#### `export` — экспорт истории
```bash
python -m KrabEar.cli export                          # Markdown в stdout
python -m KrabEar.cli export --format srt             # SRT в stdout
python -m KrabEar.cli export --format obsidian --output vault.md
```

#### `stats` — статистика использования
```bash
python -m KrabEar.cli stats
# Показывает: кол-во записей, общую длительность, latency p50/p95, размер истории
```

#### `health` — диагностика подсистем
```bash
python -m KrabEar.cli health
# Проверяет: STT, диаризацию, перевод, LLM, хранилище
```

#### `transcribe` — транскрибировать файл
```bash
python -m KrabEar.cli transcribe /path/to/audio.m4a
python -m KrabEar.cli transcribe recording.wav
# Поддерживаемые форматы: wav, mp3, ogg, m4a, flac, opus, webm, mp4, aac
```

---

## REST API

Запустите REST-сервер через `./start_rest_service.command` или:
```bash
PYTHONPATH=$PYTHONPATH:$(pwd)/KrabEar python KrabEar/backend/rest_server.py
```

Swagger UI: `http://127.0.0.1:5005/api/docs`

### Основные эндпоинты

| Метод | Путь | Описание |
|---|---|---|
| GET | `/health` | Liveness-проба |
| GET | `/v1/readiness` | Проверка готовности компонентов |
| POST | `/v1/stt/transcribe` | Транскрибировать аудиофайл |
| GET | `/v1/vocabulary` | Текущий пользовательский словарь |
| POST | `/v1/vocabulary` | Добавить слова в словарь |
| GET | `/metrics` | Метрики производительности (JSON) |
| GET | `/metrics/prometheus` | Метрики в формате Prometheus |
| GET | `/v1/events` | SSE-поток событий STT |
| WS | `/ws/events` | WebSocket-поток событий |

### Пример транскрипции через curl
```bash
curl -F "file=@recording.m4a" \
     -F "quality_profile=balanced" \
     -F "lang_hint=ru" \
     http://127.0.0.1:5005/v1/stt/transcribe
```

### Аутентификация
Если задана переменная `KRAB_EAR_REST_API_KEY`, передавайте токен:
```bash
curl -H "Authorization: Bearer your-api-key" \
     http://127.0.0.1:5005/metrics
```

---

## Избранное, теги, коллекции

### Теги
В панели History кликните на запись → добавьте тег. Теги можно использовать для фильтрации.

Через IPC:
```json
{"method": "add_tag", "params": {"id": "item-id", "tag": "meeting"}}
{"method": "search_by_tag", "params": {"tag": "meeting"}}
{"method": "list_all_tags", "params": {}}
```

### Коллекции
Файл `KrabEar/backend/collection_manager.py` — группировка записей в именованные коллекции. Доступно через IPC-методы `create_collection`, `add_to_collection`, `get_collection`.

### Избранное
Используйте тег `favorite` для маркировки важных записей:
```bash
# Через IPC
{"method": "add_tag", "params": {"id": "item-id", "tag": "favorite"}}
```

---

## Резервное копирование

### Авто-резервирование
При включённом `auto_backup` в настройках бэкап создаётся автоматически перед компакцией истории.

### Ручное резервирование
```json
{"method": "backup_history", "params": {}}
```
Бэкапы хранятся в директории данных (`~/.krab_ear_data/backups/` или `~/Library/Application Support/KrabEar/backups/`).

### Просмотр и восстановление
```json
{"method": "list_backups", "params": {}}
{"method": "restore_history", "params": {"backup_id": "backup-2026-04-12T10-00-00"}}
```

### Очистка старых записей
```json
{"method": "cleanup_old_history", "params": {"days": 90}}
```

---

## Устранение неисправностей

### Бэкенд не запускается
```bash
python -m KrabEar.cli health
# или проверьте лог:
tail -f ~/.krab_ear_data/backend.log
```

### Нет вставки текста
1. Проверьте Accessibility: System Settings → Privacy & Security → Accessibility → Krab Ear ✓
2. Убедитесь, что курсор стоит в текстовом поле перед записью
3. Проверьте, что `auto_paste: true` в настройках

### Низкое качество распознавания
- Переключите `quality_profile` на `max`
- Добавьте специфичные слова в словарь: `POST /v1/vocabulary`
- Проверьте уровень микрофона: `python -m KrabEar.cli health`

### Перевод не работает
- Убедитесь, что `network_mode` не `offline_strict`, если используете облачный перевод
- Проверьте глоссарий на конфликты через `get_glossary_suggestions`

### Ошибка Socket not found
```
Error: Socket not found: ~/Library/Application Support/KrabEar/krabear.sock
```
Запустите бэкенд: `python KrabEar/main.py --data-dir ~/.krab_ear_data` или откройте `Start Krab Ear.command`.

### Диагностика
```bash
python -m KrabEar.cli status   # общее состояние
python -m KrabEar.cli health   # состояние подсистем
python -m KrabEar.cli stats    # статистика и метрики
```

---

## Хранение данных

| Файл | Содержимое |
|---|---|
| `history.ndjson` | Транскрипты (append-only с tombstone-удалениями) |
| `settings.json` | Настройки пользователя |
| `transcripts/` | Markdown-файлы импортированных аудио |
| `backups/` | Резервные копии истории |

Директория данных:
- **Production (launchd):** `~/Library/Application Support/KrabEar/`
- **Dev standalone:** `~/.krab_ear_data/`
