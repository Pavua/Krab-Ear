# Phase 1 Voice Assistant Mode — Руководство по установке и запуску

**Дата:** 2026-04-17  
**Версия:** Phase 1 MVP  
**Автор:** Claude Code для Pavel

---

## Что такое Voice Assistant Mode (Phase 1)?

Режим "Разговор с AI" позволяет вам общаться с Кравом голосом **в реальном времени**, как с живым ассистентом:

- **160-400ms задержка** end-to-end (вместо 3-4 секунд в обычном режиме STT→LLM→TTS)
- **Full-duplex диалог**: вы можете перебить AI посередине фразы, и он адаптируется
- **Многоязычность**: RU (основной), EN, ES поддерживаются
- **Интеграция с Кравом**: общая память, инструменты, настройки — voice — это просто "ещё один канал"
- **3 способа запуска**: кнопка в интерфейсе, глобальная клавиша, или voice-команда "Краб"

**Архитектура**: Krab Ear UI ↔ Voice Gateway (Moshi/Seamless движки) ↔ Krab agent (LLM мозг)

Подробнее: [Voice Assistant Mode spec](./superpowers/specs/2026-04-17-voice-assistant-mode-design.md)

---

## Требования и Checklist

### Системные требования

- **macOS 13+**
- **M1/M2/M3/M4 Apple Silicon** (Metal GPU поддерживается, обязателен для Moshi)
- **Минимум 36 GB RAM** (у вас M4 Max 36 GB — ок, но плотно):
  - Moshi 7B: 8-12 GB RAM
  - Qwen3-30B: 17.2 GB RAM (включая KV cache)
  - Seamless: 12-16 GB RAM
  - Буфер OS + другие приложения: ~5 GB
- **Свободное место на диске**: ~50 GB (модели + кэши)
- **Интернет**: требуется для первого скачивания моделей (затем работает оффлайн)

### Ports (должны быть свободны)

- **8090** — Voice Gateway WebSocket
- **8081** — Voice Gateway OpenClaw proxy (Krab agent bridge)
- **5005** — Krab Ear REST API (если используется)

### Prerequisites Checklist

- [ ] Python 3.9+ с venv (проверьте: `python3 --version`)
- [ ] Xcode Command Line Tools (`xcode-select --install`)
- [ ] LM Studio установлена (для скачивания/управления Qwen3-30B)
- [ ] Krab Ear `.app` собрана (`native/runtime/KrabEarAgent` существует)
- [ ] Voice Gateway repo доступен (`/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway`)
- [ ] Krab telegram-агент запущен (для OpenClaw)

---

## Step-by-Step Setup (10 шагов)

### Шаг 1: Подготовка Voice Gateway окружения

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway"

# Создайте venv (если ещё нет)
python3 -m venv .venv_voice_gateway
source .venv_voice_gateway/bin/activate

# Установите зависимости Voice Gateway
pip install --upgrade pip
pip install -r requirements.txt
```

**Что устанавливается:**
- `moshi-mlx==0.3.0` — Kyutai Moshi 7B engine для англ.
- `torch`, `transformers` — для SeamlessStreaming/SeamlessM4T
- `fastapi`, `uvicorn` — обслуживание Voice Gateway

**Возможная ошибка:** `moshi-mlx` конфликтует с версией `torch`.
→ **Решение:** установите в отдельном venv или используйте `pip install --no-deps moshi-mlx==0.3.0 && pip install torch transformers`.

### Шаг 2: Скачайте Qwen3-30B через LM Studio

1. Откройте **LM Studio** app
2. Перейдите в поисковую панель
3. Найдите: `lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit`
4. Нажмите **Download** (будет скачано ~17 GB)
5. Дождитесь завершения (может занять 30-60 минут в зависимости от скорости интернета)

**Где сохраняется:** обычно в `~/.cache/huggingface/hub/models--lmstudio-community--Qwen3-...`

**Если скачивание не работает:** проверьте:
- [ ] Интернет доступен
- [ ] Свободное место на диске (>20 GB)
- [ ] Нет блокировки HuggingFace (региональные ограничения)

### Шаг 3: Установите Seamless модели (опционально для RU)

Для русского языка Phase 1 использует **SeamlessStreaming** или **SeamlessM4T v2**.

```bash
# В активированном venv Voice Gateway:
python3 -c "from transformers import AutoModel; m = AutoModel.from_pretrained('facebook/seamless-streaming'); print('Downloaded to ~/.cache/huggingface')"
```

**Размер:** 12-16 GB (будет скачано один раз)  
**Предупреждение:** Лицензия CC-BY-NC 4.0 (только личное использование, не коммерческое).

### Шаг 4: Подготовьте Porcupine Wake Word (для "Краб" voice trigger)

#### Шаг 4a: Зарегистрируйтесь на Picovoice Console

1. Перейдите на https://console.picovoice.co
2. Зарегистрируйтесь / войдите (можно через GitHub)
3. Скопируйте **AccessKey** из Account → Access Keys

#### Шаг 4b: Сохраните AccessKey локально

```bash
mkdir -p ~/.krab_ear_data
echo "YOUR_PICOVOICE_ACCESS_KEY" > ~/.krab_ear_data/porcupine_access_key
```

Замените `YOUR_PICOVOICE_ACCESS_KEY` на реальный ключ из Picovoice.

#### Шаг 4c: Тренируйте keyword "Краб"

1. В Picovoice Console → Custom Keywords
2. Создайте новый keyword: **название** = "Краб", **язык** = Russian
3. Запишите 2-3 примера произношения слова "Краб"
4. Нажмите **Train**
5. Скачайте `.ppn` файл (укажите platform: **macOS ARM64**)
6. Переместите в нужную папку:

```bash
mkdir -p ~/Library/Application\ Support/KrabEar/
# Скопируйте downloaded_file.ppn в папку:
cp ~/Downloads/Краб_ru_mac_v3_0_0.ppn ~/Library/Application\ Support/KrabEar/
```

**Проверка:** должен существовать файл `~/Library/Application Support/KrabEar/Краб_ru_mac_v3_0_0.ppn`

### Шаг 5: Раскомментируйте Porcupine в Swift Package

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"

# Отредактируйте Package.swift:
nano Package.swift
```

