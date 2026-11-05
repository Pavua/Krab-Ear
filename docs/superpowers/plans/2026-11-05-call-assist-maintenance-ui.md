# Call Assist: не считать отказ IPC состоянием idle

База — exact #2184 `270f64c4967d4560bed5a66b090d90005d4d2846`.
В этой ветке `start_call_assist` может вернуть `ok=false` с
`maintenance_active`/`maintenance_unknown`. Swift `applyCallAssistState`
принимает ответ как state, не показывает причину и при отсутствии active
подставляет false. Состояние UI и сохранённые настройки не должны изменяться
как при успешном start, когда backend отказал.

## Проверка и границы

1. До правки через маленький Swift harness вызвать извлечённый фактический
   `applyCallAssistState` с fake UI elements: отказ должен сообщить ошибку и
   оставить предыдущие статус/кнопки. RED на старом методе; выполнить также
   control для обычного state без `ok` и успешного активного состояния.
2. В методе обработать `ok=false` до изменения кнопок, показывая понятную
   причину maintenance; неизвестную ошибку не выводить как сырой текст.
   На start не сохранять настройки при отказе. Повторить harness как GREEN.
   Второй harness извлекает фактический `onStartCallAssist`, имитирует IPC и
   проверяет отказ, успех и legacy-ответ; на parent он даёт причинный RED.
3. Проверить Swift source wiring и связанные имеющиеся тесты при доступном
   ресурсе, независимое review exact diff. Standalone harness не является
   реальным AppKit E2E, не подтверждает внешний вид или установленный .app.
4. Без запуска .app, модели, реального аудио, Gateway или runtime lifecycle.

Не решает start/stop race, owner, summary persistence, global admission/drain
или live-call qualification. Удаление Gateway при неизвестной сохранности
остаётся HOLD; эксперимент `55804461` отозван.
