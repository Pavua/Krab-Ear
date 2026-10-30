# Local REST operation subtree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task after Root profile GO.

**Goal:** Retain actual adapter executor ownership after outer REST callable returns, without changing normal call admission.
**Architecture:** Stdlib OwnedOperations publishes root/child identities before submit; ContextVar binding explicitly installed inside worker wrappers; actual engine adapter dispatch calls shared submit_owned. Existing ActivityReservations stays advisory and unchanged. Full server drain remainsunknown.
**Tech Stack:** Python3.12+,stdlib threading/contextvars/Future/uuid; existing Flask scratch factory, no dependencies added.
**Spec:** Accepted REST_NESTED_DESIGN/DESIGN_RU.md + CROSS_PROJECT_DRAIN_DESIGN_REVIEW.json.
**Base:** exact fresh remote f100ced4668531b356fd5b528504dd9bdfd4e756; isolated codex/ear-owned-subtree-1030. C77 publicpacket stays independent; no cherry-pick and no file mutation there.

## Global Constraints

No runtime/install/restart/network/audio/models/AGY/UI/ps/security/endpoints/grants/persistentaccess/publish. Ordinary admission ALWAYSopen; no new call blocking/rerouting. No maintenancefence implemented yet. scope_complete=false, fence_result=null always. Exclusions upload/auth/parse,unregisteredotheradapterpaths,RUIPC,WS,TTS,batch,UI,Earinternalwarmup. Internal metadata-only snapshot, no transcript/audio/paths. Old20REST43text4batch not rerun. Source fields are not server authority.

## Exact files / responsibilities

Create KrabEar/core/operation_ownership.py: stdlib operation registry+opaque token+worker context+submit helper. No app imports/global startup workers.
Modify KrabEar/backend/rest_server.py: import registry; separate _REST_OPERATIONS; root registration immediately before actual POST executor submit; submit wrapper delegates existing _REST_ACTIVITY.run and sttbudget callable unchanged; follow root Future for cancelled-before-start/unknown submission. Not exported as endpoint/health success field.
Modify KrabEar/core/engine.py: only native fallback adapter block _transcribe_with_fallback_impl at old _pool.submit(adapter_fn,**_adapter_kwargs) → submit_owned(_pool,adapter_fn,**_adapter_kwargs). Keep budget/result/cancel/shutdown behavior and error mapping.
Create KrabEar/tests/test_operation_ownership.py: real control-object unit/causal lifecycle cases.
Create KrabEar/tests/test_rest_operation_ownership.py: narrow Flask boundary receipt + source AST binding assertions, no ML import. Stand artifacts remain external until reviewed.
Create docs/superpowers/plans/2026-10-30-rest-operation-subtree.md (this card).

## Proposed exact interface

OperationToken frozen(epoch,operation_id); registry accepts own tokens only.
OwnedOperations(scope_id='rest_post_native_adapter',capacity=256)
  admit_root()->OperationToken|None
  register_child(parent:OperationToken)->OperationToken|None
  run(token,fn,*args,**kwargs)->Any (sets own ContextVar, calls fn, finally records callable terminal+seals registration; exceptions propagate unchanged)
  follow_future(token,future)->None (cancelTrue-beforestart terminal; done exceptions terminal only if known actual wrapper finished; unresolved/invalidcallback ->unknown)
  mark_unknown(token)->None
  snapshot()->dict
submit_owned(executor,fn,*args,**kwargs)->Future: uses current registry/token; register_child BEFORE executor.submit; wrapper installs exact child context; returns actual Future. Without ownership context calls executor.submit directly preserving othercallers. Submission ambiguity retainsunknown (never invent cancelled/complete), exceptions re-raised unchanged. Actual executor Future callbacks may release pendingcancelled-beforestart child; cancelFalse keepsrunning. Context never implicitly copied betweenthreads.
Registry snapshot: schema_version1,scope_id,owner_epoch,revision,scope_completefalse,admissionopen,fence_resultnull,coverage_lost,exclusions,operations[{operation_id,parent_operation_id,admission_seq,callable_state:running|completed|unknown,registration_sealed,state:running|completed|unknown}]. admitted but not started callable_state running means ownedpending; no fake execution claim. Separate queued field if needed review, not infer started fromadmission. Parent state completed only callableterminal+sealed+every childcompleted; unknown dominates subtree completion. Success/error outcome independent from owner completion. No resume/closeAPI/fenceimplemented.
IDs serverassigned UUID. Revision increments transitions, not incarnation; process/fork check replaces lock/state/epoch conservatively with prior_owner_unknown/coverage_lost, oldepoch callbacks ignored and cannot settle new debt. No callbacks mutate foreign tokens; guard against wrongregistry with epoch. No pending/unknown eviction. Capacity overflow runs ordinaryrequest untracked, returnsNone token and permanentlycoverage_lost; cannot silently claim authority. Bounded terminal retention/compaction must preserve descendant aggregates; initial simplest noeviction, saturatesunknown after256 records rather than unboundedRAM. Later tuning separate.