Найдите строку с `.package(url: "https://github.com/Picovoice/ios-sdk.git"...` и раскомментируйте её.

Также в `WakeWordListener.swift` раскомментируйте блоки `#if PORCUPINE_ENABLED`.

```bash
# Или через sed (один раз):
sed -i '' 's/\/\/ \.package(url: ".*picovoice/.package(url: "...picovoice/g' Package.swift
```

### Шаг 6: Соберите и подпишите Swift Agent

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"

# Build в release режиме
swift build -c release

# Скопируйте в .app bundle
cp .build/release/KrabEarAgent ../runtime/KrabEarAgent

# Подпишите (ad-hoc)
codesign -s - -f ../runtime/KrabEarAgent

# Проверьте что скопировалось:
ls -lh "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
```

**Время сборки:** 3-5 минут на M4 Max.

### Шаг 7: Запустите Voice Gateway

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway"
source .venv_voice_gateway/bin/activate

# Запустите сервер на порту 8090
python -m app.main

# Должно вывести:
# INFO:     Uvicorn running on http://127.0.0.1:8090
# INFO:     Voice Gateway ready for v1/sessions/{id}/conversation WS
```

**В отдельном терминале проверьте:**

```bash
curl http://127.0.0.1:8090/health
# Ответ: {"status": "ok"}
```

**Если не запускается:**
- Проверьте что порт 8090 свободен: `lsof -i :8090`
- Проверьте что requirements установлены: `pip list | grep moshi`
- Посмотрите лог ошибки на экране

### Шаг 8: Убедитесь что Krab OpenClaw работает

Voice Gateway нуждается в Krab agent для LLM мозга (port 8081).

```bash
# Проверьте что Krab запущен
ps aux | grep "krab\|openclaw" | grep -v grep

# Или запустите через start_krab.command:
/Users/pablito/Antigravity_AGENTS/new\ start_krab.command
```

**Требуется:** OpenClaw Gateway должен быть доступен на `http://127.0.0.1:8081`.

### Шаг 9: Запустите Krab Ear `.app`

```bash
# Откройте приложение:
open "/Users/pablito/Antigravity_AGENTS/Krab Ear/Krab Ear.app"

# Или из терминала (для видения логов):
PYTHONPATH=/Users/pablito/Antigravity_AGENTS/Krab\ Ear/KrabEar \
/Users/pablito/Antigravity_AGENTS/Krab\ Ear/Krab\ Ear.app/Contents/MacOS/KrabEarAgent
```

### Шаг 10: Включите "Разговор с AI" в Settings

1. В Krab Ear `.app` → перейдите на вкладку **Settings** (3-я вкладка)
2. Найдите раздел **Voice Assistant Mode**
3. Включите toggle **"Разговор с AI"**
4. Убедитесь что выбраны правильные параметры:
   - **Engine**: Moshi (для EN) или Seamless (для RU)
   - **Language**: Русский (RU) или English (EN)
   - **TTS Voice**: ваш предпочитаемый голос (macOS `say`)

