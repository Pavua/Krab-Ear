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

## 1а. Pre-deploy (только для production-тега)

Перед тегом для production-деплоя обратитесь к `docs/DEPLOY_V2.0.5.md` —
там описаны: проверка migration, бинарный drift-check (bundle vs runtime),
launchd plist reload, дешифровка `.env` секретов и rollback-план.

> Чеклист ниже (разделы 2–6) обязателен для **всех** релизов.
> `docs/DEPLOY_V2.0.5.md` — дополнительный слой для production-деплоя v2.0.5+.

## 2. Сборка

- [ ] Swift agent собирается в release:
  ```bash
  cd native/KrabEarAgent && swift build -c release
  ```
- [ ] Shell-скрипты без синтаксических ошибок:
  ```bash
  find . -maxdepth 2 -name "*.command" -print0 | xargs -0 -n1 zsh -n
  ```
- [ ] IPC_API_REFERENCE.md актуален — проверить drift от `origin/codex/krab-ear-v2`:
  ```bash
  git diff origin/codex/krab-ear-v2 docs/IPC_API_REFERENCE.md
  ```
  Если есть значимые изменения в хендлерах — перегенерировать документ (W745 regen,
  см. PR #678 для контекста). Незначительный drift (форматирование) допустим.

## 3. Тесты

- [ ] Unit-тесты backend (все проходят):
  ```bash
  PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v
  ```
- [ ] Orphan-imports audit — нет сиротских импортов в service.py (W750):
  ```bash
  python3 scripts/audit_orphan_imports.py
  ```
  Скрипт завершится с ненулевым кодом и перечислит нарушения, если импорт
  модуля присутствует, но ни один его символ не используется в `service.py`.
- [ ] Wiring-tests guard — все extracted services покрыты dispatch-тестами (W762):
  ```bash
  PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -k "dispatch" -v
  ```
  Убедиться, что ни один extracted service не пропущен в `test_dispatch_invariants_*.py`.
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
- [ ] Sentry release-tag верифицирован (W704): в Sentry Issues следующее событие
  после деплоя должно иметь тег `release=krab-ear@<new_version>`. Проверить:
  1. Открыть Sentry → Issues → любой новый event после деплоя.
  2. В поле Tags убедиться, что `release` = `krab-ear@v2.0.X` (не старый тег).
  3. Если тег не обновился — перезапустить backend (он читает версию из
     `CFBundleVersion` / `__version__` при старте, а не из env-переменной).
  Источник правды: `KrabEar/backend/observability.py` + `SentryConfig.swift` (PR #241).
