# Единый звонковый интерфейс: согласованный source-only контракт и offline PLAN

Действующий design supersedes прежнее join=muteAI предположение. Это проект контракта, НЕ implementation/readiness. Источники: Ear139d, bounded Gateway173eec5a, Main1921; исторический fbb6 сохранён в отдельной инвентаризации, qualification в FRESH_SOURCE_QUALIFICATION.json. Configuredruntime8b40 не квалифицирован. Никаких тестов/imports/network/audio/calls/расписаний/config/credentials не выполнялось. Incident VG остаётся приоритетом.

## Рекомендация и режимы
Общий authenticated same-session entry для поддержанных клиентов, без обязательного Telegram. Telegram — один канал уведомления во время звонка; post-call summary/recording отдельно. При incoming робот сразу отвечает, owner invitation готовится независимо от summary/TG. Пропущенный ring не закрывает возможность войти в ещё живой звонок. iPhone app — условный кандидат с существующими pieces, foreground web/desktop/SIP — клиенты с разной готовностью, не обещанная эквивалентность.

| Выбор владельца | Микрофон / кто слышит | Поведение секретаря |
|---|---|---|
| OBSERVE | По умолчанию mic muted; владелец слушает допустимый downlink | BOT продолжает; JOIN/прослушка НЕ вызывают takeover |
| WHISPER | Явный private text/voice к AI; не /mic-audio, не телефонный mix | BOT продолжает; подсказка не равна смене authority или автоматическому озвучиванию текста |
| HUMAN_DIRECT | Явный takeover, подготовленный direct duplex и explicit unmute | После подтверждённого cancel/clear/mute ACK секретарь уступает |
| HUMAN_TRANSLATED | Явный takeover и RU↔ES в обе стороны; raw RU→caller запрещён | Секретарь уступает, остаётся только согласованный переводчик |

Generic JOIN/CallKit answer открывает безопасный mic-muted context; не выбирает HUMAN или TRANSLATED автоматически. Индикатор «кто слышит» обязателен до capture/unmute: никто / только AI / собеседник через перевод / собеседник напрямую. Требование нового UI — proposal, не существующая кнопка. WHISPER — канал взаимодействия поверх BOT, не синоним передачи разговора владельцу.

## Source pieces и ограничения
- Gateway173 `_notify_screening_started`: ранние TG/PushKit, но awaitTG перед push; настоящая параллельность/фоновые гарантии не доказаны. iOS PushKit→CallKit→answerIncomingCall(existing sid)→mic существует. Нынешний answer handler включает mic, не связывая его с явным режимом/AIquiesce: для нового безопасного JOIN контракта это gap, а не требование всегда добавлять takeover.
- Ear CallObserverCoordinator/VGCommandPosting: observer events/downlink через CallAudioPlayer `/monitor/audio`, textwhisper `/agent/whisper`; самостоятельного human micjoin нет. Legacy onIntervene вызывает local `_bot_active` и переключает UI несмотря на IPCerror — не реальный takeover.
- Gateway `/agent/whisper`→`agent_whisper`→`_deliver_whisper` передаёт текст в engine.add_whisper или agentqueue, не прерывая autopilot. iOS GatewayClient `sendWhisperVoice` использует отдельный `/agent/whisper/voice`; `agent_whisper_voice` делает STT→тот же deliver, не вызывает callee inject. Это реальные source pieces. Ear voicewhisper в рассмотренном observer protocol не найден.
- Приватность whisper не доказана end-to-end: handlers публикуют `agent.whisper.ack` с текстом в общий session event stream; новый observer permission/redaction contract отсутствует в этом обзоре. Нужны audience isolation/redaction и проверка latecallback, а не обещание приватности только из-за отдельного HTTP endpoint. LLM может учитывать подсказку в ответе: private transport не доказывает неразглашение её содержания. Не пересылать raw/translated whisper как caller audio или автоматическую реплику.
- `/agent/takeover` предоставляет cancel/quiesce/passthrough control, а `/mic-audio` — owner audio. Takeover mode ACK не равен confirmed mediajoined. Passthrough auto зависит от языков; iOS currentclient discards body. Предлагаемый direct/translated выбор должен быть явным.
- owner→caller: `_process_mic_audio` takeover+voice_passthrough — raw; иначе STT→translate→TTS→callee. caller→owner cascade reverse: tgt→src, opt-in flag (source defaultFalse), ещё отдельный voice gate. Realtime reverse unavailable status. `_deliver_translation_voice` публикует `tts.ready remote_to_user`; iOS direction+ConnectionIdentity guards полезны. Обычная конференция не является переводчиком.
- SIPTransport.create_call failclosed `sip_bridge_not_implemented`. Ear provider Gateway/none. Никакого готового SIP/RTP/foregroundwebjoin sourceproof. Ear Conversation — отдельная mic/AI session, не phone attach; wake-word pause не полный audio arbiter.

