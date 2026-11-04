# CI cleanup: удержать владение process group до сигнала

База `origin/codex/krab-ear-v2` @ `6e2043fb`; owned branch
`codex/ear-ci-group-owner-1104`. Source/tests/docs only. Публикация, merge,
deploy, production restart и действия над соседними процессами не разрешены
этой карточкой. Изолированные тесты создают только собственные короткие процессы.

## Причина и решение

Существующий `scripts/run_isolated_pytest.py` сначала делал `wait()` команды,
затем `killpg(pid)`. После reap PID/PGID может стать чужим; простой отказ от
cleanup оставил бы тестовых потомков. Поиск по scripts/tests не нашёл другого
готового механизма. `os.waitid` отсутствует в используемом macOS Python 3.12.

Parent запускает отдельного keeper/session leader с двумя pipe. Keeper ждёт
команду внутри своей группы, передаёт код результата и остаётся живым. Parent
не вызывает poll/wait keeper до group-kill; затем bounded wait (5s).
Control EOF после гибели parent и любые ошибки keeper приводят к cleanup
собственной группы в finally. Keeper проверяет PID=PGID=SID до запуска команды
и любого self-kill; служебные fd >2, команда их не наследует (`close_fds=True`).
Нестандартный SIGCHLD отклоняется до запуска; malformed/truncated status →125,
неуспешный запуск команды →127, сигнальный результат →128+signal.

Гарантия ограничена созданной process group. Потомки, самостоятельно вызвавшие
setsid, в неё не входят. Нет глобального поиска/kill по имени и сигналов после
reap keeper; неподтверждённый cleanup/reap не возвращает успешный 0.

## Проверки

- Causal RED 2: прежний after-reap signal и отсутствие SIGCHLD guard.
- Causal RED 2: stdio-alias pipe и ошибочный hidden keeper в caller group.
- Независимое Astra/Ultra review нашло race parent death между control check и
  status-write. Causal RED 1: BrokenPipe оставлял группу; общий finally закрыл.
- GREEN: 22 теста на Python 3.12 и 22 на Python 3.14; существующие 4 process
  tests и 5 workflow guards, новые ownership/fd/protocol/SIGKILL/startup cases.
  Реальные тесты подтверждают сохранность чужого процесса, уборку тестового
  потомка, отмену и control EOF при SIGKILL parent. Pure mocks отдельно проверяют
  порядок kill-before-reap, malformed status и отсутствие небезопасного запуска.
- `audit-all`, CI-policy flake8 и diff check: exit 0. Независимое whole-diff re-review GO.
- Это локальные проверки helper, не hosted CI нового SHA и не runtime acceptance.

## Публикация

Владелец 04.11 отдельно разрешил draft PR и hosted CI для этой ветки,
без merge/deploy. Код сохранён коммитом `bdba89be`.
Runtime, модели, Gateway/Main и существующие PR #2160–#2162 не менялись.
