# Follow-up к брифу «порт 13 секций на Claude Design» — находки гейта

Дата: 2026-09-03 03:05. Исполнитель: Gemini 3.1 Pro (agy). Основной бриф:
`docs/design-briefs/2026-09-03-cd-port-remaining-sections.md` — все его инварианты
действуют. Правки НЕ коммитить. `HistoryPanelController.swift` и `Tests/` НЕ трогать —
цикл в CD-ветке подключает Claude сам.

## Что доделать (ровно это, ничего сверх)

1. **Нет двух строителей.** Создать по образцу соседних:
   - `cdBuildRecordingSchedulerSection()` в `HistoryPanelController+RecordingScheduler.swift`
     — форма (дата `makeSchedulerTimeField`, длительность `makeSchedulerDurationField`,
     описание `makeSchedulerDescField`, кнопка `makeSchedulerSubmitButton`) + список
     запланированных. Assoc-ключи `RecordingSchedulerAssocKeys.*` выставлять на СВОИ поля
     (как в Gemini-версии), иначе `onScheduleRecording` прочитает поля скрытого Gemini-бара.
     `rebuildSchedulerCard` (строка ~173) типизирован под `ThemeCardView` — обобщить на
     `NSView` с выбором `contentStackView`, ровно как ты уже сделал в `rebuildWebhookCard`.
   - `cdBuildSTTModelMemorySection()` в `HistoryPanelController+STTModelMemory.swift` —
     строки: устройство (`makeSTTDevicePicker`), прогрев (`makeSTTWarmupToggle`), простой
     (`makeSTTIdleStepperAndLabel` → stack label+stepper), принудительная выгрузка
     (`makeSTTEnforceToggle`), кнопки+статус (`makeSTTButtonsAndLabel`). Заголовок «Модель STT в памяти».
2. **Потерянные контролы** (в Gemini-версии есть, в CD нет — для владельца это исчезнувшая настройка):
   - `cdBuildWebhookManagerSection`: добавить поля **«События»** и **«Секрет»** (в Gemini —
     `eventsField` NSTextField и `secretField` NSSecureTextField, строки 74–102) и выставить
     `WebhookManagerAssocKeys.eventsField` / `.secretField` на них — `onRegisterWebhook`
     читает именно assoc-объекты.
   - `cdBuildQuickCaptureSection`: добавить строку с `pasteUndoButton` (лейбл — по его
     Gemini-строке в `buildQuickCaptureSection`).
3. **Откатить переименования, которых не просили:**
   - `makeSTTDevicePicker`: пункты вернуть к исходным «GPU (mps)» / «Процессор (cpu)»
     («Accelerate (CPU)» — неверный термин, GigaAM работает на torch mps/cpu).
   - `cdBuildAllSettingsSection`: заголовок вернуть «Все настройки», лейбл строки поиска —
     «Поиск и обновление» (не «Сырые ключи»).
4. `cdBuildVoiceAssistantSection`: у `cdMakeSliderRow` порога вместо одноразового
   `NSTextField(labelWithString: "")` использовать тот же value-label, что обновляет
   `onVAWakeWordThresholdChanged` (если такого stored/assoc-лейбла нет — оставить как есть
   и написать об этом в отчёте одной строкой).

## DoD
```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3
cd ../.. && python3 scripts/audit_orphan_panel_controls.py --fail-on-found | tail -1
```
Отчёт — по-русски, коротко: список файлов и что сделано по пунктам 1–4.
