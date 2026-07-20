# Продвинутая настройка STT — GigaAM v3

> Это приложение к [Руководству пользователя](USER_MANUAL.md) для активной
> русскоязычной диктовки. Для EN/ES основным движком остаётся Whisper.

## Что даёт GigaAM

GigaAM — специализированная модель русской речи от Sber/Salute Developers
(MIT). Продовый режим Krab Ear — `v3_e2e_rnnt`: он возвращает пунктуацию,
капитализацию и числа сразу из модели. GigaAM запускается в отдельном Python
3.12-окружении, поэтому его PyTorch-зависимости не смешиваются с основным
backend.

Подключать его разумно, если большая часть диктовок на русском и важны имена,
термины или разговорная речь. Для преимущественно EN/ES и минимального размера
установки достаточно Whisper.

## Установка одним запуском

1. Установи Python 3.12, если его ещё нет:

   ```bash
   brew install python@3.12
   ```

2. Дважды нажми в Finder на `scripts/install_gigaam_venv.command` или запусти
   тот же `.command` из Terminal:

   ```bash
   ./scripts/install_gigaam_venv.command
   ```

Installer создаёт `~/.venv_krab_ear_gigaam`, checkout'ит пинованный commit
GigaAM, устанавливает полный extra `longform` и проверяет импорты GigaAM,
PyTorch, pyannote, Hugging Face Hub и torchcodec. Повторный запуск не удаляет
существующее окружение без явного подтверждения.

3. В приложении открой «Настройки → STT-движки» и включи GigaAM.

Первая фраза после старта backend включает загрузку модели и будет медленнее.
Следующие запросы используют уже прогретый worker.

## Как устроен fallback

Когда GigaAM включён и язык — русский:

1. `AudioEngine` ставит GigaAM первым в STT-chain.
2. Аудио уходит в долгоживущий worker из изолированного venv.
3. Worker возвращает один JSON-ответ через stdin/stdout.
4. Пустой ответ, crash, timeout или ошибка модели не считаются успехом — chain
   продолжает распознавание через Whisper.

У GigaAM shortform есть точная граница: не более `25 * 16000` сэмплов, то есть
25,0 секунды при 16 кГц. Любая запись длиннее 25 секунд сначала режется
`AudioChunker` на безопасные фрагменты до 20 секунд и склеивается обратно.
Если chunker недоступен, используется `transcribe_longform()` с pyannote VAD.

## Параметры

| Setting (`KRAB_EAR_<NAME>`) | Default | Назначение |
|---|---:|---|
| `STT_GIGAAM_ENABLED` | `False` | Добавить GigaAM в русский STT-chain |
| `STT_GIGAAM_MODE` | `v3_e2e_rnnt` | Модель; полное имя исключает неоднозначный alias `rnnt` |
| `STT_GIGAAM_DEVICE` | `cpu` | `cpu` или `mps` |
| `STT_GIGAAM_TRANSPORT` | `auto` | `auto`, `in_process` или `subprocess` |
| `STT_GIGAAM_VENV_PYTHON` | пусто | Пусто означает `~/.venv_krab_ear_gigaam/bin/python` |
| `STT_GIGAAM_HF_TOKEN` | пусто | Токен для загрузки gated-модели pyannote при cache miss |

## Longform и Hugging Face

Новый installer уже ставит longform-зависимости; вручную доустанавливать
`gigaam[longform]` или откатывать `huggingface_hub` не нужно. Для основного
`AudioChunker`-пути HF token также не нужен.

Pyannote-fallback сначала ищет `pyannote/segmentation-3.0` в локальном HF-кэше.
Если модели там нет:

1. Прими условия доступа на странице модели Hugging Face.
2. Укажи token в настройках Krab Ear (`hf_token` или
   `stt_gigaam_hf_token`). Значение применяется без рестарта и никогда не
   выводится в лог.

Успешный fallback имеет engine `gigaam-rnnt-longform`; обычный chunker-путь —
`gigaam-rnnt-chunked`.

## Проверка

После включения продиктуй одну короткую и одну запись длиннее 25 секунд. В
диагностике backend должны появиться сообщения о добавлении GigaAM в chain и
завершённой транскрибации, а не `gigaam-error` или пустой текст.

Для просмотра недавних локальных сообщений:

```bash
log show --last 5m --predicate 'eventMessage CONTAINS "GigaAM"' --style compact
```

## Откат

Выключи GigaAM в «Настройки → STT-движки»: Whisper сразу снова станет первым
движком. Если окружение больше не нужно, после выключения backend его можно
переместить в Корзину через Finder; удалять его во время активного worker нельзя.
