#!/usr/bin/env python3
"""Живой e2e-смок C2a: meeting-сессия против THROWAWAY backend.

Запуск (руками, НЕ CI):
  python KrabEar/main.py --data-dir /tmp/krab_ear_meeting_e2e &   # throwaway
  python3 scripts/e2e_meeting_smoke.py /tmp/krab_ear_meeting_e2e/krabear.sock

Проверяет: start -> активная сессия -> транскрипт растёт (реальный CHUNK_STT
по микрофону ЛИБО тишина -> len==0, оба валидны, важно отсутствие ошибок) ->
stop -> финальный history_id -> сессия неактивна. items требуют LM Studio —
проверяются мягко (degraded.llm допустим).
"""
import json
import socket
import sys
import time


def call(sock_path: str, method: str, params: dict | None = None) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # 90s: comfortably above the 30s worst-case internal wait
    # (MeetingSessionService._stop_worker()'s thread join), but still fails
    # fast instead of hanging the operator for up to 10 minutes on a genuine
    # backend deadlock. Mirrors scripts/e2e_ipc_smoke.py's 30s default for the
    # same kind of local Unix-socket round trip.
    s.settimeout(90)
    s.connect(sock_path)
    s.sendall(json.dumps({"id": "e2e", "method": method,
                          "params": params or {}}).encode() + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode())


def main() -> int:
    sock = sys.argv[1] if len(sys.argv) > 1 else "/tmp/krab_ear_meeting_e2e/krabear.sock"
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("OK  " if cond else "FAIL") + f" {name} {detail}")
        if not cond:
            fails.append(name)

    # Everything from here on runs against a LIVE meeting session (open mic +
    # meeting-gpu-slot worker thread + held brain-lease). try/finally
    # guarantees a best-effort meeting_stop even if a transient socket error,
    # a malformed/truncated response, or Ctrl-C during the sleep interrupts
    # us before the real meeting_stop call below — otherwise the session
    # would keep recording indefinitely on the target backend.
    stopped = False
    try:
        r = call(sock, "meeting_start")
        res = r.get("result", {})
        check("meeting_start ok", r.get("ok") and res.get("ok"), str(r)[:200])

        time.sleep(30)  # один CHUNK_STT-такт (default 25с)
        st = call(sock, "get_meeting_live_state").get("result", {})
        check("state active", st.get("active") is True, str(st)[:200])
        check("no crash in degraded", isinstance(st.get("degraded"), dict))
        print(f"    transcript_len={st.get('transcript_len')} tail={st.get('transcript_tail', '')[:80]!r}")

        r = call(sock, "meeting_stop")
        res = r.get("result", {})
        check("meeting_stop ok", r.get("ok") and res.get("ok"), str(r)[:200])
        print(f"    item_id={res.get('item_id')}")
        # Reuse the exact condition just checked above — not a bare literal.
        # If meeting_stop's RPC round-trip succeeds but the handler raises
        # AFTER _stop_worker() (worker already dead) and BEFORE
        # _teardown_session(...) (e.g. handle_stop_recording() blows up during
        # finalization), handle_request returns {"ok": false, "error": {...}}
        # with no "result" key — check() correctly reports FAIL, and stopped
        # must stay False so the finally block retries the cleanup stop.
        stopped = bool(r.get("ok")) and bool(res.get("ok"))

        st2 = call(sock, "get_meeting_live_state").get("result", {})
        check("inactive after stop", st2.get("active") is False)
    finally:
        if not stopped:
            print("!!  interrupted before a confirmed meeting_stop — issuing best-effort cleanup stop")
            try:
                call(sock, "meeting_stop")
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup, must not mask the original error
                print(f"!!  cleanup meeting_stop failed (session may still be live): {exc}")

    print("\n" + ("ALL GREEN" if not fails else f"FAILS: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
