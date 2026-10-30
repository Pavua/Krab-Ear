# Stage B: реальный Flask upload → executor → STT-result

База f100ced4668531b356fd5b528504dd9bdfd4e756, isolated branch codex/ear-rest-stageb-1030.
Scope: один подтверждённый gap: engine error dictionary превращается в HTTP200/statusok.
Использовать существующий 10-line gate c9e62600/#2120, не изобретать новый classifier.
Без publication/merge/runtime/install/restart/model/provider/audio/network/clone/OrbStack.

- [x] Реальный REST import/create_app/decorators/upload/singleflight/budget/ThreadPoolExecutor.
- [x] Отдельный HOME/TMP/data, сетевой OS deny, Python audit module/file/process/thread guards.
- [x] Три error envelope × общий/call-auto профили: RED HTTP200 и history side effect вместо503.
- [x] Сохранить silence/vad_skip HTTP200 для обоих профилей.
- [x] Перенести существующий privacy-aware gate после post-call privacy/deadline, до history/metrics.
- [ ] Независимое AGY whole-diff review через Root, без дублирующего AGY запуска здесь.

Files: backend/rest_server.py; tests/rest_stageb_boundary.py (reuse bounded lifecycle guard),
tests/rest_stt_stageb_probe.py; tests/rest_stt_stageb_parent.py.
Run: `/opt/homebrew/anaconda3/bin/python3 -I -B KrabEar/tests/rest_stt_stageb_parent.py <external-output-dir>`.
Parent hard test limit35s; only owned group cleanup, hashes and actual receipt validated.

Причинный RED2: шесть error cases status200 + два silence200, violations0. RED3 добавляет
реальную scratch-history invariance: общий профиль записывает error payload в историю.
GREEN: шесть503/stable stt_failed/redacted text, два legitimate200; history invariant
для ошибок, восемь настоящих executor workers завершены, ни provider import, ни сеть.

Граница: transcriber и normalize_audio заменены синтетическими leaf. Не ASR/STT fidelity,
не upload decoding/model/hardware/PSTN E2E, не nested native drain. Deadline механика
не менялась и отдельной новой timeout acceptance пока нет. Старый Stage A401/403 не
выдаётся за новую Stage B проверку. Ошибка начальной fixture из-за mimetypes system read
не считается causal RED; устранена явным audio/wav MIME без расширения read allowlist.

## Расширение отрицательных контрактов после c8a0e565

20 реальных HTTP запросов /18 реальных executor workers: прежние8 плюс
4privacy(toggle/unreadable ×general/call),4empty_audio/null-error-success controls,
2deterministic call-deadline/privacy precedence,2invalid-upload pre-worker400.
Errors/privacy/deadline/invalid сохраняют scratchhistory/metrics; нормальный общий
результат действительно добавляется в scratchhistory, callprofile не сохраняется.
Пустой результат StateStore не добавляет как новую запись — первая assertion на+1
была ошибкой fixture и исправлена, production не менялась. Deadline движется
syntheticclock внутри worker; реального ожидания/hard-exit timer не создаётся.
EXPANDED2 PASS, violations0,20cases/18workers joined, parent reaped/groupempty,
hashstable. Исходный10-line gate неизменён; новое sourceповедение не добавлялось.
