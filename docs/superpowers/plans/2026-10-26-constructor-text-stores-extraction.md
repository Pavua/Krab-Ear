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

## Checkpoint перед PR, 26.10

Шаги 1–5 выполнены на production checkpoint `d4ec1f5b`: shape RED на
отсутствующем helper, затем 40 PASS на Python 3.14 и 40 PASS на Python 3.12;
обратный inline сохранил module AST, 16 статических audit-команд прошли.
Это узкий AST/reader pack, а не полноценное Ubuntu/backend parity.

Дополнительная проверка хранилищ выполнена отдельным внешним стендом после
независимого source review: 34 синтетические проверки механизма запуска PASS,
затем 11 исходных классов / 92 исходных метода PASS на Python 3.14.8.
Манифест фиксирует 11 канонических модулей; выборка не включает dispatch и
`backend.service`. Scratch HOME/TMP, отсутствие ошибок/skips/запрещённых
обращений/потоков, завершение процесса/группы и pre/post hashes проверены.
Freeze оригинальных 92 методов:
`ce6c7816badc2994a4393b79c9f225fded6da390b3c0708446af3c6c4c34084d`.
Квитанции и исходники стенда сохранены отдельно от diff приложения.

Перед PR остаются финальное whole-diff review и CI точного финального SHA.
Runtime/startup/E2E и релиз остаются отдельными гейтами. Позднейшее разрешение
владельца допускает лёгкие offline-проверки параллельно с START/FINISH;
для общего тяжёлого CI/build/model/lifecycle согласование нагрузки сохранено.
