# Robot-first / подключение владельца: карта фактических возможностей

## Ограничение владельца: Apple membership
У владельца нет платной Apple Developer membership (уточнение координатора 30.10.2026). PushKit/CallKit здесь — существующие части исходного кода, а не подтверждённое доступное фоновое приглашение на установленном iPhone. Credentials, provisioning, entitlement и текущая установка приложения UNKNOWN; настройки не читались. Покупки, регистрация и настройка APNs/PushKit не входят в scope. Актуальные возможности Apple, стоимость и альтернативы исследует отдельный исполнитель координатора; этот документ не делает выводов о доступности или цене. Рекомендация app route остаётся условным кандидатом до этого исследования. Same-call media/режимы и запрет raw fallback сохраняются независимо от способа приглашения. Простая конференция не обеспечивает двусторонний перевод.

Источник: bounded чтение committed source Ear139d и Gatewayfbb6; точные SHA и хеши в MANIFEST.json. Это 100% текущего блока инвентаризации, не готовность продукта. Никаких запусков, тестов, сетевых проверок, чтения credentials/config или изменений runtime.

## Согласованный UX (актуализировано)
Робот отвечает сразу; приглашение владельцу параллельно, доступно и позднее пока звонок LIVE. Generic JOIN = OBSERVE, mic muted, BOT продолжает. WHISPER privately в AI, не в телефонную линию; HUMAN_DIRECT и HUMAN_TRANSLATED требуют ЯВНОГО takeover перед AIcancel/clear/mute. OneactiveTX/controllease отделён от observer subscriptions; observerlimit unknown. Telegram не обязателен. Полный согласованный контракт заменён целиком в JOIN_TRANSLATOR_CONTRACT_AND_TEST_PLAN_RU.md, без старого join=muteAI инварианта.

## Уже реализованные части
| Путь / символ | Подтверждено кодом | Граница |
|---|---|---|
| VG app/main.py:2976 `_notify_screening_started` | Ранний incoming_call PushKit всем зарегистрированным owner devices; TG и push ветки | Флаг/config и реальная доставка не проверялись. Push выполняется после awaited TG: строгой одновременности нет |
| VG iOS ContentView.swift:507 / :861 `answerIncomingCall` | PushKit→CallKit; принятый UUID→existing session ID; WS и mic upload в эту сессию | Нет вызова takeover перед mic; UI «подключён» выставляется до permission/start. Ошибки mic start/upload подавлены; callback не сверяет captured sid с новым active ID |
| VG IncomingScreeningLifecycleModel.swift:255 `requestTakeover` | Отдельный takeover endpoint, generation/terminal guards; ошибки показываются | Сам метод не подключает mic; не доказано атомарное объединение с CallKit answer |
| VG routers/prompt_call.py:1075 `agent_takeover` | Pause AI, quiesce audio, fail503 при неуспехе; passthrough в meta | ACK mode не доказывает human media joined. Bridge-фраза планируется отдельно; requested/queued не равно heard |
| VG app/main.py:4109 `receive_mic_audio`, :4221 takeover branch | PCM16 upload; takeover+voice_passthrough→raw voice, mute/hold+epoch guards | auto passthrough может быть false при разных языках; GatewayClient.swift:473 игнорирует response body |
| Ear CallObserverCoordinator.swift:73 / CallAudioPlayer.swift:103 | События, прослушка /monitor/audio, hangup и text-whisper | Observer protocol не содержит takeover/mic join; whisper — подсказка AI, не голос владельца |
| Ear ConversationViewController+Audio / +WebSocket | Собственный mic/speaker/WS AI conversation | Не найден attach к наблюдаемому telephone session. main.swift:818 приостанавливает wake word, но это не доказанный общий audio arbiter |

## Подтверждённые ограничения
1. Ear CallAutomationController.swift:765 `onIntervene` игнорирует ошибку IPC и всё равно меняет кнопку. Backend call_session_service.py:426/459 меняет только локальный `_bot_active`; удалённый takeover не вызывается. Старую кнопку нельзя принимать как готовый human join.
2. Ear call_provider_factory.py поддерживает Gateway/none; исторический local SIP adapter удалён. Gateway app/telephony/sip.py:109 по умолчанию возвращает sip_bridge_not_implemented: fake bridge не реализует SIP/RTP. Рабочий SIP путь не подтверждён.
3. iOS имеет НАСТОЯЩИЕ source pieces incoming mic attach, а не только исходящий startSession. Но связка answer→takeover ACK→raw passthrough→downlink ready отсутствует в рассмотренном answer handler. Live duplex не проверен.

## Минимальные недостающие контракты (предложение, без реализации)
- Один session/generation owner для invitation→accepted→joining→joined, с идемпотентной отменой stale callbacks. Существующий mode=takeover не использовать как humanjoined receipt.
- Ранний owner invitation независим от TG/summary; политика decline/timeout/voicemail и callerhangup cancels ownerleg. App route — кандидат с существующими pieces; SIP route не готов.
- Mic разрешать только после подтверждённого AI cancel/clear/mute и согласованного raw passthrough. Не объявлять звонок подключённым до media/permission ACK; ошибки видимы.
- Проверить raw downlink, echo cancellation, interruption/permission lifecycle, dictation/Conversation/monitor mic ownership. Не создавать второй call/session для присоединения.
- Не менять маршрутизацию, номера, credentials, клон или модели. До incident fix — никакой takeover implementation. Следующий допустимый отдельный scope: offline state-machine plan/tests после разрешения координатора.

Source inspection не доказывает текущие APNs credentials, доставку push, звонок iPhone в фоне, PSTN audio, AI silence или восстановление после обрыва. Реальный статус всего этого UNKNOWN.

## Уточнение универсального входа
Telegram — уведомление, не обязательная точка входа. Единый authenticated same-session join для клиентов-кандидатов iPhone app/foreground web/desktop/реального SIP, с одним активным owner и безопасным handoff. Сейчас это proposal: existing173 API mode/mic/monitor/stream не доказывают device lease/replay/expiry contract; webjoin не квалифицирован, SIPstub, PWAbackgroundring не обещается. Детали и дополнительные11offlinecases23–33 в JOIN_TRANSLATOR_CONTRACT_AND_TEST_PLAN_RU.md. Никаких новыхauth/credentials/exposure/implementation.

## Whisper source qualification173
Gateway agent_whisper и agent_whisper_voice → _deliver_whisper → engine/agentqueue; voice отдельный /agent/whisper/voice, не /mic-audio. iOS sendWhisperVoice существует. Earobserver textwhisper существует. Transportisolation полезна, но privateACK публикует текст в sessionstream: observeraudience/redaction/end-to-end non-disclosure не квалифицированы. Никаких новых security grants.