## Lifecycle, authority и handoff — proposal
Независимые измерения: call LIVE→ENDING→ENDED; invitation RINGING/MISSED/DECLINED/ACCEPTED; presence DETACHED/OBSERVING; conversation BOT/HUMAN_DIRECT/HUMAN_TRANSLATED. Terminal latch сильнее всех поздних событий. Missed/declined ring оставляет BOT и late authenticated join пока LIVE. Нельзя возобновлять закрытый звонок или создавать новый из старой ссылки.

Observer subscriptions отделены от одного активного TX/control lease. Допустимое число observers, их recording/transcript/whisper visibility — UNKNOWN, новая политика не выдаётся молча. OBSERVE не резервирует mic и не может менять authority. WHISPER capture требует явного private action и отдельного scoped sender/control permission; это не «ещё один открытый call mic».

HUMAN takeover transaction: authenticated explicit intent + selectedmode, permissions и нужные directional media readiness, затем AIcancel/clear/mute ACK, fenced mode commit и explicitunmute. До commit owner audio закрыто. Quiescefailed/unknown не даёт connected/voiceallow и не оправдывает guessed rollback. Только явный takeover выключает BOT; observing/whispering не выключают его. Возврат к BOT отдельно подтверждается, не выводится из временного disconnect.

Один authenticated owner subject, device identity, call generation, idempotency ID, ограниченное непросроченное право и monotonic TXlease/media epoch. Это логические требования, не новые endpoints/tokenformat. Ссылка не bearergrant; caller/session/owner проверяются заново. Expired/revoked/replayed callbacks отвергаются. Existing endpointauth не является доказанным universaldevice lease. Секретов в ссылках/логах нет.

Handoff A→B: B готовится без uplink; после явного transfer intent атомарно fenceA/incrementepoch→permitB. Два TX одновременно запрещены, observers могут оставаться в рамках будущей политики. Ошибка до fence оставляет A; ошибка после fence блокирует uplink, старый A не возрождается автоматически. Передача устройства не переключает перевод в raw. Callerhangup отменяет ringing, joins, capture и queued delivery; повтор callback идемпотентен.

## Directional media и no raw fallback
Ownercallcapture→callee; caller_ingress→owner; privatewhispercapture→AI; TTS/monitorplayback — отдельные origins. WHISPER никогда не направляется в raw/translated caller pipeline, общий audio mix или playback echo. STT privatevoice разрешён только в privatewhisper pipeline. Late private STT completion после смены context не должен стать обычной репликой или отправиться другому звонку. SessioneventACK не должен раскрывать private content наблюдателям без нужных прав.

HUMAN_TRANSLATED: ownerRU→STT(RU)→ES→TTS(ES)→caller; callerES→STT(ES)→RU→TTS(RU)→owner. При ошибке/empty/mock/timeout/reverseunavailable — TRANSLATION_BLOCKED с видимой ошибкой, без автоматического DIRECT, rawRU или исходного текста вместо перевода. Выбор translator для обычного incoming не считается сделанным по умолчанию.

