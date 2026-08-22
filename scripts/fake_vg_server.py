#!/usr/bin/env python3
"""Fake Voice Gateway для e2e Call Observer w1 (spec §6).

Реализует ровно потребляемое подмножество контракта VG:
GET /v1/sessions, WS /v1/sessions/<id>/stream (скриптованные события),
WS /v1/sessions/<id>/monitor/audio (metadata + μ-law синус 440Гц),
GET /v1/sessions/<id>/diagnostics, POST /v1/telephony/calls/<id>/hangup.

Таймлайн: сессия появляется через 1с после старта, события идут по сценарию,
звонок живёт до hangup или 60с. Порт — argv[1] (default 18090).
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time

from flask import Flask, jsonify
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

START_TS = time.time()
SESSION_ID = "e2e-call-1"
STATE = {"status": "running", "hangup_calls": 0, "started": START_TS + 1.0}
LOCK = threading.Lock()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _session_row() -> dict:
    with LOCK:
        return {
            "id": SESSION_ID, "status": STATE["status"], "phone": "+34600111222",
            "call_direction": "outbound", "created_at": _now_iso(),
            "updated_at": _now_iso(), "src_lang": "es", "tgt_lang": "ru",
            "source": "twilio_pstn_outbound", "call_brief": "e2e",
        }


@app.get("/v1/sessions")
def list_sessions():
    if time.time() < STATE["started"]:
        return jsonify({"ok": True, "count": 0, "items": []})
    return jsonify({"ok": True, "count": 1, "items": [_session_row()]})


@app.get(f"/v1/sessions/{SESSION_ID}/diagnostics")
def diagnostics():
    # Реальный VG мержит diag на верхний уровень: {**diag, "status": ...}.
    return jsonify({"ok": True, "status": STATE["status"], "timeline_size": 3,
                    "costs": {"total_usd": 0.07,
                              "breakdown": {"twilio": 0.05, "ai": 0.02}}})


@app.post(f"/v1/telephony/calls/{SESSION_ID}/hangup")
def hangup():
    with LOCK:
        STATE["hangup_calls"] += 1
        already = STATE["status"] in {"stopped", "failed"}
        STATE["status"] = "stopped"
    return jsonify({"ok": True, "session_id": SESSION_ID, "call_sid": "CA-e2e",
                    "status": "completed", "already_terminal": already})


@app.get("/e2e/hangup_count")
def hangup_count():
    return jsonify({"count": STATE["hangup_calls"]})


_EVENTS = [
    (0.2, "call.state", {"session_id": SESSION_ID, "status": "running"}),
    (0.2, "stt.final", {"text": "hola, quería preguntar", "engine": "e2e",
                        "confidence": 0.9, "duration_ms": 900, "language": "es"}),
    (0.2, "translation.final", {"text": "привет, хотел спросить", "source_text": "hola, quería preguntar",
                                "src_lang": "es", "tgt_lang": "ru", "provider": "e2e"}),
    (0.2, "agent.response", {"text": "Claro, dígame", "text_ru": "Конечно, слушаю",
                             "role": "assistant", "lang": "es", "utterance_ts": "u1",
                             "action": "continue", "goal_reached": False, "summary": ""}),
    (0.2, "agent.suggestion.auto_spoken", {"text": "Uno momento", "text_ru": "Минуту",
                                           "action": "continue", "digits": "",
                                           "goal_reached": False, "summary": "", "result": ""}),
    (0.2, "agent.interrupted", {"utterance_ts": "u1", "spoken_fraction": 0.4,
                                "spoken_text": "Claro, dí"}),
    (0.2, "weird.unknown_event", {"x": 1}),  # forward-compat: клиент обязан молча съесть
]


@sock.route(f"/v1/sessions/{SESSION_ID}/stream")
def stream(ws):
    for delay, etype, data in _EVENTS:
        time.sleep(delay)
        with LOCK:
            terminal = STATE["status"] in {"stopped", "failed"}
        if terminal:
            break
        ws.send(json.dumps({"type": etype, "ts": _now_iso(), "data": data}))
    # Ждём hangup (или 60с), затем терминальная цепочка.
    deadline = time.time() + 60
    while time.time() < deadline:
        with LOCK:
            if STATE["status"] in {"stopped", "failed"}:
                break
        time.sleep(0.1)
    ws.send(json.dumps({"type": "call.ended", "ts": _now_iso(),
                        "data": {"reason": "hangup", "provider": "e2e"}}))
    ws.send(json.dumps({"type": "call.closed", "ts": _now_iso(),
                        "data": {"session_id": SESSION_ID}}))
    ws.close()


def _mulaw_encode(sample: int) -> int:
    """Стандартный G.711 μ-law encode (audioop удалён из Python 3.13)."""
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_SINE_FRAME = bytes(
    _mulaw_encode(int(6000 * math.sin(2 * math.pi * 440 * i / 8000)))
    for i in range(800)
)


@sock.route(f"/v1/sessions/{SESSION_ID}/monitor/audio")
def monitor(ws):
    ws.send(json.dumps({"format": "mulaw_8k", "frame_ms": 100}))
    for _ in range(600):  # до 60с
        with LOCK:
            if STATE["status"] in {"stopped", "failed"}:
                break
        ws.send(_SINE_FRAME)
        time.sleep(0.1)
    ws.close(1000)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18090
    app.run(host="127.0.0.1", port=port, threaded=True)
