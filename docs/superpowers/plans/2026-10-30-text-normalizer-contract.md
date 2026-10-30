# Ear: synthetic RU/ES text contracts

Base `165526430d6c4d378b69da5abfa71eb6076982c9`; prior `6d02fe73` preserved. Source/review only. No tests/build/models/runtime/providers/audio/publish before independent source GO and coordinated slot; Crypto owns heavy resources.

Existing mechanism: `engine.py` applies optional NumberNormalizer then DateTimeNormalizer, stateless post-STT/pre-LLM. Text normalization is not extraction, dialogue correction resolution, caller authentication or ASR.

Synthetic invariant: preserve LunaTest, code leading zeros QA0472→QA0427, explicit 17Nov2026→19Nov2026 correction provenance and 16:35. Do not invent doctor/identity/missing fields from truncated input; unknown/askrepeat belongs to downstream dialogue policy. Preserve both dates in normalized text; do not require this normalizer to collapse corrections.

Confirmed by the bounded causal RED below: ES alas_pat consumes the hour prefix of `a las 16:35`, emits HH:00 and leaves :35. This is a text-normalization defect, not evidence about ASR or a live call.

Sanitized packet and hash manifest: external `ear-text-contracts-20261030/{REVIEW_BRIEF.md,AGY_PACKET.txt,MANIFEST.json,source/}`. Includes two actual modules, their original tests and bounded actual engine-order excerpt. No private content.

Next: approved AGY static review; source findings→one minimal causal test after exact freeze+START; fix only proven defect in separate bounded RED/GREEN and independent review. No REST503/AST checkpoint rerun.

## RED квалифицирован / минимальный fix подготовлен

ONE START: 5 методов, 5 причинных subcase failures (16:35→16:00:35; 9:35→09:00:35; 1234→12:0034; 9:5→09:00:5; 16:350→16:00:350). Остальные четыре метода проходят. Child0.020s/parent0.1304s, exit1/reaped/PGIDempty, errors/skips/violations/forbidden imports/threads0, три source hashes стабильны; raw receipt/stdout лично прочитаны.

Fix ограничен `(?![\d:])` непосредственно после digit-hour группы alas_pat: запрет частичного совпадения цифрового токена/HH:MM и отката к одной цифре. PM/timezone/dialogue/date/code/ordinal policies не меняются. Date source SHA964b4965f7c13db5b87a4733a4efb2c403bee4b8adcfe43f9df055b5294807fc; test02fc5667 unchanged. Отдельный GREEN runner подготовлен с новым pin; требуется независимый source review + новый START. GREEN не запускался; parity/CI/merge/release отсутствуют.

## Независимый source gate

AGY Gemini 3.8 Flash High, conversation `1f15a373-2c67-4a8f-bcc6-49fa20b65559`: SOURCE_GO для exact Date SHA964b4965 и unchanged test SHA02fc5667. Root и Ear лично прочитали ответ и ROOT_ACCEPTANCE.json. Это статическое ревью, не новый тестовый прогон. Source scope — только immediate digit/colon boundary; пробел перед двоеточием, date/code boundaries и ordinal composition отдельно, не объявляются исправленными.

GREEN runner SHA02c522501c26528f906d89e1341e7b1c0e81cb7fb4975675bec8e3cb37b9f36e; тот же 5-method/11-assertion pack и guard. Следующий шаг: ONE GREEN только после отдельного START, лично прочитать stdout/receipt, затем FINISH. После GREEN — зависимые исходные date/time tests и Py3.12 parity в отдельном согласованном окне, финальное review/lint, явные git paths и local commit; публикация/CI только по очереди. Релиз/ASR/live call acceptance в этот блок не входят.

## GREEN квалифицирован, 30 октября

ONE fresh START: 5 методов / 11 leaf assertions PASS, failures/errors/skips/violations/forbidden modules/threads0; child39260 exit0/reaped/PGIDempty, timeout/cleanup отсутствуют. Child0.019s, parent0.132955s; все три frozen hashes до/после совпали. Полный GREEN_STDOUT.log и GREEN_RECEIPT.json лично прочитаны до FINISH; окно освобождено, повторов нет.

Локальный causal slice завершён. Оригинальные dependent date/time suites, Python3.12 parity, lint/CI и публикация ещё не выполнены; готовность релиза/ASR/живых звонков не заявляется. Следующий безопасный шаг — согласованный stdlib pack существующих date/time тестов и parity, без backend/models/audio.
