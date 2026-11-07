# Krab Ear — рельсы на следующие волны (2026-11-07)

Для исполнителей уровня «Sonnet/agy/Haiku». Каждая задача: цель → файлы → шаги → DoD → запреты.
Общие правила (обязательны для ВСЕХ задач):
- Работа только в отдельном worktree от `origin/codex/krab-ear-v2`, первая команда `git checkout -b <ветка>`.
- TDD: сначала красный тест (по правильной причине), потом фикс. Тесты — ПО ОДНОМУ файлу:
  `PYTHONPATH=$PWD/KrabEar "$HOME/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" -m pytest -p no:cacheprovider -W ignore -q KrabEar/tests/<file>.py`
- Гейт перед PR: узкие тесты + `flake8 --select=E9,F,W293` по изменённым файлам + py3.12-паритет (`scripts/pre_merge_py312_check.sh <files>`).
- НЕ трогать: `Krab Ear.app/` бинарники, прод-plist, release-worktree, рестарты backend. Не пушить в `codex/krab-ear-v2`.
- Каждая находка сверяется с кодом построчно; «не воспроизвёл» — так и писать.
- Отчёт: DONE/BLOCKED, headSha, список прогнанных тестов с итогом.

## Статус на 07.11
- #2190 (Claude): close()→WebhookManager.shutdown; сторож wake-word не путает стопор процесса с зависанием; тест lang-id без гонки patch.dict. Прод hotfix `480e7300` (база 5a09e630 + только эти фиксы).
- Прод отстаёт от main на 37 мержей (5a09e630 vs 4a84f6ac). Выкатка main целиком — отдельное решение владельца.

## R1. Подтвердить/закрыть KeyError('mlx') (если CI #2190 снова красный)  — модель: Opus
- Где: `KrabEar/tests/test_audio_lang_id.py::TestAudioLangIDConcurrentMlxLock`, `KrabEar/tests/conftest.py` (purge стабов ~стр. 760–780).
- Шаги: 1) скачать лог (`gh run view <id> --log-failed`), найти соседние тесты чанка; 2) воспроизвести ЧАНКОМ (тот же список файлов в одном процессе); 3) найти тест, оставляющий `mlx.core` без `mlx` или живой поток.
- DoD: детерминированный репро + фикс источника утечки. Запрет: `@skip` и ретраи вместо фикса.

## R2. REST STT: проверить #2183/#2184 на реальном STT  — модель: Sonnet (ревью Opus)
- Цель: доказать, что timeout REST STT не убивает backend и карантин не запирает сервис навсегда.
- Файлы: `backend/rest_server.py`, `backend/rest_inprocess.py`, PR #2183, #2184.
- Шаги: тест на временном dev-бэкенде (`scripts/run_e2e_smokes.command`, temp data-dir), WAV из `say -v Milena` → `/v1/stt/transcribe`; искусственный timeout.
- DoD: отчёт «до/после» с кодами ответов; проверено, вызывается ли `_begin_recording_maintenance` в проде (grep по AST, не подстрокой).
- Запрет: прод-сокет и порт 5005 прода.

## R3. Call Assist: сохранность summary (дизайн → код)  — модель: Opus (дизайн), Sonnet (код)
- Спека: `docs/superpowers/specs/2026-11-05-call-assist-stop-contract-proposal.md` (ветки codex/*-1105).
- Первый шаг БЕЗ Gateway: идемпотентный receipt в Ear — передавать стабильный `item_id` из summary/timeline_to_history в `add_history_item`; повтор с тем же id = no-op.
- DoD: тест «потерянный ACK + повтор → одна запись». Gateway-часть — только через бриф сессии VG.

## R4. Журнал допуска (lab v5) — закрыть три дыры тестами  — модель: Opus
- Файл: `KrabEar/core/maintenance_claim_journal.py` (ветка codex/ear-admission-protocol-lab-1105), тесты `offline_tests/admission_lab/`.
- Дыры: claim старого grant после CLOSED до revoke/seal; seal и незаявленные grant'ы; ошибка после создания файла блокирует create без пути восстановления.
- DoD: по красному тесту на каждую дыру + решение (fail-closed С путём деградации). В runtime не интегрировать.

## R5. Сторож wake-word: адаптерный stop() тоже стопор-aware  — модель: Sonnet
- `backend/openwakeword_adapter.py::stop()` ставит wedged по join(3с) без учёта стопора процесса (ревью #2190, п. «другой писатель wedged»).
- Идея: перед `self._wedged = True` проверить, не опоздал ли сам join (замер monotonic до/после >> timeout) → не ставить wedged, лог WARNING.
- DoD: тест с фейковым clock.

## R6. Аудиты ширины (agy / Gemini Flash)  — без git-мутаций
- Поиск сиблингов: другие сторожа/поллеры с одиночным наблюдением вместо гистерезиса (`*watchdog*`, `*monitor*`, `HealthMonitor.swift`).
- Результат — список кандидатов с file:line; гейт делает Claude.

## Порядок по умолчанию
R1 (если CI красный) → R5 → R2 → R3 → R4 → R6 параллельно всему.

## Решения владельца (07.11)
- Выкатка main целиком — только после R2 (живая проверка REST STT).
- Порядок задач — по умолчанию выше.
