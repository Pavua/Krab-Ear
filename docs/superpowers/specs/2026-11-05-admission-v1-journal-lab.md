# Изолированная связка v1 и журнала

Статус: **учебный эксперимент**, не production transport/auth, не допуск к
работе, не подтверждение drain и не восстановление после crash. Живые данные,
процессы и сетевые сервисы не используются. Версия v5 создаётся только как новый
тестовый файл; v4 и v5 не мигрируются и не открываются друг другом.

В v5 канонический запрос и результат хранятся в одной строке SQLite одной
транзакции. На повторе исходный request сравнивается до проверки revision;
отличающиеся данные вызывают конфликт. Owner epoch хранится в control и
сверяется с каждой записью и закреплённым контекстом. Journal ID и schema
проверяются при каждой команде. При проверке после повторного открытия журнал
inspection-only и UNKNOWN; восстановление права на исполнение отсутствует.

Адаптер имеет одного тестового владельца и не отдаёт permit в ответе. Первый
тестовый permit хранится только в памяти владельца, повтор не создаёт новый;
неудача или прерывание при сохранении удерживает UNKNOWN. Никакая квитанция не
восстанавливает потерянный permit и не доказывает завершение работы.

Отдельный Python-процесс в тесте намеренно не отправляет ответ после commit и
перед повтором сообщает тестовому harness только синтетический маркер. Проверки
требуют прежний claim ID, один grant, один claim, две записи и один удержанный
тестовый permit. Negative controls подменяют ID или счётчик и должны провалить
тот же oracle. Это не тест реального сетевого транспорта или падения процесса.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/KrabEar" /tmp/py312/bin/python -m pytest \
  offline_tests/admission_lab/test_v1_journal.py \
  offline_tests/admission_lab/test_response_v1.py \
  offline_tests/admission_lab/test_revoke_contract.py \
  offline_tests/admission_lab/test_codec.py \
  offline_tests/admission_lab/test_owner.py \
  KrabEar/tests/test_maintenance_claim_journal.py \
  KrabEar/tests/test_maintenance_synthetic_submit.py -q --tb=short -o addopts='' \
  --timeout=60 --timeout-method=thread -p no:cacheprovider
```

Не доказаны production authentication, fencing, межъязыковой parser и duplicate
keys для нового протокола, independent writers, crash durability, восстановление,
учёт настоящих workers/recording и их persistence, или итоговый drain. Повтор
старого receipt не описывает текущее состояние; статус lab не даёт права на stop.
