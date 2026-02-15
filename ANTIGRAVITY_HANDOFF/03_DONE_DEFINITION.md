# Definition of Done (для Antigravity)

## Обязательно

1. Boundary-check:
- `Run Agent Boundary Check.command antigravity` -> без нарушений.

2. Сборка/проверка:
- для UI задач: `swift build -c release --package-path /Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent`
- для Gateway iOS: билд/запуск проекта iOS без runtime-crash
- для OpenClaw: локальный smoke выбранных команд

3. Отчёт:
- что изменено;
- какие команды запускались;
- что осталось открытым.

## Нельзя считать завершением

- правки в чужой backend зоне;
- отсутствие boundary-check отчёта;
- «сделано, но не проверялось».