При каждом смене режима/device epoch остановить old admission/cancel+clearqueues, проверить readiness, commit. Stale raw/TTS/translation/whisper callback проверять послеawait и перед output. Agent modeepoch/ConnectionIdentity существуют, но end-to-end media/deviceepoch в currentPCM API не квалифицирован. Echo isolation не доказана аудиомиксом: monitor/TTS нельзя обратно в input/STT. Ear dictation/Conversation/phonecapture должны иметь явную audio ownership политику; не начинать второй захват и не терять диктовку молча.

## Offline state-machine PLAN — 33 базовых сценария, НЕ ИСПОЛНЕНЫ
Будущий purestdlib reducer/fakeports/clock; synthetic sid-A/sid-B, без runtimeimports/deps/network/audio/model/childprocesses. Exactsource/profile/window отдельным разрешением послеincident. SyntheticPASS не будет proof существующей wiring/security/echo/фоновой доставки.

| № | Сценарий | Обязательный результат |
|---|---|---|
| 1 | TG pending/fails при incoming | app invitation независим, BOT отвечает |
| 2 | Ringtimeout/decline | BOT продолжает, latejoin покаLIVE |
| 3 | JOIN/CallKitanswer→OBSERVE | micmuted, нет takeover/cancelAI |
| 4 | OBSERVE+duplicatejoin | одна presence subscription по identity, BOT не меняется |
| 5 | TEXTWHISPER | только AIqueue, нет callerinject или takeover |
| 6 | VOICEWHISPER | privatecapture→privateSTT→AI, ноль /mic-audio |
| 7 | Whisper STTawait + hangup/modeswitch | stale rejected, не обычная реплика |
| 8 | Whisper ACK + read-onlyobserver | private content не раскрывается без audiencepermission |
| 9 | Явный HUMAN takeover при clearpending | callmic закрыт, joined не заявлен |
| 10 | Quiescefailure/unknown | нет raw output, truthful UI, no guessedrollback |
| 11 | TRANSLATED, reverse unavailable | blocked, no rawRUfallback |
| 12 | STT/translate/TTS timeout/empty/mock | blocked, оригинал не подставляется |
| 13 | OwnerRU и callerES одновременно | правильные независимые направления |
| 14 | Direct→Translated, oldraw in-flight | oldepoch reject перед выводом |
| 15 | Translated→Direct, oldTTS in-flight | stale reject, нет догоняющей озвучки |
| 16 | BOT→HUMAN, oldAI/filler/suggestion | cancel/clear, stale sideeffects запрещены |
| 17 | ModeABA | oldepoch rejected даже при томже mode |
| 18 | TTS/monitorplayback попал в input | echoorigin не направляется в STT/caller/whisper |
| 19 | Permissiondenied/audiointerruption | нет hiddencapture/connected; BOT не отключён observeraction |
| 20 | Hangup в любой await | terminal wins, ringing/media очищены |
| 21 | Wrongowner/session/generation/expiry/replay | reject до control/media |
| 22 | Forged/forwarded invitation | ссылка не grant, нет доступа к audio/history |
| 23 | App/web/desktop entry | один logicalsession, Telegram необязателен |
| 24 | Два TXjoins одновременно | один lease, loser conflict, observers отдельно |
| 25 | Handoff A→B success | fenceA до permitB, mode сохраняется |
| 26 | B fails до fence | A остаётся, lease не украден |
| 27 | B fails после fence | blocked, A не оживает со старым lease |
| 28 | Stale A PCM/TTS после transfer | reject внутри тойжеsession/mode |
| 29 | Handoff/accept одновременно hangup | terminal, оба без uplink |
| 30 | Duplicate transfer/cancel/hangup | один commit, стабильныйreceipt |
| 31 | Devicehandoff translator теряет readiness | blocked, no rawRUfallback |
| 32 | Voicemail/ambiguous separatePSTNanswer | не humanconfirmation/takeover |
| 33 | Postcall summary/recording | отдельнаяsurface, не активируетjoin/call |

