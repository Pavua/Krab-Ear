# Продвинутая настройка STT — GigaAM-RNNT v2

> Это приложение к [Руководству пользователя](USER_MANUAL.md) для профессиональных пользователей русскоязычной диктовки. Если вы только начинаете — базовой настройки из руководства (раздел 10) более чем достаточно.

## GigaAM-RNNT v2 (RU-специализированная модель)

GigaAM — модель распознавания русскоязычной речи от Sber (salute-developers, лицензия MIT). На стандартном тесте Common Voice RU даёт **WER ~3.8%** против ~9.8% у whisper-large-v3 — то есть **в 2.5 раза меньше ошибок** на русском. По умолчанию выключена; включается опционально, для пользователей с активной русскоязычной диктовкой.

### Когда включать

- 80%+ диктовок на русском
- Часто диктуете имена/термины/специальную лексику (Whisper их часто слышит «как в литературе»)
- Готовы потратить ~630 МБ диска под изолированный venv + ~1 ГБ под веса модели

### Когда НЕ включать

- Преимущественно EN/ES — GigaAM работает только с русским
- Хотите идеально lightweight setup (whisper-large-v3 уже есть и работает)

### Установка (одноразово, ~3-5 мин)

GigaAM нельзя поставить в основной Krab Ear venv: пакет требует `torch<=2.5.1`, что несовместимо с Python 3.14 + torch 2.11. Поэтому используется изолированный venv с Python 3.12 (~/.venv_krab_ear_gigaam).

1. **Убедись что Python 3.12 установлен:**
   ```bash
   /opt/homebrew/bin/python3.12 --version
   # Если нет — установи: brew install python@3.12
   ```

2. **Запусти one-click installer:**
   ```bash
   bash scripts/install_gigaam_venv.command
   ```
   Скрипт создаст `~/.venv_krab_ear_gigaam`, поставит torch 2.5.1 + onnxruntime 1.23 + gigaam, и проверит smoke import. На M4 Max занимает ~3 мин (зависит от скорости сети).

3. **Включи движок:** в приложении — «Настройки → STT-движки» → тумблер GigaAM-RNNT. Альтернатива без GUI — через IPC:
   ```bash
   python3 -c "
   import socket, json, os
   sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
   sock.connect(os.path.expanduser('~/Library/Application Support/KrabEar/krabear.sock'))
   sock.sendall(json.dumps({'id':'1','method':'set_settings','params':{'stt_gigaam_enabled':True}}).encode()+b'\n')
   print(sock.recv(4096).decode())
   "
   ```

### Как работает

Когда `STT_GIGAAM_ENABLED=true` и detected_lang == "ru":
1. AudioEngine помещает GigaAM первым в STT chain
2. Аудио уходит в worker subprocess (запущен из venv_gigaam, держит модель в памяти)
3. Worker возвращает текст через JSON по stdin/stdout (~50–200 мс на короткую фразу после первой загрузки)
4. При любой ошибке (worker crash, timeout, недоступность venv) — fallback на whisper-large-v3 без потери транскрипции

Первая транскрипция после старта backend будет **медленнее** (~5-15 сек на загрузку модели в worker'е). Все последующие — быстрые.

### Параметры (в config.py / env vars)

| Setting (env: `KRAB_EAR_<NAME>`) | Default | Описание |
|---|---|---|
| `STT_GIGAAM_ENABLED` | `False` | Включить GigaAM в STT chain |
| `STT_GIGAAM_MODE` | `"rnnt"` | `"rnnt"` (выше качество) или `"ctc"` (быстрее) |
| `STT_GIGAAM_DEVICE` | `"cpu"` | `"cpu"` (default, рекомендуется по bench 2026-04-26) или `"mps"` |
| `STT_GIGAAM_TRANSPORT` | `"auto"` | `"auto"` / `"in_process"` / `"subprocess"` |
| `STT_GIGAAM_VENV_PYTHON` | `""` | Путь к venv-Python (пусто = `~/.venv_krab_ear_gigaam/bin/python`) |
| `STT_GIGAAM_HF_TOKEN` | `""` | HuggingFace API token для longform (см. ниже) |

### Длинные аудио (> 30 сек) — `transcribe_longform`

GigaAM `transcribe()` имеет hard limit ~25–30 сек на одну операцию. Для длинных файлов (импорт звонков, диктовка > 30 сек) используется `transcribe_longform()`, который через `pyannote.audio` нарезает аудио по VAD-сегментам и склеивает результат.

**Setup (one-time):**

1. **Доустановить зависимости в venv_gigaam:**
   ```bash
   ~/.venv_krab_ear_gigaam/bin/pip install "gigaam[longform]"
   # huggingface_hub 1.x несовместим с pyannote 3.4 (deprecated `use_auth_token` API):
   ~/.venv_krab_ear_gigaam/bin/pip install "huggingface_hub<0.26"
   ```

2. **Принять TOS на HuggingFace** (`pyannote/voice-activity-detection` — gated repo):
   - Открой https://hf.co/pyannote/voice-activity-detection (потребуется HF аккаунт)
   - Нажми **"Agree and access repository"**
   - Опционально: то же для `pyannote/segmentation-3.0` если попросит

3. **HF token** уже должен быть в `~/.cache/huggingface/token` (от предыдущей `huggingface-cli login`). Проверь:
   ```bash
   ~/.venv_krab_ear_gigaam/bin/python -c "from huggingface_hub import HfApi; print(HfApi().whoami()['name'])"
   ```
   Если token нет — `huggingface-cli login` с тем же интерпретатором.

4. **Активировать longform в backend** (опционально — установи свой token):
   ```python
   set_settings({"stt_gigaam_hf_token": "hf_..."})  # пусто = используется cached
   ```

После этого `GigaAMAdapter.transcribe(audio, longform=True, hf_token=settings.STT_GIGAAM_HF_TOKEN)` будет работать. В транскрибированной записи поле `engine` станет `gigaam-rnnt-longform`.

### Проверка после включения

После того как enable + первая диктовка на русском прошла:
```bash
log show --last 5m --predicate 'eventMessage CONTAINS "GigaAM"' --style compact 2>/dev/null | head -20
```
Должна быть строка `GigaAM-RNNT добавлен в chain первым` и `GigaAM транскрибация завершена`.

В транскрибированной записи поле `engine` будет `gigaam-rnnt` (вместо `mlx-whisper-large-v3`).

### Откат

```bash
# Выключить: «Настройки → STT-движки» → тумблер GigaAM, или через IPC: stt_gigaam_enabled = false
# Венв можно удалить если больше не нужен:
rm -rf ~/.venv_krab_ear_gigaam
```
