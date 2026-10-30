# Поддержанный дательный день после Number → DateTime

База f100, isolated codex/ear-dative-dates-1030. REST branch не меняется.
Characterization17 puretext inputs: DateTime уже понимает первому/второму/третьему,
но Number превращает их в1-му/2-му/3-му, которых digital-day regex не принимает.
Например direct«к первому мая»→«к01.05», composed→«к1-му мая».

Scope: добавить только уже существующее numeric ordinal окончание `му` в digital-day
regex. Использовать прежние boundaries/calendar validation/partial-date guards.
NumberNormalizer, compound/year parser, другие словесные окончания не расширять.
Словесная «двадцать третьему» в direct DateTime уже частично меняется на baseline;
это существующая отдельная проблема. Composed numeric prefix должен сохраниться.

- [x] ONE puretext characterization; actual source, isolatedHOME/OSnetworkdeny, no app/ASR.
- [x] RED: test_dative_ordinal_date_composition_contract.py, seven methods.
- [x] Minimal regex suffix change; same test GREEN, existing ordinal/partial controls.
- [x] Composition/idempotence, adjacent numbers/wordyear/calendar/token negatives.
- [ ] Root independent exactdiff review before acceptance.

No publication/merge/install/restart/AGY/ps/provider/network/hardware/model work.

Receipts external ear-dative-dates-1030: CHAR17inputs; RED7methods/9causal subfailures;
GREEN7PASS; COMBINED36methodsPASS/errors0/skips0/threads1,0.061stest/0.103sparent.
All children reaped/groupempty/hashstable. Focused CI-option lint2paths/diffcheckPASS.
Source delta: regex `е|го`→`е|го|му` and comment only. No Number changes.
Root exactdiff review pending; no fullpipeline/ASR/hostedCI/publication/runtime acceptance.