## Платформенные и финансовые границы
Paid Apple Developer membership у владельца НЕТ. PushKit/CallKit — codepieces, usablebackgroundinvitation/configuredAPNs/provisioning/currentinstall UNKNOWN; не покупаем/создаём. PWAbackgroundring не обещаем. Отдельный Appleworker проверяет актуальные платформенные возможности и цену; webresearch не дублируем. Реальные cost/latency/quality/backgroundreliability UNKNOWN. Source содержит STT/translate/TTS/provider stages и costaccounting; нулевую цену/instant нельзя вывести из кода. WatchSE2/iPhone15ProMax/LiveActivities только optionalclienttiers ниже.

До auth/security или privatewhisper реализации — независимый adversarialreview. После будущих offlinechecks отдельно требуются actualapp/servercontracts, permissions/background/duplex/echo/translationquality/multidevice E2E; сейчас ничего из этого не заявляется. Без новыхroutes, скрытойrecording, securitygrants, credentials/networkexposure/runtime/model/clone/OrbStack действий.

## Дополнение: единый incoming/outgoing интерфейс — frozen source-only план

Срезы: Gateway173eec5a, Ear139d228f, Main1921de38, exact blobs в OUTGOING_REUSE_PINS.json. Ничего не запускалось/не планировалось в сервисах. Incoming incident остаётся приоритетом VG; это добавление к прежней карте, не новый implementation проект.

### Что переиспользовать
| Режим интерфейса | Реальные source pieces | Пробел / граница |
|---|---|---|
| Владелец говорит напрямую или RU↔ES | VG app/models.py `OutboundCallRequest`; iOS GatewayClient.swift `startOutboundCall`; Ear GatewayCallProvider.dial без prompt → `/v1/telephony/calls/outbound` | Переводческий API существует. Общий join/mode/lease и подтверждённый duplex из предыдущего контракта ещё не квалифицированы; DIRECT и TRANSLATED выбираются явно |
| Автономный звонок по промпту | VG app/routers/prompt_call.py `PromptCallRequest`, `prompt_call`, `_do_prompt_call_impl`; iOS `GatewayClient.promptCall`; Ear `GatewayCallProvider.dial(prompt=...)` → `/v1/telephony/calls/prompt-call` | prompt/lang/max_duration и client_request_id есть на сервере. iOS promptCall не передаёт client_request_id/max_duration; Ear forwarding также не передаёт client_request_id. Нельзя обещать server-side dedup для этих клиентских путей без дополнения |
| Расписание datetime+timezone+retry cap | Main src/core/follow_ups.py `FollowUpStore.due_scheduled`, `run_follow_ups_tick`, `prompt_call_vg`; callback_handler.py owner action «Позвонить сейчас» | Это restart-safe напоминание + ручная кнопка, а не автономный scheduled dial. Notify retry и hold timers VG не являются retry звонка. Bounded search call paths VG/Ear не выявил готового persistent autonomous call scheduler; глобальное отсутствие во всех проектах не доказано |

Main follow_ups явно запрещает автоматический звонок без owner click в текущем контракте; менять его семантику молча нельзя. `prompt_call_vg` передаёт to/prompt, не полный новый envelope. Повтор после HTTP timeout требует reconcile provider/session состояния, а не предположения «звонка не было». VG `_idempotent_call_response` различает session без provider call и состоявшийся запуск; это полезная основа, не гарантия exactly-once внешнего провайдера.

### Единая карточка намерения — proposal
Перед исходящим уточнять только реально отсутствующие или неоднозначные адресат, цель и полномочия. Уже однозначно заданные поля повторно не спрашивать; новое согласование нужно при материальном изменении envelope. Это UX-политика проекта, не разрешение на реальный звонок.

