# Ear: минимальный bounded дизайн наблюдения producer/drain/passive

Source-only исследование отf100; не runtime qualification и не разрешение stop.
Не добавлять новый глобальный механизм по одному owner-local счётчику.

## Существующие hooks и их реальные границы

- `BackendSupervisor.supervisionMode` — private(set) lazy cached mode. `detectSupervisionMode`
  проверяет launchctl только при первом обращении; успешный launchctl сейчас не
  восстанавливает сохранённый режим уже живого Swift. `main.swift` пишет точный
  startup log marker. DEBUG override и BackendSupervisorTests уже существуют.
- `ensureBackendRunning` passive branch только ping/wait, без spawn и socket unlink.
  Но `forceRestartBackend` passive branch явно вызывает kickstart: passive не
  означает полный запрет lifecycle. Внешнее UI/operator окно должно исключать его.
- `RecordingCoreService.begin_shutdown()` выставляет closing-event без ожидания
  lifecycle lock. Это МУТАЦИЯ, не read-only preflight/временная pause: public resume
  контракта у этого hook не доказано. Не вызывать ради диагностики занятости.
- `CallSTTService.begin_shutdown()` закрывает свой admission под lock; `close(timeout)`
  удерживает активную lease до завершения worker и возвращаетFalse при недрэйненном
  worker. Повторный close может завершить cleanup. Это только call-STT subset.
- `BackendService.close()` вызывает callSTT/meeting/recording shutdown и прочие stop;
  это необратимый lifecycle этой generation, не глобальная read-only drain query.
- `Transcriber.close()` лишь делегирует engine.close, а отсутствие close/legacyNone
  совместимо трактует какTrue. Нельзя использовать wrapperTrue как доказательство
  полного native drain во всех старых поколениях.
- `activity_diagnostics.owner_activity_snapshot` намеренно оставляет generationNone,
  coverage.completeFalse и stateunknown даже при всех tracked componentsidle.
  `observe_brain_lease` — read-only annotation, не reservation/lock ownership grant.

## Минимальный следующий design, ещё не implementation

1. Для нового Swift startup будущая read-only diagnostics должна отдавать уже
   сохранённый `supervisionMode` + process incarnation; не повторно detect launchctl.
   Existing DEBUG tests позволяют проверить passive no-spawn/no-unlink отдельно от
   explicit force-restart. Получение этого из уже живого бинарника без hook остаётсяUNKNOWN.
2. Использовать existing read-only recording/finalizing/meeting/activity snapshots;
   schema должна сохранять incomplete coverage и не создавать `can_stop=true`.
   Не сбрасывать owners/leases и не вызыватьclose из inspector.
3. Реальное обслуживание требует отдельного producer fence у Main/VG плюс ownerquiet
   directTTS/IPC/UI; затем bounded ожидание уже принятых owner-specific задач.
   ACK на client-onlySTT pause не означает server drain и не отменяет inferred native work.
4. До доказанного полного coverage решение `HOLD/legacy-risk explicit approval`,
   а не синтетическое `all_idle`. Новый глобальный reversible admission пока не проектировать
   без отдельной карточки всех producers и ясной resume/rollback generation семантики.

## Исполненная маленькая offline проверка существующего механизма

`test_call_stt_service.py` выбранные4 существующих теста: timeout держит owner busy
до actual worker release; bounded closeFalse и повторный closeTrue; begin_shutdown
закрывает admission доdecode; shutdown во времяdecode запрещает reservation.
Реальный CallSTTService/threads, decoder/router/lease — synthetic. Результат4PASS,
46deselected,0.42s pytest /0.596s parent, threads_after1, groupempty/reaped.
OSnetworkdeny, disposableHOME, внешний25сек deadline. Это owner-local contract,
не глобальный Ear drain, не ASR, не проверка живого Swift или launchd.
