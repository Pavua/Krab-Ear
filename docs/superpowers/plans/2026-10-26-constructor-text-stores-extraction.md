# Первый срез: извлечение трёх text stores

Основание: прямой GO владельца 26.10 03:14, карта и условия Claude review,
characterization `eba5a8eb` (32 PASS + четыре причинных mutation RED).
Owned WT `ear-startup-characterization-1025`, remote base `66e44c5`.

## Ограничения

Ровно три присваивания VocabularyStore → TextSnippetService → PhoneticVocabService
переносятся в `_init_local_text_stores(self, store: StateStore) -> None`.
Один синхронный вызов непосредственно после `_plaintext_export_ipc`.
Три отдельных `store.data_dir`, immediate assignments, exception propagation.
Settings/privacy/socket/recorder/rewriter/warmup и downstream wiring не менять.
Никаких runtime, models, clone, OrbStack, dependencies, config, restart/deploy.

## Шаги

1. Тест actual helper required и сохранение прежних behavior assertions.
   RED на прежнем source: `reviewed helper extraction is required`.
2. Startup harness подключает только exact AST helper через строгий allowlist;
   unknown helper и unsafe shape отклоняются до исполнения.
3. Минимальный production diff: helper из прежних трёх Assign + один direct call.
4. GREEN: прежние 10 startup, 23 slice tests, семь исходных reader assertions
   в изолированной AST-обёртке без backend import (это не их полные suites).
5. Module AST после обратного inline совпадает с `eba5a8eb`; source review,
   аудит и parity квалифицируются отдельно, запуск каждого bounded окна только
   по точному freeze и свежим Main/VG ACK. Не ставить backend dependencies.

Команды изолированного runner/guard и immutable phase hashes сохраняются
в artifact `ear-constructor-extraction-20261026`, без production lifecycle.
До изменения source после RED — новый GREEN freeze и fresh ACK.
