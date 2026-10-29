# Конструктор: инициализация translation client

Продолжение шага 2 `docs/REFACTOR_PLAN_2026-10-25.md`. База
`165526430d6c4d378b69da5abfa71eb6076982c9`; изолированная ветка
`codex/ear-constructor-translator-1029`. Первый text-store slice уже закрыт.

Перенести только четыре соседних присваивания от `self.translator` до
`self.translator._settings_getter` в `_init_translation_client(self, store,
translator)`, один прямой вызов на прежнем месте перед `_start_time`.
Сохранить `translator or Translator()` (включая falsey injected object), одно
`str(store.data_dir)`, порядок присваиваний, identity кэша и bound getter,
проброс ошибок и частично собранное состояние. Никаких новых catches/cleanup.
Соседние STT, warmup, callbacks, settings/privacy и lifecycle не менять.

1. Characterization actual AST statements с инертными factory/property spies:
   injected/default/falsey translator, cache identity, порядок, ошибки factory,
   data_dir, str и setter. Baseline GREEN до переноса.
2. Shape gate требует ровно один helper и caller между прежними границами.
   RED на отсутствии helper, затем перенос и GREEN того же набора.
3. Проверить обратный inline полного модуля против точной базы; выполнить
   имеющиеся прямые static wiring assertions без импорта backend.
4. Независимое source review; локальный commit, документация и handoff.

Один процесс, stdlib unittest, без conftest/plugins/backend imports:
`/usr/bin/sandbox-exec -p '(version 1)(allow default)(deny network*)' /opt/homebrew/anaconda3/bin/python3 -I -B KrabEar/tests/test_constructor_translation_contract.py`
Каждое окно отдельно согласовать с Main/VG. Это узкая source/AST проверка:
реальные Translator/TranslationCache constructors, файловая персистентность,
полный BackendService и privacy E2E здесь не исполняются. Audit-all/parity,
hosted CI и live E2E остаются отдельными интеграционными гейтами.
Без push/PR/merge/CI, runtime/restart, моделей/клона/OrbStack.

Новый private helper добавляет виртуальный lookup и stack frame — обратный
inline AST не доказывает эквивалентность внешних subclasses/introspection.

## Локальный результат 29.10

Все четыре шага выполнены. Baseline 8 PASS до изменения production; затем
shape RED: 8 PASS / 1 FAIL именно из-за отсутствия helper. После переноса:
9 PASS (0.277 с). В отдельных subcases исполняются три прежних static getter
assertions; они не посчитаны как три дополнительных самостоятельных теста.

Отдельный финальный pack: `make -j1 audit-all` PASS (48.127 с), обратный inline
полного module AST совпал с точной базой (0.149 с), прежние три constructor
test-файла — 40 PASS (4.359 с). Это отдельные прогоны, не один combined run.
Два pytest config warning об отключённых timeout/timeout_method ожидаемы при
plugins OFF; внешнее ограничение 120 с действовало независимо от pytest.
Все три дочерние группы завершены/reaped/пусты, timeout не сработал.

Независимое fresh-context Astra whole-diff source review: GO, блокирующих
замечаний нет. Затем flake8 двух изменённых Python-файлов и diff-check PASS.
Исходники после проверок не менялись: service SHA256
`c0da6bf17d2def8abee894a655a5bafbf97a90be08d6a0781f60dea888a8e786`, test SHA256
`80e57b7d3fae21df5b4c1b0e9393c9768fc0735cb76f69795dba1b5d42d83d74`.

Квитанции и raw logs: локальный artifact `ear-constructor-translation-20261029`
текущего чата. Main/Gateway согласовали отдельные START/FINISH для каждого
окна. Локальный срез завершён; full backend/parity/hosted CI/live E2E и release
не заявляются. Следующий шаг — отдельная интеграционная приёмка после расширения
scope; повторять уже прошедшие тесты без изменений не требуется.
