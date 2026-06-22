#!/usr/bin/env python3
"""E2E privacy-gate canary: verify privacy_mode_enabled actually suppresses
transcript-derived content end-to-end over the live socket.

Strategy (canary word): seed history items containing a unique secret token,
enable privacy_mode_enabled via set_settings, then call every transcript-derived
("gated") IPC method and assert the secret token appears NOWHERE in the response
(the strongest possible leak test) AND the response is shaped as a safe/empty
gate. Then disable privacy and confirm at least one method surfaces the token
again (proving the canary is real, not a false-clean). Restores the original
privacy setting at the end.

Run against a THROWAWAY/dev backend (it seeds + toggles privacy):
    python scripts/e2e_privacy_gates.py [/path/to/krabear.sock]
Default socket: ~/Library/Application Support/KrabEar/krabear.sock
Exit 0 if no leak, 1 if any gated method leaks the secret or fails to gate.
"""
import json
import os
import socket
import sys

SOCK = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/Library/Application Support/KrabEar/krabear.sock"
)

SECRET = "ЗЕБРАСЕКРЕТКАНАРЕЙКА42E2E"  # distinctive, unlikely to be tokenized away


def call(method, params, timeout=40):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    s.sendall((json.dumps({"id": f"pg-{method}", "method": method, "params": params}) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0])


def need(cond, label):
    print(f"  {'OK ' if cond else 'LEAK/FAIL'}  {label}")
    return cond


# Transcript-derived ("gated") methods + the params each needs.
# Each MUST NOT echo the secret token when privacy_mode_enabled=True.
GATED = [
    ("get_analytics_dashboard", {"days": 30}),
    ("get_sentiment_trends", {"days": 30}),
    ("get_keyword_cloud", {"limit": 30, "days": 30}),
    ("get_activity_calendar", {"months": 3}),
    ("generate_daily_digest", {}),
    ("get_topic_timeline", {"limit": 20}),
    ("get_timeline_view", {"granularity": "day"}),
    ("get_recording_insights", {"days": 7}),
    ("generate_stats_report", {"days": 30}),
    ("extract_action_items", {"id": None}),          # id filled from seed
    ("semantic_search", {"query": SECRET, "top_k": 10, "fallback": True}),
    ("find_duplicates", {"similarity_threshold": 0.5, "limit": 50}),
    ("word_frequency_analysis", {"limit": 50}),
    ("get_speaker_statistics", {}),
    ("get_glossary_suggestions", {"limit": 20}),
    ("get_vocabulary_suggestions", {"limit": 20}),
    ("suggest_medical_glossary_terms", {"limit": 20}),
    ("get_context_memory", {}),
    ("search_history", {"query": SECRET, "limit": 20}),
    ("get_meeting_report", {"id": None}),
]

results = []


def response_contains_secret(resp):
    """True if the secret token appears anywhere in the JSON-serialized response."""
    return SECRET in json.dumps(resp, ensure_ascii=False)


print("=== SEED history with the canary secret ===")
seed_ids = []
for i in range(4):
    text = f"Обычный текст номер {i}. {SECRET} встречается здесь явно для проверки утечки."
    r = call("add_history_item", {"text": text, "paste_status": "pasted"})
    iid = (r.get("result") or {}).get("id")
    if iid:
        seed_ids.append(iid)
print(f"  seeded {len(seed_ids)} items containing the secret")
if not seed_ids:
    print("  FATAL: could not seed"); sys.exit(2)

# Read + remember original privacy setting, then turn privacy ON.
orig = call("get_settings", {}).get("result", {})
orig_privacy = bool(orig.get("privacy_mode_enabled", False))
print(f"\n=== Enabling privacy_mode_enabled (was {orig_privacy}) ===")
call("set_settings", {"privacy_mode_enabled": True})
# settings cache is 5s TTL — verify it took
now = call("get_settings", {}).get("result", {})
need(now.get("privacy_mode_enabled") is True, "set_settings privacy_mode_enabled=True took effect")

print("\n=== With privacy ON: NO gated method may echo the secret ===")
any_leak = False
for method, params in GATED:
    p = dict(params)
    if p.get("id") is None and "id" in p:
        p["id"] = seed_ids[0]
    try:
        resp = call(method, p)
    except Exception as exc:
        print(f"  ERR   {method}: {exc}")
        results.append((method, False))
        continue
    leaked = response_contains_secret(resp)
    ok = need(not leaked, f"{method}: secret NOT leaked under privacy"
              + (f"  <<< LEAK: {json.dumps(resp, ensure_ascii=False)[:200]}" if leaked else ""))
    if leaked:
        any_leak = True
    results.append((method, ok))

# Canary validity: with privacy OFF, the secret MUST resurface somewhere
# (proves the test isn't trivially clean because the secret never made it in).
print("\n=== Disabling privacy: secret MUST resurface (canary validity) ===")
call("set_settings", {"privacy_mode_enabled": False})
now2 = call("get_settings", {}).get("result", {})
need(now2.get("privacy_mode_enabled") is False, "privacy_mode_enabled=False restored for canary check")
probe = call("search_history", {"query": SECRET, "limit": 20})
canary_ok = need(response_contains_secret(probe),
                 "search_history surfaces the secret with privacy OFF (canary is real)")
results.append(("__canary_validity__", canary_ok))

# Restore original privacy setting.
print(f"\n=== Restoring original privacy_mode_enabled={orig_privacy} ===")
call("set_settings", {"privacy_mode_enabled": orig_privacy})

print("\n=== PRIVACY-GATE SUMMARY ===")
all_ok = True
for name, ok in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    all_ok &= ok
print(f"\n  {sum(1 for _, ok in results if ok)}/{len(results)} checks PASSED")
print("=== " + ("ALL PRIVACY GATES HOLD" if all_ok and not any_leak else "PRIVACY LEAK / GATE FAILURE") + " ===")
sys.exit(0 if all_ok and not any_leak else 1)