---

## Testing — Проверка работоспособности

Следуйте этим шагам чтобы убедиться что всё работает:

### Тест 1: Простой диалог

1. Убедитесь что Voice Gateway запущен (шаг 7)
2. Откройте Krab Ear `.app` → вкладка **Conversation** (или "Разговор с AI")
3. Нажмите кнопку **Start Conversation** (или Right Option double-tap если wake word включён)
4. Скажите: **"Привет, как дела?"**
5. Вы должны увидеть:
   - [ ] Live transcript (ваш голос транскрибируется в реальном времени)
   - [ ] AI ответ (текст появляется по мере обработки)
   - [ ] AI говорит ответ через speaker

**Ожидаемая задержка:** 1-3 секунды до первого слова AI ответа (cold start медленнее).

### Тест 2: Full-duplex interruption

1. Нажмите Start Conversation
2. Начните говорить
3. В середине AI ответа скажите что-то новое (interrupt)
4. AI должен остановиться и начать обрабатывать ваше новое высказывание

### Тест 3: История и история

После диалога проверьте что:

1. Вкладка **History** содержит новую запись с режимом `voice_assistant`
2. Transcript сохранён в `~/.krab_ear_data/history.ndjson`
3. Вы можете экспортировать диалог как `.md` файл

---

## Troubleshooting — Диагностика и решение проблем

### Engine not loading — Timeout при загрузке Moshi

**Симптом:** Voice Gateway зависает при инициализации Moshi, выдаёт timeout после 120+ сек.

**Ожидаемое поведение:** первая загрузка 30-60 сек (cold start), последующие <2 сек (LRU cache).

**Решение:**
1. Проверьте доступность Metal MPS:
   ```bash
   python3 -c "import torch; print('MPS available:', torch.backends.mps.is_available())"
   ```
   Если `False` → Moshi не может использовать GPU, будет медленнее на CPU.

2. Проверьте размер кэша моделей:
   ```bash
   du -sh ~/Library/Caches/mlx/
   ```
   Если > 50 GB → очистите кэш: `rm -rf ~/Library/Caches/mlx/*`

3. Проверьте свободную память:
   ```bash
   vm_stat | grep "free\|inactive"
   ```
   Нужно минимум 6-8 GB свободно для Moshi 7B.

### High latency (>2s end-to-end)

**Симптом:** Voice Assistant отвечает через 3+ секунды (вместо ожидаемых 1.4 сек).

**Целевые метрики (M4 Max 36 GB):**

| Этап | p50 | p95 |
|------|-----|-----|
| Moshi STT (EN) | 800ms | 1.2s |
| Qwen3-4B first token | 400ms | 800ms |
| TTS synthesis (macOS say) | 200ms | 400ms |
| **End-to-end (round-trip)** | **~1.4s** | **~2.4s** |

**Диагностика:**
1. Проверьте что LM Studio загрузил правильную модель (НЕ qwen3-30b):
   ```bash
   ps aux | grep lm-studio | grep -v grep
   # Должна быть qwen3-4b (4 GB модель), не 30b (17 GB)
   ```

2. Проверьте Metal GPU utilization:
   ```bash
   # В Activity Monitor → перейдите в GPU tab
   # Moshi должна использовать 70-90% GPU при обработке
   # Если 0% → процесс работает на CPU (медленнее в 5-10 раз)
   ```

3. Проверьте swap pressure:
   ```bash
   vm_stat | grep "pageouts"
   # Если растёт быстро → недостаточно RAM, модели вываливаются на диск
   ```

### Voice Gateway WebSocket disconnects

**Симптом:** Krab Ear логирует `ws://127.0.0.1:8090 connection closed` или `WebSocket disconnected`.

**Решение:**
1. Проверьте что Voice Gateway запущена и healthcheck проходит:
   ```bash
   curl http://127.0.0.1:8090/health
   # Ответ: {"status": "ok"}
   ```

2. Проверьте логи Voice Gateway:
   ```bash
   tail -f ~/.krab_ear_data/voice_gateway.log
   # Ищите ошибки вроде "address already in use" или "connection reset"
   ```

3. Убедитесь что порт 8090 свободен:
   ```bash
   lsof -i :8090
   # Должна быть только одна строка с uvicorn/python
   # Если несколько процессов → `pkill -f "app.main"` и перезапустите
   ```

### TCC permission loops

**Симптом:** Даже после grant Accessibility, Krab Ear снова запрашивает permission при каждом запуске.

