# E2/E5: локальный source/offline checkpoint

База: `139d228f6f3f0609b740deb801ecef129233d2fc`. Проверенный кандидат:
`e9327cea8b5a3a02e99ace197e90ade535bdae33`. Последующее изменение этого README
не меняет проверенные исходники и тесты. Результаты относятся к точному E932,
не к текущей удалённой ветке и не к production.

## Выбранный контракт

После таймаута действительно работающей direct-engine STT-задачи
`_STT_POISONED` закрывает новые admissions до перезапуска процесса. Проверки
до и после захвата singleflight не пропускают ожидавший запрос после poison.
Callback завершения Future освобождает primitive lock, но не снимает poison.
Старый response.close/exit70 сохраняется. Обычные success, completed error и
cancelled pending Future не устанавливают poison.

Callback-only кандидат был отозван: освобождение admission после первого drain
позволяло начать новую работу, которую затем мог оборвать поздний close старого
ответа. Поэтому прежнее обещание «после drain снова 200» здесь неприменимо.

## Причинное воспроизведение и пять контролей

Исходный test-only RUN1: три метода, один причинный assertion failure E2 и два
PASS. Наблюдались first504, second200 при ещё работающей первой Future и
незакрытом первом ответе. Ошибок стенда и оставшихся потоков не было. Это
доказательство admission gap, не параллельного MLX/GPU/OOM.

ONE GREEN1 точного E932: **5 PASS**, wall 0,196 секунды, exit0, дочерний процесс
собран, группа пуста, четыре owned worker завершены, хеши сохранены. Ошибок,
skips, нарушений guard и оставшихся потоков нет. Проверяются:

- E2: first504/second503 при работающей первой Future; после drain также503,
  второй worker не входит; old close70 сохранён.
- E5: характеристика двух отдельных бюджетов admission и Future.result.
  Логические admission+6/work+5 не доказывают нарушение whole-request deadline.
- Минимум/clamp10 и некорректные значения бюджета.
- Освобождение admission после обычного success/completed error.
- Отмена настоящей pending Future через минимальный executor seam; реальный
  scheduling pending pool этой проверкой не доказан.

Полные тела шести функций rest_server извлекаются AST; registration/auth/privacy
декораторы сняты только в offline-профиле. Реализации auth/privacy не проверены.
Future, Lock, ThreadPool и ContextVar настоящие; request/response/upload/metrics
синтетические. Ускорители Future.result(0) и lock.acquire(0) явно отличаются
от исходного RUN1; узкая эквивалентность свободного lock не означает одинаковое
расписание всего теста. Supervisor: Python3.12 -I-S-B, максимум5 workers,
30 секунд плюс TERM3/KILL3, запрет сети, моделей и application imports.

## Отдельные HTTP-контроли

ONE HTTP2 на точном E932: **2 PASS**, wall1,948 секунды, exit0, process reaped,
группа пуста, все19 pins стабильны, два owned worker завершены. Настоящий Flask
POST200 с синтетическим transcriber учитывает root и вложенную задачу: внешний
обработчик уже завершён, а child ещё running; после release subtree completed.
Engine dispatch binding проверен только AST. Стенд использует scratch settings,
не открывает настоящий listener и не обращается к моделям/сети.

Это не HTTP504 poison lifecycle test и не проверка реального ASR, auth/privacy,
ранней RU IPC ветки, native UI, полного server/subprocess drain или heard/audio E2E.
Существующие operation_ownership и11 unit-тестов побайтно не менялись от139d;
новый запуск11 не выполнялся и не суммируется с этими результатами.

Сырые RUN1/GREEN1 и HTTP receipts, immutable freezes и независимые отчёты
хранятся во внешнем артефакте ear-e2-e5-causal-139d-1030 текущего Ear-чата.
Все запуски согласованы отдельно; автоматических повторов нет. Hosted CI,
merge, deployment и runtime acceptance этим checkpoint не заявляются.
