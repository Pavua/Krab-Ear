"""Проверки lab wire codec до подключения владельца журнала."""

import json
import struct

import pytest

from codec import decode_request


def frame(payload):
    return struct.pack("!I", len(payload)) + payload


def test_valid_reserve_request():
    request = {"v": 0, "op": "reserve", "request_id": "req-1",
               "client_incarnation": "client-1", "kind": "stt", "executor": "worker-1"}
    assert decode_request(frame(json.dumps(request).encode())) == request


@pytest.mark.parametrize("message", [
    {"v": 0, "op": "status", "request_id": "r", "client_incarnation": "c"},
    {"v": 0, "op": "claim", "request_id": "r", "client_incarnation": "c",
     "executor": "worker", "grant_id": "12345678-1234-4234-8234-123456789abc"},
])
def test_other_valid_requests(message):
    assert decode_request(frame(json.dumps(message).encode())) == message


@pytest.mark.parametrize("payload", [
    b'{"v":0,"v":0,"op":"status","request_id":"r","client_incarnation":"c"}',
    b'{"v":true,"op":"status","request_id":"r","client_incarnation":"c"}',
    b'{"v":0,"op":"status","request_id":"r","client_incarnation":"c","x":NaN}',
    b'{"v":0,"op":"status","request_id":"r","client_incarnation":"c","x":1e309}',
    b'{"v":0,"op":"status","request_id":"\\ud800","client_incarnation":"c"}',
    b'{"v":0,"op":"status","request_id":"","client_incarnation":"c"}',
    b'{"v":0,"op":"status","request_id":"r","client_incarnation":"c","extra":1}',
    b'{"v":0,"op":"invalid","request_id":"r","client_incarnation":"c"}',
    b'{"v":0,"op":"claim","request_id":"r","client_incarnation":"c","executor":"e","grant_id":"not-a-uuid"}',
    b'{"v":0,"op":"reserve","request_id":"r","client_incarnation":"c","kind":[],"executor":"e"}',
    b'{"v":0,"op":"status","request_id":"two words","client_incarnation":"c"}',
    b'\xff',
])
def test_invalid_payload_is_rejected(payload):
    with pytest.raises(ValueError):
        decode_request(frame(payload))


@pytest.mark.parametrize("raw", [b"", b"\x00\x00\x00", b"\x00\x00\x00\x00",
                                 struct.pack("!I", 65537), frame(b"{}") + b"extra"])
def test_invalid_frame_is_rejected(raw):
    with pytest.raises(ValueError):
        decode_request(raw)