**Решение:** см. CLAUDE.md раздел "TCC permissions troubleshooting". Кратко:
```bash
pkill -9 -f KrabEarAgent
tccutil reset All com.antigravity.krab-ear
# User вручную добавляет Krab Ear.app в System Settings → Privacy → Accessibility
```

### Low free RAM prevents Moshi load

**Симптом:** `MemoryError: Unable to allocate 2.3 GB for Moshi weights`.

**Требования RAM:**
- Moshi 7B: 5-6 GB (+ KV cache)
- SeamlessStreaming 2.5B: 2-3 GB
- Qwen3-4B (LM Studio): 4 GB
- OS + другие приложения: 3-5 GB
- **Итого:** нужно 12-15 GB свободно перед стартом

**Решение:**
```bash
# Закройте браузеры, IDE и другие heavy приложения
pkill -9 Chrome  # или Safari/Firefox
pkill -9 Simulator

# Перезагрузитесь если свободно <10 GB
```

---

## Expected Latency Metrics (M4 Max 36GB)

**Source:** benchmark matrix из PR #42 Phase 1 testing.

| Метрика | Значение | Примечание |
|---------|----------|-----------|
| Moshi STT (EN) cold start | 30-60 sec | Первая загрузка модели |
| Moshi STT (EN) warm start | <2 sec | LRU cache hit, subsequent turns |
| Moshi STT (EN) latency | 160-200ms | Per audio frame (80 ms window) |
| Qwen3-4B TTFT | 400ms | Time-to-first-token |
| TTS synthesis (macOS say) | 200ms | Voice synthesis + playback |
| **End-to-end (round-trip)** | **~1.4s** | **800+400+200 ms** |
| Qwen3-4B throughput | 30-50 t/s | Tokens per second |
| Full conversation (5 turn) | ~10-15 sec | 3-4 sec per cycle + TTS |

---

## Troubleshooting — Решение проблем

### ❌ "moshi-mlx" не устанавливается / конфликт torch

**Симптом:** `pip install moshi-mlx` выдаёт ошибку версий torch.

**Решение:**
```bash
# Вариант 1: Отключите зависимости moshi
pip install --no-deps moshi-mlx==0.3.0
pip install torch>=2.1 transformers>=4.45

# Вариант 2: Используйте отдельный venv для Moshi
python3 -m venv .venv_moshi
source .venv_moshi/bin/activate
pip install moshi-mlx==0.3.0
```

### ❌ Qwen3-30B не загружается в LM Studio

**Симптом:** Поиск не находит модель или скачивание зависает.

**Решение:**
1. Очистите кэш HF: `rm -rf ~/.cache/huggingface/hub/models--lmstudio*`
2. Скачайте вручную:
   ```bash
   huggingface-cli download lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit
   ```
3. Или скачайте напрямую: https://huggingface.co/lmstudio-community/Qwen3-30B-A3B-Instruct-2507-MLX-4bit

### ❌ Porcupine keyword не срабатывает

**Симптом:** Говорите "Краб" но wake word не детектируется.

**Решение:**
1. Проверьте AccessKey сохранён:
   ```bash
   cat ~/.krab_ear_data/porcupine_access_key
   ```
2. Проверьте файл `.ppn` существует:
   ```bash
   ls ~/Library/Application\ Support/KrabEar/Краб_ru_mac_v3_0_0.ppn
   ```
3. Переучите keyword на Picovoice (добавьте больше примеров произношения)
4. Убедитесь что микрофон работает (System Preferences → Sound → Input)

### ❌ WebSocket connection refused (8090 не доступен)

**Симптом:** Krab Ear логирует: `ws://127.0.0.1:8090 connection refused`.

**Решение:**
1. Проверьте Voice Gateway запущен:
   ```bash
   lsof -i :8090
   # Должна быть строка с uvicorn/python
   ```
2. Перезапустите Voice Gateway:
   ```bash
   # Убейте старый процесс (если зависает)
   pkill -f "python.*app.main"
   # Запустите заново
   python -m app.main
   ```
3. Проверьте нет ошибок на старте:
   ```bash
   python -m app.main 2>&1 | head -20
   ```

### ❌ OpenClaw Gateway (8081) недоступен

**Симптом:** Voice Gateway логирует: `OpenClaw bridge failed` или `port 8081 refused`.

**Решение:**
1. Убедитесь что Krab запущен:
   ```bash
   ps aux | grep openclaw | grep -v grep
   ```
2. Если не запущен, запустите через start_krab.command:
   ```bash
   /Users/pablito/Antigravity_AGENTS/new\ start_krab.command
   ```
