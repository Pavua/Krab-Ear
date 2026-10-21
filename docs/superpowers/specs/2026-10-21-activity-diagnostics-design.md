# Диагностика занятости и памяти Ear

Основание: владелец 21.10 05:49 CEST разрешил реализацию согласованной карточки, тесты и Ear deploy/restart. ES — после стабилизации памяти. Main ведёт общий контракт, Ear владеет только своими исходниками и lifecycle. Предыдущий design/architecture audit и Main ACK требуют честного unknown вместо глобального idle.

## Контракт

Расширить существующие get_memory_ledger IPC и /v1/readiness REST полем activity. Schema 1 содержит owner, owner_pid, owner_epoch (UUID инкарнации процесса), advisory:true, generation:null, generation_known:false, state:busy|unknown, components и coverage. Owner-idle/released/drain grant не выдаются: background warmup, batch, и прочие admission пути пока покрыты не полностью. Busy имеет приоритет над unknown; отсутствие busy при неполном coverage означает unknown. Loaded identity и ready остаются отдельными существующими полями stt_residency.

Компоненты IPC: recording/finalizing и meeting. Методы activity_snapshot сервисов читают только metadata под существующими locks, acquire(blocking=False); ни preview text, ни settings, ни HTTP, ни model operations. Recording сообщает physical capture, active generation и число finalizing; отсутствие/ошибка provider означает unknown. Meeting учитывает setup, stopping/recovery, session и retained worker; занятый transition/worker lock означает unknown.

Строгий observe_brain_lease() — отдельная additive API. Состояния held/expired/empty/absent — качества наблюдения, не доказательство завершения inference; contention/replaced/malformed/unreadable/unavailable — known:false. Не вызывать старый поглощающий ошибки reader; readonly nonblocking open/flock, regular-file validation, cap64KiB, проверка inode и метаданных до/после чтения. Не создавать parent/file, не писать shared lease. Legacy API не меняется. Payload/пути/тексты транскриптов не выдаются; только валидированные owner/pid/timestamps.

Компонент REST native_stt: один наблюдатель server-owned completion tokens вокруг уже существующих POST/WS путей, без новой очереди/admission. Reserved token становится running внутри executor, завершается в worker finally либо подтверждённым Future completion/cancel-before-start. HTTP timeout/disconnect и снятие singleflight не означают completion. Неизвестный Future остаётся unknown/busy. Подмножество имеет собственный generation, увеличиваемый на reserve/start/complete/unknown под одним lock, но это не owner-wide CAS version. WS token живёт до возврата native window worker, даже после client disconnect. Lock contention даёт unknown немедленно. Не менять текущую semaphore/fail-fast политику в диагностической карточке.

## Проверки и выпуск

TDD на temp lease paths и fake external leaves; реальные helper/service methods, route wiring и controlled Futures. Финализация без записи, meeting setup/stop tail, worker-alive с unknown identity, очереди, timeout/cancel/exception, чтение при contention/replace/oversize. Новые GET не делают STT/TTS/load/unload и не пишут production stores. Legacy tests, Python3.12 Ubuntu parity/CI, audit-all, independent Astra whole-diff. Никаких новых dependencies, enforce flags, общей очереди/координатора или соседних edits.

Source/CI допускаются во время соседних окон; Ear release только после VG FINISH и Main FINISH, exact-head/postmerge CI, fresh resource/recording/finalizing/meeting gate и runtime qualification. Только штатный безопасный release/restart; Swift binary не запускать. ES/model benchmarks, clone/Main/OrbStack lifecycle не входят. Следующая отдельная работа — owner-local admission/drain с полноценной event version, stable-lock и участием всех warmup/inference owners.