## Review Focus

1. Child accepts+starts before outertimeout; cancelFalse; root remains owned despite outerreturn.
2. Submitted callable raises; actual wrapperfinally seals; exception result≠unknown owner whenthreadfinished.
3. Submit raises after ambiguousdispatch; retainedunknown, not dropped/terminal; actual spawnedworker cleanup guaranteed in test.
4. Parentseal races childregistration; never attach unnoticed toterminal parent. Registered child can register descendants after rootsealed until ownseal; realbarriers test.
5. Foreign epoch/duplicatecallback/overflow/cancel-beforestart; no currentdebt cleared and ordinaryadmission unchanged.

## Task1: exact RED stand preparation (NO RUN before Root GO)

- [ ] Write named tests against actualnewAPI, no fake bool registry. Missingmodule/API at f100 is explicit unimplemented-contract RED, not proof existing regression. Root may reject this RED classification before anyrun.
- [ ] Create frozen external parent/child; CPython -I-S-B; stdlib registry cases noimports backend/ML. 25sec childdeadline, ownedTERM/KILL/reap/PGIDempty; disposableHOME/TMP/networkdeny; audits selectedsource/readruntimeprefix/scratchwrites honestly not universalsandbox; bounded max20 realthreads, nofixedsleep. Eventbarriers cleanupfinally and executor.shutdown wait only after release.
- [ ] Root selected ONE availability assertion RED: API absent at base, exactly1 failure/0errors, not causal runtimebug. No10ImportErrors. Full parent/child/futureUtests frozen before RootGO. After confirmed RED, implementation authorized; then exact source/profile review before GREEN. Causal semantic proof on GREEN is U2/U3/H1 with actualhelper+realnested executor/Event; old advisoryunknown stays unchanged.

Named unit methods (10, actualunittest methods, subcases not inflated):
U1 test_root_and_child_ids_revision_and_sealed_completion
U2 test_outer_return_does_not_release_running_adapter (realexecutor+Event, actualsubmit_owned; rootcallablereturns whilechildblocked)
U3 test_nested_grandchild_remains_owned_after_parent_seals (real2executors, latechild descendant beforeownseal)
U4 test_exception_terminalizes_real_callable_and_preserves_exception
U5 test_cancel_before_start_releases_only_never_started_child (1executoroccupied +queuedcancel)
U6 test_cancel_running_does_not_release_child (startedEvent+cancelFalse)
U7 test_submit_failure_retains_unknown_and_rethrows (realexecutorclosed+controlledexecutorwrapper ambiguousexceptionafterdispatch; no fakeworkcompletion)
U8 test_seal_vs_late_child_registration_retains_uncertainty (Eventbarrier race; no global bool)
U9 test_foreign_epoch_duplicate_callback_and_fork_reset_cannot_clear_debt (no os.fork! pure reset seam simulated incarnation only; currenttoken unaffected)
U10 test_capacity_overflow_is_unknown_without_blocking_callable (capacity2, actualexecutionscontinue).

## Task2: minimal implementation after causal RED accepted

- [ ] Implement registry/helper only, then exact REST/engine hooks. ActivityReservations unchanged, no parallel claim of drainauthority.
- [ ] Syntax/focusedlint; beforeGREEN save onlyactualchangedsourcehashchain vsbase. freeze testassertions unchanged.
- [ ] ONE boundedserial GREEN U1..U10 after Root resource window; no hiddenretries.

## Task3: actual Flask boundary (separate profile freeze/rootGO)

