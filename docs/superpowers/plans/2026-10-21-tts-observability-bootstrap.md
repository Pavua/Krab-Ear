# TTS observability bootstrap — reviewable runbook proposal, NOT executable approval

## Verified source and remaining gap

#2107 merged3f08c9dc has server IPC stages; #2108 OPEN head09bbd0fd has TTS handler
metadata. Both exact-head CI sets pass; #2108 not merged/deployed. Last qualified
production snapshot0ff399bc: backend31618/REST63152, Swift20131. These PID/path
values are dated observations, never inputs to a future stop command without
rechecking. No lifecycle or configuration change is authorized by this document.

Old0ff REST exposes nativeSTT activity only. It has no supported GET of its live
TTS handler reservations. New source counter starts with the new process; it
cannot attest old in-flight work. Searching log terminals, sampling six fast IPC
queries or waiting one STT keepalive cadence does not close this information gap.
HTTP503 may be busy OR unavailable. Even new handler idle does not attest detached
model loading/direct bytes API/background GPU release; coverage stays false.

## Evidence gates, in order

1. Prepare reviewed source/release/rollback candidates; source trees/CI/SHA match.
   Preserve canonical dirty app/WIP. For #2108, source merge/postmerge CI requires
   its own serial ACK; no merge or new CI is assumed here.
2. Establish participating producer ownership without payload/audio/credentials.
   Accept owner/client receipt with UTC, client PID+start, method/route, request ID,
   completion status, retry policy. Correlate brief TCP peers if naturally present;
   absence during a bounded sample is not a no-producer certificate. Main/VG denial
   of their own TTS does not identify another client. Do not stop unknown processes.
3. Obtain producer ACK that no new TTS/WS/batch is submitted in the eventual window,
   finish accepted calls, and verify current transports/known descendants and
   complete terminal receipts. A quiesced known producer plus complete accounting
   may establish drain; silence/elapsed timeout without accounting may not.
   If a request, caller, accepted non-SSE socket or tail is unknown: HOLD before
   bootout. Do not use a fresh request or model load as a liveness/drain probe.
4. Qualify the running Swift build, active SSE consumers and a recovery path.
   Bind PID/start/app binary identity; list each active subscription/filter and
   its owner lifetime from existing trustworthy metadata. TCP count alone and
   matching counts of historical GET200 do not identify subscriptions. No memory
   dumps, packet bodies or live-debug injection to recover content/closures.
5. After ALL reversible preparations above, obtain a fresh owner no-dictation/
   meeting/voice-TTS window and Main/VG serial ACK; old "сейчас можно" expires.
   Recheck current PIDs/config, resources over an agreed observation window,
   history count/digest/encryption/restore, four safe idle probes≥60s and final probe.
   Existing synthetic STT keepalive exception is separate and must be applicable
   to this window. It does not authorize unknown TTS interruption.
6. Only with gates closed, reviewed two-unit cutover from the existing safe-release
   plan. Repeat REST completion/socket/worker gates after backend has stopped.
   Stop/replace/bootstrap with stage-specific recovery, no unconditional trap/kill.
7. New exact PID/epoch/readiness: TTS `known:true`, `reservations:0`, `queued:0`,
   `inflight:0`, `unknown:0`, `coverage_lost:false` over the qualified idle window.
   `generation_scope:tts_handler`, process/epoch stable. Simultaneous nativeSTT
   component qualified separately; owner generation/global coverage still unknown.
   This is counter acceptance, not evidence that the old process was idle.
8. Restore each enumerated SSE owner using its qualified path. Need HTTP2xx plus
   accepted SSE MIME, correct owning lifetime/connection generation and a naturally
   occurring keepalive/control event (no audio/text export). A bounded runtime window
   confirms reconnect/MIME/control-flow and no observed retry storm; it cannot prove
   absence of all stale callbacks or duplicate business effects. Those negative
   scenarios require the source regression tests. Runtime business-effect acceptance
   remains NOT_TESTED without its corresponding explicitly authorized scenario.
   Health200 alone fails.
9. Observe≥60s stable BOTH process readiness, required natural keepalive completion,
   no restart loop, history parity. Then Main unique UUID idle client↔server stage
   correlation, separately its thinking payload tests; fresh resource gate before
   any agreed27B admission. ES still deferred to memory stability.

## Narrow proposed exception: enumerated passive SSE only

Existing draft gate "no accepted5005 TCP connections at all" would reject genuine
idle SSE forever. Proposed replacement, only after independent operational review:
"no working or unattributed connections; individually attested GET /v1/events
streams may remain until REST stop, each with a known post-bootstrap recovery".

Scope: exactly the enumerated passive streams of the verified Swift incarnation,
not POST/WS/STT/TTS, new clients, model work or untracked tails. Risk: events during
downtime have no replay, UI may remain stale if recovery fails. Mitigation: fresh
quiet window, per-owner recovery already tested/available, no migration/recording,
and explicit acceptance of the bounded UI-event gap. If current Swift lacks a
recovery path, use a separately reviewed known Ear Swift release/restart plan;
that must not be substituted with stopping any unidentified TTS producer.

Rollback: server plist rollback follows the existing staged plan and the same
fresh idle gates. Reverting server code does NOT restore disconnected Swift
streams or replay missing events; execute the same qualified UI recovery for
every enumerated owner. Busy/unknown new process prevents automatic rollback.
If UI recovery is unavailable, reject this SSE exception before initial stop.

## Explicitly rejected exceptions

- TTS UNKNOWN → assumed idle after N seconds / no recent logs / health200.
- Interrupt unknown TTS once "to install its counter"; old keepalive allowance
  is not a permission for that interruption.
- Hot reload/monkey patch/attach-to-process injection, auth/config manipulation,
  blanket port fencing or stopping an unknown client as diagnostic shortcuts.
- Readiness/new counter returning idle used as a model-release/27B grant.

If producer ownership/old drain cannot be established using existing capabilities,
bootstrap remains technically unqualified. State exactly that missing fact to the
owner; no SSE source fix removes it, and this plan does not conceal it as a
temporary warning. No runtime START or exception execution accompanies this plan.
