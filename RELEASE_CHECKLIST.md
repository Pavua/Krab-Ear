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

## v2.0.5 Candidate — PRs для включения

| PR | Wave | Описание |
|----|------|----------|
| #619 | W525 | Extract analysis module |
| #622 | W545 | Backend hardening |
| #624 | W547 | Test coverage |
| #625 | W546 | Core fixes |
| #623 | W554 | Backend service cleanup |
| #628 | W568 | Error handling improvements |
| #629 | W577-578 | Dual-wave stability patch |
| #630 | W575 | Backend refactor |
| #631 | W611 | Service extraction |
| #632 | W632 | Test coverage sweep |
| #634 | W635 | Dead handler audit (86 active verified) |

### Шаги верификации перед тегом v2.0.5

- [ ] Все PR из таблицы выше смержены в `codex/krab-ear-v2`
- [ ] `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -q` — 0 failures
- [ ] `grep -c '"method"' KrabEar/backend/service.py` — сверить с 86 активными хендлерами
- [ ] `swift build -c release` в `native/KrabEarAgent` — без ошибок
- [ ] Smoke: hotkey → запись → транскрипция → вставка
- [ ] `git tag v2.0.5 && git push origin v2.0.5`

---

## 6. Пост-релиз

- [ ] Агент запущен с нового тега — работает штатно
- [ ] `Run Daily Driver Validation.command` — пройден
- [ ] `Run Regression Radar.command` — без регрессий
- [ ] Отчёты сохранены в `docs/reports/`
