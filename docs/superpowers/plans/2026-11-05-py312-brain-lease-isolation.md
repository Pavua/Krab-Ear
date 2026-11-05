# Python 3.12 harness: отдельный BrainLease для теста

**Goal:** каждый test-процесс parity harness получает собственный временный BrainLease, не используя default/shared или унаследованный lease caller.

**База:** `origin/codex/krab-ear-v2` = `823a99ced4e8dd64bef07663bfd4616dd26205f6`.
**Worktree:** `/Users/pablito/Documents/Codex/2026-11-05/task/ear-py312-lease-isolation`.
**Scope:** `scripts/pre_merge_py312_check.sh`, новый `KrabEar/tests/test_py312_harness_brain_lease_isolation.py`, эта карточка. В101 существовавшем worktree WIP по скрипту не найден.

## Подтверждение и ограничения

Во время ErrorBus RED тестовый BackendService попытался открыть общий `~/.openclaw/lm_studio_brain.lock` с O_RDWR; sandbox отказал. Последующие проверки были безопасны с ручным `KRAB_EAR_BRAIN_LEASE_PATH`. Harness должен задавать этот documented override сам, отдельно каждому child.

Production BrainLease, LM Studio, модели, Main runtime, Gateway `.env` и настоящие lock-файлы не меняются. Не запускать приложение, звонки, рестарты или model lifecycle. Не менять venv-build/purge поведение, worker snapshot/leak checks, timeouts и набор тестов. Сохранять чужой WIP, stage явные пути.

## План

- [x] RED: standalone stdlib tests запускают настоящий Bash harness с fake Python/ps, без импорта backend, venv rebuild, сети, настоящих workers и shared files. Проверить inherited sentinel, отсутствие env, разные test-paths, сохранение failure/leak verdict. Команда: `/tmp/py312/bin/python -B KrabEar/tests/test_py312_harness_brain_lease_isolation.py`.
- [x] В единственном pytest child invocation задать отдельный абсолютный lease path внутри уже существующего owned WORKER_SNAPSHOT_DIR. Parent env и sentinel не меняются; существующий trap очищает файл.
- [x] GREEN: повторить standalone tests; выполнить новый файл через обычный pytest Python3.12/noMLX с временным внешним lease и пустым SENTRY_DSN, чтобы pre-existing conftest не затрагивал runtime.
- [x] Bash3.2 syntax, CI-equivalent Flake8, Ruff для нового файла, diff-check и независимый review. Проверить отсутствие изменений других guard-команд.
- [ ] После review сохранить точный commit/patch и согласованную merge-order с #2185. До exact-head CI и согласования конфликтов merge не выполнять; runtime admission/drain HOLD.

## Результат 2026-11-05

- RED: 3 FAIL / 2 control PASS; дополнительный relative TMPDIR случай дал 1 FAIL / 5 PASS до нормализации, затем 6 PASS.
- Финальный standalone: 6 PASS; Ruff нового файла, Bash3.2 syntax, CI Flake8 и diff-check PASS.
- Финальный стандартный Python3.12/noMLX parity: 6 PASS, без новых workers, прежние timeouts и leak guards. MPLCONFIGDIR указывает на private warmed test cache; первый холодный импорт Matplotlib превышал прежний budget, budget не менялся.
- Caller sentinel сохранил точные байты. Унаследованный/общий BrainLease не используется child pytest.
- Независимое финальное source review: GO, без новых P1/P2. script SHA256 c6bcc0794f38784f4a55e41251457a7b3a59ad82009528a15bf524229e0e7390; test SHA256 a63f5b481c01b2a6793d42639968038e662d1017d73e958c0fb47558a47f7a87.
- Ear #2185 слит в f36721157392ff137940c91973ba931cc0c5f5aa; этот независимый harness-срез пока локальный, без PR/merge/runtime-активации.
