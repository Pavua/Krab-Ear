# Finalizing tail: безопасный restart admission

База merged2098 `8dc5e48d88b9357e4782ae87ea5686c936618530`, отдельная ветка codex/ear-finalizing-idle-20261016. Owner разрешил реализацию/тесты/merge/deploy; текущий release2098 frozen. Причинность исторических AppHang/504 не установлена.

Проблема: phase A переносит поколение из active в finalizing перед STT/history. get_recording_state возвращает physicalFalse/ownerNone/tokenNone; оба lifecycle shell-gate считают это idle и могут оборвать tail. Независимый Astra Ultra review подтвердил gap.

1. RED: actual RecordingCore start/stop, barrier phase C; getter должен показывать finalizing_count=1/is_finalizing=True при physicalFalse, затем0/False после полного ответа. Проверить параллельную G2 без изменения semantics active generation.
2. RED: настоящие shell/parser/private Unix IPC для safe_restart и install gate; count1, missing/malformed count/owner/token/start_request → no launchctl; valid0 → прежний idle.
3. Под lifecycle-lock публиковать только count и boolean, без текста/токенов старых поколений. Оба shell-gate требуют exact nonnegative int count0 и согласованныйFalse flag/пустой activeowner+token. Старый backend без полей UNKNOWN/HOLD; force не использовать.
4. GREEN: зависимые recording lifecycle/privacy и реальные shell gates, audit-all14; Ubuntu exact-head CI. API docs и NOW.
5. Независимый audio/lifecycle whole-diff gate, merge/postmerge exactSHA CI. Production не перезапускать до доказанной старой finalization idle/согласованного runtime gate. Новое API само по себе не доказывает старое runtime-state.