3. Дождитесь инициализации (~10 секунд)
4. Проверьте что 8081 доступен:
   ```bash
   curl http://127.0.0.1:8081/health
   ```

### ❌ Высокая задержка или прерывистый звук

**Симптом:** Voice Assistant отвечает через 5+ секунд или звук заикается.

**Причины:**
- Qwen3-30B полностью загрузился (первый раз — ок, это normal)
- Ваш Mac перегревается (проверьте Activity Monitor → CPU/GPU)
- Voice Gateway занимает много памяти
- Других приложений потребляют RAM

**Решение:**
1. Закройте другие приложения (особенно браузеры)
2. Проверьте что только одна модель loaded:
   ```bash
   # В Voice Gateway логах должно быть одно "loaded" сообщение
   ```
3. Если модель не выгружается, перезапустите Voice Gateway

### ❌ "Segmentation fault" при загрузке Seamless

**Симптом:** Крах при инициализации SeamlessStreaming для RU.

**Решение:**
1. Убедитесь что torch скомпилирован для Metal:
   ```bash
   python3 -c "import torch; print(torch.backends.mps.is_available())"
   # Должен быть True
   ```
2. Очистите кэш моделей:
   ```bash
   rm -rf ~/.cache/huggingface/hub/models--facebook--seamless*
   ```
3. Переустановите зависимости:
   ```bash
   pip install --upgrade torch transformers
   ```

---

## Known Limitations (текущие ограничения Phase 1)

### Moshi 5-minute buffer cap

- Moshi 7B имеет встроенный буфер на 5 минут аудио
- После 5 минут модель автоматически recycled (внутренняя операция)
- **Воздействие на user:** пауза ~100ms при переходе буфера (почти не заметна)

### SeamlessStreaming latency 1-2 сек (не 160ms)

- Несмотря на название "streaming", SeamlessStreaming имеет ~1-2 сек latency
- Это нормально (vs Moshi которая ~200ms для EN)
- **Решение для RU:** используйте Moshi если возможно (требует фин-тюнинга на RU данных)

### Qwen3-30B требует 17.2 GB RAM

- При M4 Max 36 GB это "плотно" если запущены другие приложения
- **Решение:** закройте браузеры, IDE, другие heavy приложения перед диалогом
- **Fallback:** используйте qwen3-4b если 30B не помещается

### Только русский/английский/испанский

- Фиксированные языки в Phase 1
- Phase 2 добавит автоматическое определение языка
- Phase 3 добавит больше языков

### Нет сохранения контекста между сеансами

- Каждый диалог — это новый сеанс (fresh context)
- LLM не видит предыдущих диалогов (только через history)
- **Будущее:** Phase 2 добавит persistent context

---

## Performance Benchmarks (на M4 Max 36 GB)

| Метрика | Значение |
|---------|----------|
| Cold start Moshi | ~8 сек (первая загрузка модели) |
| Warm start (second turn) | ~1.5 сек |
| Moshi latency (EN) | 160-200ms |
| SeamlessStreaming latency (RU) | 1-2 сек |
| Qwen3-30B latency (LLM processing) | 68-100 t/s (tokens/sec) |
| Memory: Moshi loaded | +8-12 GB |
| Memory: Qwen3-30B loaded | +17.2 GB |
| Memory: Seamless loaded | +12-16 GB |
| **Total when all 3 active** | ~36-40 GB (requires 36 GB machine) |

**Рекомендация:** используйте ONE engine за раз (lazy-load + LRU eviction).

---

## Next Steps

1. ✅ Завершите все 10 шагов setup
2. ✅ Пройдите testing section (3 теста)
3. ✅ Проверьте troubleshooting если есть issues
4. 📊 Обратитесь в документацию Phase 1 если нужен deeper dive:
   - [Voice Assistant Mode spec](./superpowers/specs/2026-04-17-voice-assistant-mode-design.md)
   - [Voice Assistant Mode plan](./superpowers/plans/2026-04-17-voice-assistant-mode.md)

---

## Feedback & Support

Если вы столкнулись с проблемой которая не описана здесь:

1. Проверьте Voice Gateway логи: `tail -f ~/.krab_ear_data/voice_gateway.log`
2. Проверьте Krab Ear логи: `~/.krab_ear_data/krab_ear.log`
3. Создайте issue на GitHub с логами + описанием шагов воспроизведения

---

**Last updated:** 2026-04-17  
**Next Phase:** Phase 1.5 (Porcupine wake word hardening), Phase 1.6 (UI polish), Phase 2 (Live Translation)
