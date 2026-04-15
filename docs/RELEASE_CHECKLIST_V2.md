# Release Checklist — Krab Ear v2.0+

Используется перед каждым тегом начиная с v2.0.0.
Автоматическая часть: `scripts/run_release_checklist.command`
Отчёт сохраняется в `docs/reports/release_checklist_*.md`.

---

## 1. Версия и история изменений

- [ ] Обновить `CFBundleShortVersionString` и `CFBundleVersion` в `Krab Ear.app/Contents/Info.plist`
- [ ] Обновить строку `"version": "1.0.0"` в `KrabEar/backend/service.py` (метод `handle_ping`, строка ~703)
- [ ] Дополнить `docs/CHANGELOG.md` — новый раздел `## vX.Y.Z — YYYY-MM-DD`
- [ ] Нет незакоммиченных изменений: `git status`

---

## 2. Окружение

- [ ] venv активен и python доступен:
  ```bash
  source .venv_krab_ear/bin/activate && python --version
  ```
- [ ] Зависимости актуальны:
  ```bash
  pip install -r KrabEar/requirements.txt
  ```
- [ ] HF-токен задан (нужен для диаризации pyannote):
  ```bash
  grep HF_TOKEN ~/.krab_ear_data/settings.json || echo "KRAB_EAR_HF_TOKEN=<token>" >> ~/.zshenv
  ```
- [ ] Свободного места ≥ 3 ГБ (MLX-модели ~1.5 ГБ + бэкапы)

---

## 3. Тесты

- [ ] Все Python-тесты проходят (ожидается 4099+, 0 ошибок):
  ```bash
  PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v --tb=short 2>&1 | tail -5
  ```
- [ ] Полный автоматический чеклист (lint + build + tests + smoke):
  ```bash
  scripts/run_release_checklist.command
  ```
- [ ] Smoke-тест релиза:
  ```bash
  scripts/run_smoke_release.command
  ```

---

## 4. Сборка Swift-агента

- [ ] Swift-агент собирается в release без ошибок:
  ```bash
  cd native/KrabEarAgent && swift build -c release 2>&1 | tail -5
  ```
- [ ] Бинарник скопирован и подписан:
  ```bash
  cp -f .build/release/KrabEarAgent ../../native/runtime/KrabEarAgent
  codesign -s - -f ../../native/runtime/KrabEarAgent
  cp -f ../../native/runtime/KrabEarAgent "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
  codesign -s - -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
  ```
- [ ] Shell-скрипты без синтаксических ошибок:
  ```bash
  find . -maxdepth 2 -name "*.command" -print0 | xargs -0 -n1 zsh -n
  ```

---

## 5. Функциональная проверка (ручная)

### Запуск
- [ ] Приложение запускается: `Start Krab Ear.command`
- [ ] Все 3 вкладки отображаются: Main, History, Settings
- [ ] Права Accessibility выданы для агента

### Основной цикл
- [ ] Hotkey (Right Option) — начинает запись; повторный — останавливает
- [ ] STT транскрипция работает (запись + вставка в активное окно)
- [ ] Повторный hotkey-цикл 3–5 раз — нет ghost-recording

### AI-функции
- [ ] LLM-перезапись работает (если LM Studio запущен с qwen3-4b-abliterated)
- [ ] Перевод работает в offline-режиме (ru↔es, en→ru)
- [ ] Диаризация работает (speaker labels в транскрипте)
- [ ] Realtime overlay появляется во время записи

### Экспорт
- [ ] История показывает записи в панели History
- [ ] Экспорт SRT: `python -m KrabEar.cli export --format srt`
- [ ] Экспорт CSV: `python -m KrabEar.cli export --format csv`
- [ ] Экспорт Markdown: `python -m KrabEar.cli export --format md`
- [ ] Экспорт JSON: `python -m KrabEar.cli export --format json`
- [ ] Экспорт Obsidian: `python -m KrabEar.cli export --format obsidian`
- [ ] Экспорт HTML: `python -m KrabEar.cli export --format html`

### REST API
- [ ] Health endpoint отвечает:
  ```bash
  curl -s http://localhost:5005/health | python -m json.tool
  ```
- [ ] Swagger UI загружается в браузере: `http://localhost:5005/api/docs`
- [ ] Prometheus-метрики отдаются: `curl -s http://localhost:5005/metrics/prometheus | head -20`

### CLI
- [ ] CLI tool работает:
  ```bash
  PYTHONPATH=$(pwd)/KrabEar python -m KrabEar.cli status
  PYTHONPATH=$(pwd)/KrabEar python -m KrabEar.cli health
  PYTHONPATH=$(pwd)/KrabEar python -m KrabEar.cli stats
  ```

### Резервное копирование
- [ ] Бэкап создаётся:
  ```bash
  scripts/create_stable_backup.command
  ```
  Проверить наличие timestamped-файла в `~/.krab_ear_data/backups/`

---

## 6. Регрессия и дейли-дравер

- [ ] `Run Regression Radar.command` — без регрессий
- [ ] `Run Daily Driver Validation.command` — пройден
- [ ] Отчёты сохранены в `docs/reports/`

---

## 7. Релизные артефакты

- [ ] Тег создан и запушен:
  ```bash
  git tag v2.X.Y && git push origin codex/krab-ear-v2 --tags
  ```
- [ ] PR из ветки в `codex/krab-ear-v2` создан или merge выполнен
- [ ] GitHub Actions CI зелёный (pytest + Swift build)

---

## 8. Пост-релиз

- [ ] Агент запущен с нового тега — работает штатно
- [ ] `KRAB_EAR_LOG_FORMAT=json` — структурированные логи пишутся корректно
- [ ] IPC socket доступен: `ls ~/Library/Application\ Support/KrabEar/krabear.sock`

---

## Быстрая сводка: места хранения версии

| Файл | Поле | Текущее значение |
|------|------|-----------------|
| `Krab Ear.app/Contents/Info.plist` | `CFBundleShortVersionString` / `CFBundleVersion` | `1.0.0` → обновить |
| `KrabEar/backend/service.py` строка ~703 | `"version": "..."` в ответе `handle_ping` | `"1.0.0"` → обновить |
| `docs/CHANGELOG.md` | Заголовок нового раздела | добавить |
