# Релизный чеклист Krab Ear

Прогоняется перед каждым тегом. Автоматическая часть —
`scripts/run_release_checklist.command`; ниже — полная ручная развёртка.

---

## 1. Предварительные проверки

- [ ] venv активен и python доступен:
  ```bash
  source .venv_krab_ear/bin/activate && python --version
  ```
- [ ] Зависимости актуальны:
  ```bash
  pip install -r KrabEar/requirements.txt
  ```
- [ ] Свободное место ≥ 2 ГБ (модели MLX занимают ~1.5 ГБ)
- [ ] Accessibility-права выданы для `native/runtime/KrabEarAgent`
- [ ] Нет незакоммиченных изменений: `git status`

## 2. Сборка

- [ ] Swift agent собирается в release:
  ```bash
  cd native/KrabEarAgent && swift build -c release
  ```
- [ ] Shell-скрипты без синтаксических ошибок:
  ```bash
  find . -maxdepth 2 -name "*.command" -print0 | xargs -0 -n1 zsh -n
  ```

## 3. Тесты

- [ ] Unit-тесты backend (все проходят):
  ```bash
  PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v
  ```
- [ ] Smoke-тест релиза:
  ```bash
  scripts/run_smoke_release.command
  ```
- [ ] Полный автоматический чеклист (lint + build + tests + smoke):
  ```bash
  scripts/run_release_checklist.command
  ```
  Отчёт появится в `docs/reports/release_checklist_*.md`.

## 4. Функциональная проверка (ручная)

- [ ] Запустить агент: `Start Krab Ear.command`
- [ ] Hotkey (Right Option) — начинает/останавливает запись
- [ ] Транскрипция вставляется в активное окно (Accessibility paste)
- [ ] Панель истории открывается и показывает записи
- [ ] REST API отвечает: `curl -s http://localhost:5005/health`
- [ ] Повторный hotkey-цикл (3–5 раз) — нет ghost-recording

## 5. Релизные артефакты

- [ ] Версия/дата обновлены в ROADMAP (если применимо)
- [ ] CHANGELOG дополнен
- [ ] Коммит: `git add -A && git commit -m "release: vYYYY-MM-DD"`
- [ ] Тег: `git tag vYYYY-MM-DD && git push origin main --tags`

## 6. Пост-релиз

- [ ] Агент запущен с нового тега — работает штатно
- [ ] `Run Daily Driver Validation.command` — пройден
- [ ] `Run Regression Radar.command` — без регрессий
- [ ] Отчёты сохранены в `docs/reports/`