Тот же интерфейс показывает incoming/live/join и отдельное outgoing intent. Поля исходящего: фактический адресат/нормализованный номер и выбранная линия; DIRECT/TRANSLATED/PROMPT; src/tgt; цель, вопросы и стоп-условия; разрешённые действия и запрещённые обязательства; согласованный текст раскрытия AI/перевода/записи; duration и денежный потолок; момент запуска; timezone; окно допустимого опоздания; retry cap с интервалом; cancel и immutable intent revision/idempotency ID. Это не permission на произвольные контакты, покупки, сделки или медицинские/юридические обязательства. Содержимое собеседника не может расширить envelope. Согласие на запись и disclosure требуют отдельной применимой проверки; правовой вывод здесь не делается.

Для scheduled mode сохранять исходное локальное время+IANA zone (интерфейс владельца Europe/Madrid) и однозначный UTC instant; DST gap/fold требует явного разрешения неоднозначности. До due time проверить актуальность разрешения, неизменность адресата/лимитов/режима и budget. Отмена/expiry должны атомарно закрыть claim до dial; после фактического dial отмена завершает живую сессию по её lifecycle, а не обещает «никому не звонили». Scheduler owner один; гонки двух dispatchers/restart не создают два dial. ID расписания и ID каждой ограниченной попытки различны; повтор HTTP одной попытки использует тот же key. На ambiguous provider result — reconcile/hold, не автоматический новый dial. Max attempts и общий бюджет не сбрасываются после перезапуска или смены устройства. Пользователь может отменить из любого authenticated client; Telegram не обязателен.

Серверный bot principal уже имеет отдельные guards `_require_safe_bot_prompt` (system/cascade/duration cap, без hold); owner и bot rights не взаимозаменяемы. Отдельные новые budget/action/disclosure fields нельзя считать реализованными лишь из-за свободного текста prompt или max_duration. Конфиг/голос/модель по умолчанию сейчас не меняем.

### Дополнительные offline-сценарии PLAN ONLY
34. Повтор prompt submit одного intent→одна попытка/session; разные recipients не склеиваются.
35. Клиентский timeout после provider acceptance→reconcile, без второго dial.
36. Два scheduler workers/restart→один due claim, устойчивый attempt count.
37. DST gap/fold, expired/missed window→ноль неожиданных звонков, требуется разрешение.
38. Cancel до claim/во время dispatch/после dial→соответствующий terminal outcome, поздний callback не оживляет.
39. Retry cap/aggregate budget исчерпаны→никакого следующего dial, в том числе после restart.
40. Prompt reply пытается расширить recipient/action/обязательства→envelope не меняется.
41. Outgoing TRANSLATED без reverse readiness→blocked, без raw RU fallback.
42. Notification/deep-link на Watch/iPhone/desktop→тот же authenticated intent/session; stale identity не запускает звонок.
43. Notification retry/summary failure→не повторяет сам исходящий звонок.

Итого43 будущих сценария в пакете; NONE executed. Это specification-level план, не production tests или авторизация реализации. Перед автономным расписанием отдельно согласовать миграцию ручного follow-up контракта и независимое ревью действий/бюджета/dispatch.

### Клиентские уровни, без платформенных обещаний
iPhone15ProMax — кандидат основного клиента; WatchSE2 — возможные уведомление/подтверждение/переход к iPhone, не доказанный самостоятельный media endpoint. Live Activities — необязательная поверхность статуса, не механизм dial/join/background ringing. Возможности Apple/entitlements/стоимость/ограничения проверяет отдельный worker координатора; здесь их не утверждаем. Платной Apple Developer membership нет. Никаких purchases, credentials, APNs setup, network exposure, auth endpoints, реальных звонков или заданий этим документом не создаётся.