H1 test_post_root_tracks_real_adapter_child_after_outer_returns. Existing reviewed Flask Boundary may be reused as externalartifact, never import c77 as active release; adjust only explicitROOT/newmoduleallowlist/threadcount frozenforreview. Load actual newbranch rest_server factory/StateStore in scratch with auth/ratedisabled. normalizationleaf synthetic; transcriberleaf calls production submit_owned realexecutor/Event. Real POST upload→RESTrootwrapper→actualhelper→childworker; outer returns quickly, child remainsrunning; inspect private registry object, confirm rootretained and incomplete scope; release child/jointhen rootcompleted. HTTPstatus/history rules unchangedf100; c77errorgate NOT included.
H2 test_engine_dispatch_binding_and_exclusions: static sourceAST identifies exact engine _pool.submit replacement helpercall; read/AST only, no ML import. This static gate proves wiring location, NOT actual fullAudioEngine execution/ASR. Otheradapter/SW/IPC/TTS exclusions remain. This boundary's causal helper is actual production dispatch function, but native model/engine pipeline leaf remains synthetic; report limits explicitly.
Before H profile Root reviews fullguard/runner and exactallowedimports. FullFlask thirdpartyrequires explicitpackage searchpath with -S afterguard; never site/.pth. Existing guardfullmodule may require reviewed source-only tweaks; setupguardfailSTOP. H pack count2; not20RESTrerun. Full ubuntuparity/auditall ifrequired before eventualPR/merge remains futuregate, no currentpermissionforheavycheck.

## Task4: independent review / checkpoint

- [ ] Freeze exactnewsource/testmanifest, actualcounts/receipts. Root independent review beforecommit/publication decision; localcommit only if separatelyapproved.
- [ ] Align VG consumer with actualschema; keep requiresqualifiedauthority true/no positiveoperatorpermissions; no capability/secrets logs.
- [ ] Report source/localunit/Flask/static-binding gates separately; no fullserverdrain/installedruntime/ASR/live claims.

## Exact profile packet prepared

External PROFILE/parent.py +child.py +FREEZE_RED.json; repository futureUtests file prepared, parsed only. RED loads only Availability.test_new_owned_operation_contract_is_available and performs one assertTrue on AST-defined API presence. It does NOT import the absent module or run any futureUtest. Parent accepts expected childexit1 ONLY with1test/1failure/0errors/skips/violations/remainingthreads and FEATURE_ABSENT marker; parentqualificationexit0 means expectedfeatureRED, not productPASS. Future GREENfile still absent until exact implementation freeze+Rootreview. In child allowreadlist include ownrunner files to permit unittest failurelinecache; no live filesystem data. No GREEN/REDprofileexecuted duringpreparation.

## Checkpoint 2026-10-30 — локальная приёмка U1–U11

Root принял точные source hashes owner `14fa1984`, REST `deae1822`, engine
`6a9f3251`, unit `f0358f41` и полный журнал GREEN11: **11 методов PASS**, 10
реальных executor workers завершены, 0 ошибок/skips/guard violations, child exit0,
reaped/PGID empty, freeze stable. Повторять этот прогон без нового изменения не нужно.
Первоначальная availability RED не является воспроизведением runtime-инцидента.
Отдельный причинный RED U11 воспроизвёл ошибочную привязку чужого Future; исправление
удерживает только weak reference первого Future и проверяет его identity в callback.
Независимое source review Main принято с явными ограничениями покрытия.

H1/H2 подготовлены отдельным тестовым файлом и внешним профилем **без запуска**.
H1 выполняет actual Flask POST и actual `submit_owned` с настоящими executor/Event,
но заменяет normalization/transcriber leaf; H2 читает AST настоящего engine hook.
Это не actual ASR, live E2E, authenticated maintenance ACK или полный server drain.
Профиль: CPython3.12 `-I -S -B`, explicit installed purelib без `.pth`, scratch HOME/TMP,
OS network deny + Python audit guard, максимум два executor workers, deadline25s,
TERM3/KILL3 только своей группе. Точные source/runner hashes и полный guard передаются
Root до разрешения одного запуска; setup/guard failure означает STOP без автоповтора.
Чтение установленной библиотеки разрешено, полная транзитивная supply-chain attestation
не заявляется. Никаких application imports или исполнения тестов при подготовке.

Остаются ограничения: capacity256 без terminal compaction, admission open,
`scope_complete=false`, `fence_result=None`, GigaAM internal pools и прочие перечисленные
producers исключены. Ни локальный commit, ни зелёные unit-тесты не разрешают выкат.
Исходный пакет c77 сохраняется отдельно. Maintenance22a3 исторический: не применять
старый bundle поверх более новой установленной конфигурации5a09.
