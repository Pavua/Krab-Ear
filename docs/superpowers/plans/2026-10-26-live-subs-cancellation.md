# Отмена устаревших окон LiveSubsService

База: `66e44`, изолированная ветка `codex/ear-live-subs-cancel-1026`.
Scope (расширен координатором после source review): `live_subs_service.py`,
узкие owning-close гейты `service.py` и `rest_server.py`, новые focused-тесты,
три старых private lifecycle expectations в backpressure-тестах, эта карточка.
Runtime, модели, клон, OrbStack, сеть, backend-конструктор, commit/push запрещены.

## Дефект и решение

После bounded join reset/close теряют живой worker handle. Возвращающийся STT
продолжает перевод/emit и записывает completion уже после сброса.
Окно получает поколение сессии; reset/close инвалидируют поколение и пробуждают
ожидающих. Проверки перед STT, после STT, после перевода и перед completion
отсекают старые окна. Отдельный reentrant delivery gate линеаризует emit/reset,
без buffer/condition lock вокруг внешних callbacks и без lock вокруг ML.
Живой worker handle сохраняется после timeout. Reset и обычный stop оставляют
один reusable idle daemon, terminal close запрещает новый ingest и подтверждает
join булевым результатом. При False/exception BackendService не закрывает общий
transcriber. REST connection finally закрывает собственный экземпляр terminally,
purge singleton по-прежнему вызывает reusable reset.

Ресурсный trade-off: не более одного idle daemon на живой сервис; обработанное
окно/audio/result удаляются из локальных ссылок до idle wait (weakref regression).
Уже выполняющийся fake/native STT невозможно принудительно отменить: reset
подавляет последующие перевод/доставку/completion, а не обещает физический drain.
Уже опубликованные события/очереди/replay и downstream callbacks не отзываются.
Публикация линеаризуется с инвалидированием через отдельный delivery gate;
ML, buffer/condition/start locks во время внешнего emit callback не удерживаются.

## RED/GREEN

Тест: `KrabEar/tests/test_live_subs_cancellation_2026_10_26.py`.
Fake transcriber/translator блокируются на Event с bounded ожиданием.
Каждый тест освобождает Event и присоединяет только свой worker в finally.

1. Начать окно, дождаться входа fake STT, вызвать reset/close с нулевым join,
   проверить сохранённый живой handle, отпустить STT: перевод/emit/completion
   отсутствуют. После drain новая сессия успешно обрабатывается.
2. Заблокировать fake translate, reset, отпустить: нет emit/completion.
3. Переключить fake privacy во время STT/translate: нет позднего emit.
4. Обычный stop flush сохраняет событие и успешный результат.
5. Callback emit может вызвать reset без self-join/deadlock.

## Фактические offline receipts

Основная команда (cwd — этот изолированный worktree):

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=KrabEar '/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python' -m pytest --noconftest -o addopts='' KrabEar/tests/test_live_subs_cancellation_2026_10_26.py -q --tb=short
```

Python 3.14: исходный RED `6 failed, 1 passed` (exit1); после добавления нового
queued-generation schedule тот же RED `6 failed, 1 passed in 4.32s` (exit1).
AST BackendService.close False/exception: RED `2 failed, 9 deselected` (exit1).
REST finally: RED `1 failed, 12 deselected` (exit1).
Delayed ensure после close: RED `1 failed, 14 deselected` (exit1).
Последний focused GREEN: `15 passed in 0.30s` (exit0).

Отдельные зависимые файлы выполнялись последовательно, каждый в своём процессе,
той же командой с заменой имени файла и `--disable-warnings`:

| Файл | Receipt | Exit |
| --- | --- | --- |
| test_live_subs_service.py | 23 passed | 0 |
| test_live_subs_service_deep.py | 34 passed, 4 subtests passed | 0 |
| test_live_subs_hardening_W1770.py | 17 passed | 0 |
| test_live_subs_backpressure_F3_2026_08_12.py | 21 passed, 3 deselected | 0 |
| test_live_subs_single_pass_2026_08_12.py | 7 passed | 0 |
| test_live_subs_language_routing_2026_08_12.py | count UNKNOWN (poll потерял summary) | 0 |
| test_live_subs_prompt_leakage_2026_08_12.py | 11 passed, 1 warning | 0 |
| test_live_subs_stt_singleflight_p0e_2026_08_17.py | 6 passed | 0 |

Backpressure дополнительно использовал
`-k 'not TestBackendServiceCloseWiresLiveSubs'`, исключая три backend-import теста.
Зависимые receipts получены до финального closed-check в ensure; последний
focused15 включает этот фикс. Отдельных raw log файлов не сохраняли: таблица
перенесена из фактического stdout инструментов, не заменяет новый exact-SHA CI.

Ограничение изоляции: самый первый pytest без `--noconftest` загрузил общие
autouse fixtures и pyannote/torchcodec; затем использован `--noconftest`.
Prompt-leakage suite импортировал `core.engine.AudioEngine` с замоканными ML
leaves и выдал предупреждение torchcodec. Поэтому весь пакет НЕ является
доказательством отсутствия ML imports. Реальные модели/сеть/runtime не запускали;
focused файл использует fake transcriber/translator и AST-body BackendService.close
без импорта/конструктора backend/REST. Установка зависимостей не выполнялась.

Аудиты, Ubuntu parity, независимое review и CI — последующие гейты координатора.
Сборка, runtime/E2E, process/model drain и production release НЕ квалифицированы.
