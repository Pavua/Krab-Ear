# TTS handler activity — Implementation Plan

**Goal:** auth readiness наблюдает занятый TTS handler, даже когда native STT idle.
**База:** origin/codex/krab-ear-v2 @ 3f08c9dc; isolated ear-tts-activity.
**Architecture:** повторно использовать bounded ActivityReservations; отдельный
process-wide observer TTS handler. Семафор допуска и результаты TTS не менять.
Snapshot nonblocking, overflow/fork/exception semantics прежнего observer.
Scope только handler (включая cleanup); direct bytes API и background futures
остаются вне coverage. Никаких текста, голоса, params, auth, путей или caller claims.

**Баны:** runtime/config/ML/dependencies/Swift/Main/Gateway/clone/OrbStack lifecycle;
никакого probe-acquire TTS admission semaphore; coverage.complete остаётся false;
без busy→idle release grant, without TTS producer attribution or SSE claims.

- [x] RED actual TTS handler с event-controlled synthetic backend: cross-instance
  busy и cleanup, rejection не очищает активный вызов, exception возвращает idle.
- [x] RED actual auth Flask readiness: STT idle/TTS busy, unknown provider,
  metadata после auth; no production listener/audio/model.
- [x] GREEN bounded observer с параметрами scope и decorator through run/finally;
  TTSService.activity_snapshot и readiness через actual deps TTS instance.
- [x] Tests: новый файл, activity diagnostics/wiring/completion, TTS admission/service,
  STT residency REST; последовательные отдельные процессы. Ruff/diff/audit.
- [x] Независимый Astra adversarial whole-diff SOURCE PASS.
- [ ] Exact-head hosted CI после serial ACK Main.
- [ ] PR/receipt; runtime release/27B/ES остаются отдельной приёмкой.

## Source checkpoint

110 tests PASS в8 свежих Python3.14 процессах, включая10 новых; Ruff/diff/audit-all
PASS. Python3.12 stdlib decorator/error/contention smoke PASS. Полный local Ubuntu
parity harness не запускался: /tmp/py312 отсутствует, ML/dependencies в memory HOLD
не устанавливаются. Полный Ubuntu suite проверяется hosted CI. Source RED7 по
отсутствующим API/wiring; дополнительная regression lookup failure RED→GREEN.
Astra whole-diff SOURCE PASS. Ни модели, ни runtime, ни конфигурация не менялись.
