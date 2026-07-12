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
    s.settimeout(600)
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

    r = call(sock, "meeting_start")
    check("meeting_start ok", r.get("ok") and r["result"].get("ok"), str(r)[:200])

    time.sleep(30)  # один CHUNK_STT-такт (default 25с)
    st = call(sock, "get_meeting_live_state")["result"]
    check("state active", st.get("active") is True, str(st)[:200])
    check("no crash in degraded", isinstance(st.get("degraded"), dict))
    print(f"    transcript_len={st.get('transcript_len')} tail={st.get('transcript_tail', '')[:80]!r}")

    r = call(sock, "meeting_stop")
    check("meeting_stop ok", r.get("ok") and r["result"].get("ok"), str(r)[:200])
    print(f"    item_id={r['result'].get('item_id')}")

    st2 = call(sock, "get_meeting_live_state")["result"]
    check("inactive after stop", st2.get("active") is False)

    print("\n" + ("ALL GREEN" if not fails else f"FAILS: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
