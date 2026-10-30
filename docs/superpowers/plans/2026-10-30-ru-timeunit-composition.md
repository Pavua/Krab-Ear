# RU timeunit composition — test-only RED preparation

Base ab640f65064e620e7328959a73f4bd0f4bd314e3. Production source unchanged. No runtime/models/config/pipeline reordering/providers/ASR/CI/publish.

Источник гипотезы: Number unit mapping час*/минут*→ч/мин, DateTime accepts fullwords only; engine runs Number→DateTime. Existing datetime tests document в десять часов→10:00, девять часов утра→09:00, семь часов вечера→19:00. Fourth explicit clock в девять часов тридцать минут утра checks minute composition; execution pending, not assumed PASS.

5methods/16checks: direct4, composed4 (intermediateNumber in assertion), duration2, digitalatoms2 RU/ES, invalid/code4 unchanged inputs. Duration preservation is proposed negative control, not asserted historical product guarantee. No unanchored Nч universalclock expectation. If negative cases fail on an old independent defect, report separately before expanding fix scope.

Actual whole Number+DateTime sources, isolated ModuleType/stdlib harness without backend/core.__init__/pytest/conftest. Runner profile inherited ordinalRED, only3PINS/testpath/checkcount updated. Artifact PREPARED.json and RED_TEST_SOURCE.py under ear-ru-timeunit-qualification-20261030 in current chat visualizations. Tests NOT RUN; separate Root source review and START required.

## Causal RED и source-only candidate
ONE RED5methods/16checks:4compositionfail, direct4+controls8 PASS; rawstdout/receipt лично прочитаны. PID80511exit1/reaped/PGIDempty,0.152113458s,errors/skips/guards/threads0,all3hashstable. FINISH/RUN0, окно свободно.

Подготовлен отдельный abbreviated RU time regex после прежнего full-word: numeric ч/мин с token boundaries, обязательное «в» либо day-period marker в callback; тот же _repl_time/_valid_time. Optional minutes не могут откатиться к часам при длинном numeric minute token. Number, full-word pattern, pipeline/config неизменны. Добавлено10negative controls (unanchoredunits/durations/750minutes/longhour/code/unitprefix/markerprefix/24hour). RUfixture6methods26checks, combined24methods73checks candidate with6pins. GREEN NOT RUN; independent source review andseparateSTART required. СтарыйREDfixture сохранён byte-exact в RED_TEST_SOURCE.py. Fullsuites/parity/CI/runtime/ASR не подтверждены.

Дополнение к candidateGREEN по запросу Root: existing pure TestDateTimeNormalizerRuTime извлекается целиком exactAST из test_datetime_normalizer.py (7methods/7checks), globals unittest+actualDateTimeNormalizer only. Canonicaltestmodule imports не исполняются. Итого31methods/80checks,7PINS/readallowlist, runnerSHA92bc597f4990639373ff8fd226b495053963dd09a67f37532a27be539e3bc153. GREEN NOT RUN.

Finalcandidate update: RED_TEST_SOURCE.py82f773 verified immutable. Added4negativecontrols123min/chXYZ/minus/embeddedcode; invalidminute guard blocks мин* continuation before partialhourreplacement. Currentsource421558b4...,fixture23a55daa...,runnere313b87919a09bc8c29303c8984ed595b07c69ea3c0522181106fbcf9f167f64.31methods84checks (RU6/30+previous18/47+existing7/7),7PINS. Finalreviewpacket REVIEW_BRIEF.md +review-source/ prepared; GREEN NOT RUN.

## Итог: локальный GREEN завершён

FinalAGY actualSOURCE_GO conv94bc953b-1577-464d-ae38-7d99656aad8a (Gemini3.8FlashHigh), Root лично прочёл fullreview/diff/runnerdelta и сверил7PINS+runnere313b879 перед отдельнымSTART.

ONE unchanged combinedGREEN31methods/84checks PASS; test0.032s,parent0.197142792s,child90340exit0/reaped/PGIDempty. Failures/errors/skips/guards/apps/threads0; cleanup_signals[],interruptedFalse; все7before/afterhashes stable. Fullraw/receipt лично прочитаны отдельно. FINISH/RUN0, окно свободно. Это24syntheticmethods/77checks + existingpureRUtimeclass7methods/7checks, не полный originaltestmodule.

Artifacts: ear-ru-timeunit-qualification-20261030/combined-green/GREEN_STDOUT.log и GREEN_RECEIPT.json; source421558b4...,fixture23a55daa...,runnere313b879 unchanged. Scoped causalRED→GREEN закрыт локально. Number/order/config untouched; runtime/models/providers/audio/clone/OrbStack не менялись.

Полные suites/Py3.12parity/lint/hostedCI/PR/merge/deploy/ASR/liveE2E не подтверждены; публикации нет. Следующий шаг — согласовать интеграционный scope/очередь с Root/Main/Gateway. Новые тесты после этого checkpoint не начинать автоматически.
