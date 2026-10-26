# Первый срез конструктора: тесты до извлечения

Основание: явный GO владельца 26.10 02:53 и принятые условия Claude
в `ear-constructor-slice1-20261026/RESULT_RU.md` (внешний artifact Ear).
База remote `66e44c5`, текущий owned HEAD `42ac50fa`; production не менять.

## Границы

Только тестовая characterization трёх local text stores. Backend не импортировать,
не создавать настоящие сервисы/потоки, не читать пользовательские данные.
Не трогать runtime, модели, клон, OrbStack, соседние проекты, conftest и семь
существующих constructor readers. Не применять production extraction.

## Выполнение

- [ ] Добавить `KrabEar/tests/local_text_stores_harness.py`: извлечь actual AST
  между plaintext IPC и audio callback; исполнить с recording factories и
  наблюдаемым свойством data_dir. Сохранить actual downstream provider/IPC AST.
- [ ] Добавить `KrabEar/tests/test_constructor_text_stores_contract.py`:
  порядок Vocabulary→Snippet→Phonetic; три разных sentinel reads; identity;
  ошибка каждого конструктора: тот же exception, только предыдущие attributes.
- [ ] Пиннить допустимый будущий helper: exact signature/body/no decorators,
  единственный direct call сразу после IPC. Hypothetical AST только в памяти;
  старый startup harness по-прежнему fail-closed на неизвестном helper.
- [ ] Проверить чувствительность изолированными source mutants: смена порядка,
  повторное использование data_dir, tuple assignment, пересоздание provider.
  Mutation RED ожидается на причинных assertions, исходник сервиса не записывать.
- [ ] После exact freeze и свежих Main/VG ACK выполнить bounded pack:
  два test-файла (новый + прежний startup prefix), `--noconftest`, ранний audit,
  network deny, свежие HOME/TMP, BLAS1, per-test30/outer180, receipt+owned drain.
- [ ] Линт/whole-diff review и сохранение evidence; новый прогон только после
  изменений/ошибок и согласования. Полный suite/parity/CI и live acceptance
  остаются отдельными гейтами перед будущим production extraction.

Ожидается GREEN на неизменённом source и RED на причинных mutants. Это tests-only
характеризация существующего поведения, а не исправление production bug.
