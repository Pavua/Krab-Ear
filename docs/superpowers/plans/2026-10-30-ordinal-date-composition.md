# Порядковые даты RU/ES — test-only causal RED

Base `b79066373fbb6181e143513e7708ba3c777fb649`, оба предыдущих исправления сохранены. Production source не менять до causal RED. Runtime/models/providers/audio/ASR/config/pipeline order/publish запрещены в этом блоке.

Контракт: datetime_normalizer.py module doc и tests/test_datetime_normalizer.py требуют цифровую дату для «третье/третьего ноября», «primero de enero», «tercero de marzo». Фиксируем output_format=european, год не додумываем. NumberNormalizer заменяет ordinals на digit+suffix; гипотеза потери распознаваемости на стыке, ещё НЕ воспроизведена.

5 методов/13 проверок: direct4 baseline, composed4 на тех же фразах с intermediate в assertion message, digitdate2 RU/ES, digital date/time/code2, barehour1. Оба actual source исполняются целиком без core.__init__/pytest/conftest; synthetic only. Root source review/отдельный START обязательны. Подготовленный runner и3PINS: ear-ordinal-composition-20261030/PREPARED.json в артефактах текущего чата. Тесты НЕ запускались; no production source changes.

Если расхождение является допустимым документированным форматом — не объявлять баг без дополнительного основания. RU time units, ordinal general semantics, year inference, dialogue state отдельно.

## RED подтверждён; source candidate подготовлен
ONE RED5methods/13checks:4causal composition failures (3.º/1.º/3-е/3-го); direct4 иcontrols5PASS. Fullraw/receipt прочитаны; PID63212exit1/reaped/PGIDempty,0.148735583s,errors/skips/guards/threads0,3hashstable. FINISH/RUN0.
Минимальный DateTime patch: RU digitday принимает -е/-го только перед месяцем, левая граница исключает embeddedtoken; ES numericday принимает .º, callback убирает только этот суффикс. NumberNormalizer/order/config неизменны. Добавлено10negative datecontrols и2nondate ordinalcontrols. Fixture теперь7methods/25checks. Candidate combinedGREEN18methods/47checks (предыдущиеdate6/time5+ordinal7),5pins prepared, НЕ запускался. Source review/отдельныйSTART обязательны; старыйREDfreeze сохранён в PREPARED.json.

## Итоговый локальный GREEN

Final independent AGY SOURCE_GO conv `0a5d2cb2-4ece-4291-8b2a-54507e9703b7`, Gemini3.8FlashHigh; Root лично прочёл полный результат, actual5files/runner и выдал отдельный START. Замечание AGY про якобы stale sourcehash отклонено после сверки actualSHA: f91a086f... соответствует кандидату; pins не менялись вслепую.

ONE combinedGREEN18methods/47checks PASS: test0.037s,parent0.14416775s,child72172exit0/reaped/PGIDempty; failures/errors/skips/guards/apps/threads0; все5before/afterhashes stable. Fullraw/receipt лично прочитаны. FINISH/RUN0, окно свободно. Standalone ordinalGREEN/повтор прежних22checks отдельно не выполнялись.

SourceSHA256 `f91a086f4bdd3b01e4271cdba65b11106115932662086352d1ab80878439dbd1`; fixtureSHA256 `0c66a01f0c7047ad22a709e1be7015854d465c97a347bf85896b61b0c888c9f7`; runnerSHA256 `0db220845f02bcd94f4b5aa8cda7c3085faad977279f4de4a9983927a30e9bba`. Артефакты: `ear-ordinal-composition-20261030/combined-green/GREEN_STDOUT.log` и `GREEN_RECEIPT.json` в visualizations текущего чата.

Локальный scopedRED→GREEN завершён. Полные suites/Py3.12parity/lint/hostedCI/PR/merge/deploy/ASR/liveE2E не подтверждены. Следующая возможная карточка — source-only разбор ранее отмеченной RU timeunit composition, без автоматического запуска, смены pipeline order или конфигурации.
