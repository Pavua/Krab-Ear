# ES date token boundaries — локальный causal fix

Stacked base `80f86c26af021b23a8e965c73331b3e374432635`; предыдущий timefix сохранён отдельным коммитом. Только исходники, синтетические offline-тесты и документация. Runtime/models/providers/audio/ASR/CI/publish не менялись.

## Контракт и исправление

QA0417/A17/117 de noviembre сохраняются целиком; maravilla/noviembrecito не считаются месяцами; year20260/year2026abc не приводят к частичной нормализации фразы. Валидные17/19Nov2026 иyearless3Nov нормализуются, прежний16:35fix сохраняется.

Минимальный source diff: левая граница слова перед днём, правая после месяца; числовой токен года захватывается целиком и принимается только при ровно четырёх цифрах. При некорректном годе callback возвращает исходный match. Проверка дня и форматы вывода прежние. Fixtures не ослаблены.

## Проверки

ONE RED: 6 методов /11 проверок, 7 ожидаемых assertion failures; три контрольных метода PASS. Ошибок/skips/guard violations/threads0, child48120 exit1 reaped/PGIDempty, elapsed0.147206791s; хеши стабильны. Полный stdout прочитан.

Независимое final AGY review: Gemini3.8FlashHigh, conversation eb7a9ffb-902d-4d6d-aa06-fde887eb65ab, actual SUCCESS/nonempty SOURCE_GO по подтверждению координатора Root. Root лично прочёл review, actual diff и combined runner, проверил четыре PINS и выдал отдельный START.

ONE combined GREEN: 11 методов /22 проверки PASS, включая существующие5time methods/11checks на новом DateTime source и6date methods/11checks. Actual модули целиком, два отдельных ModuleType test namespaces, Number→DateTime composition сохранена. Child56093 exit0/reaped/PGIDempty; ошибок/skips/guards/threads0, test0.026s,parent0.138731708s; четыре хеша before/after стабильны. Полный stdout/receipt лично прочитан. FINISH/RUN0; standalone dateGREEN НЕ запускался.

Artifacts: `/Users/pablito/.codex/visualizations/2026/10/21/01a15c4e-ea0a-7d80-b807-f6c6224a9030/ear-date-boundaries-20261030/`; combined runner/receipt/rawlog в `combined-green/`. RunnerSHA256 `14214a174e870105eaeb214a7f9c53fc26d3ddcb4c42f3c490bc48b0f93a7df2`. SourceSHA256 `6d824a79d56eb91d328af427ec721f96e63caf67376cca5a5cce29dcaa9fd1a4`.

## Граница результата

Локальный узкий RED→GREEN блок завершён. Полные исходные suites/Python3.12 parity/lint/hosted CI/PR/merge/deploy не выполнены. Нормализатор не выбирает окончательную дату коррекции, не подтверждает врача/клинику и не додумывает поля. Ordinal/RUunit composition остаётся отдельной карточкой. Следующий шаг — согласовать интеграционный/CI scope и окно с Root/Main/Gateway; не запускать новые пакеты автоматически.
