# Приглушение: ожидание вне MainActor и сохранение восстановления

## Подтверждённые дефекты

`SystemAudioDuckingService` синхронно вызывает `Process.waitUntilExit()` без
срока ожидания. `AgentAppDelegate.startRecording()` вызывает его из MainActor
до первого `await`; такие же синхронные вызовы восстановления есть в stop,
reconciliation и terminal meeting callback. Это достижимый источник блокировки
UI, но связь с историческими Sentry App Hanging ещё не доказана.

`restoreAfterRecording()` очищает snapshot и isDucked через безусловные defer,
даже если восстанавливающий script не выполнился. Повторный restore теряет
исходную громкость. Неудачный mutating script также может частично изменить
звук до возврата ошибки; восстановление нужно и в этом случае.

## Требуемое поведение

- MainActor не запускает и не ждёт subprocess синхронно.
- Одна последовательная очередь владеет snapshot, lease и операциями целиком.
  Нельзя ограничиться actor с повторным входом между await.
- Чтение snapshot, mute и restore имеют общий monotonic budget операции;
  ожидающий в очереди вызов не получает новый бюджет после ожидания.
- Timeout завершает только собственного child и подтверждает его завершение.
  Неизвестное завершение не разрешает следующий mutating script.
- Возможное изменение фиксируется до запуска mutating script. Snapshot
  освобождается только после успешного restore либо доказанного no-op.
- Failed restore сохраняет snapshot и допускает повтор того же lease.
- Новый lease не наследует snapshot активной записи и не обгоняет restore.
- Отключённое приглушение и уже muted output не меняют звук.
- Pending start удерживает RecordingStartGate. После await duck повторно
  проверяются request ID, cancellation и stop-intent; отмена до IPC не открывает
  запись. Отмена после IPC сохраняет существующий backend-generation контракт.
- Фазы preparingAudio и requestSent различаются. Promotion policy допускает
  pending handoff только после фактической отправки start_recording; общий
  recordingStartInFlight во время подготовки аудио не доказывает backend start.
- Повышение dictation → meeting передаёт тот же audio lease. Только terminal
  callback соответствующей meeting generation может его отпустить.
- Запоздавший restore старого lease не меняет звук нового lease.
- В тестах используется настоящий сервис и настоящие callbacks delegate;
  `SystemAudioDuckingServiceTestable`, копирующий production, не является gate.

## Границы

Остаётся текущий osascript control plane. Не добавляются CoreAudio migration,
зависимости, настройки TCC, новые IPC методы или изменение пользовательских
настроек. Тесты process runner используют только harmless owned child fixtures;
реальный volume/mute из unit tests не вызывается. Canonical WIP, OrbStack и
соседние lifecycle не меняются.

Native mic/autopaste acceptance остаётся отдельной проверкой. Акустический
probe 16.10 около 04:00 CEST завершил два Whisper HTTP200 и восьмисекундный
захват с ненулевым сигналом, но similarity=0: FAIL. Это не успешная диктовка,
не проверка автовставки и не доказательство причины исторического AppHang/504.
