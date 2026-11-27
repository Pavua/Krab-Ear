#!/usr/bin/env python3
"""E2E privacy-gate canary: verify privacy_mode_enabled actually suppresses
transcript-derived content end-to-end over the live socket.

Strategy (canary word): seed history items containing a unique secret token,
enable privacy_mode_enabled via set_settings, then call every transcript-derived
("gated") IPC method and assert the secret token appears NOWHERE in the response
(the strongest possible leak test) AND the response is shaped as a safe/empty
gate. Then disable privacy and confirm at least one method surfaces the token
again (proving the canary is real, not a false-clean). Restores the original
privacy setting and deletes the seeded items at the end — in a ``finally``, so a
crash mid-run cannot leave privacy switched or canary records in the history.

Run against a THROWAWAY/dev backend (it seeds + toggles privacy). The socket path
is a REQUIRED argument; the production socket is refused outright:
    python scripts/e2e_privacy_gates.py /path/to/throwaway/krabear.sock
Exit 0 if no leak, 1 if any gated method leaks the secret or any check fails
(including confirmations that privacy actually switched), 2 on a refused/unsafe start.
"""
import json
import os
import socket
import sys

PRODUCTION_SOCK = os.path.expanduser("~/Library/Application Support/KrabEar/krabear.sock")

SECRET = "ЗЕБРАСЕКРЕТКАНАРЕЙКА42E2E"  # distinctive, unlikely to be tokenized away

SOCK = ""


def call(method, params, timeout=40):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall((json.dumps({"id": f"pg-{method}", "method": method, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.split(b"\n", 1)[0])


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
    ("extract_action_items", {"id": None}),  # id filled from seed
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


def check(cond, label):
    """Print AND record: a failed confirmation must fail the run (it used to be print-only)."""
    print(f"  {'OK ' if cond else 'LEAK/FAIL'}  {label}")
    results.append((label, bool(cond)))
    return bool(cond)


def response_contains_secret(resp):
    """True if the secret token appears anywhere in the JSON-serialized response."""
    return SECRET in json.dumps(resp, ensure_ascii=False)


def gate_answered(resp):
    """True, если метод ОТВЕТИЛ (есть ``result`` и нет ``error``).

    G3-privacy-05 (05-11): раньше проверялось только отсутствие канарейки, а
    ответ ``{"error": ...}`` (неизвестный метод, внутренняя ошибка, таймаут
    гейта) её тоже не содержит — сломанный или переименованный метод считался
    «защищённым». Теперь пустой/ошибочный ответ — провал проверки.
    """
    return (isinstance(resp, dict) and not resp.get("error")
            and resp.get("result") is not None)


def secret_paths(value, path="", limit=5):
    """Пути полей ответа, где встретилась канарейка (``result.items[0]``).

    G3-privacy-05 (05-08): при утечке печатаем ГДЕ, а не ЧТО. Раньше в вывод
    шли 200 символов сырого ответа IPC — вместе с канарейкой туда попадало всё,
    что backend вернул рядом (тексты истории, имена, ключи словаря).
    """
    found = []

    def walk(node, where):
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if SECRET in str(key):
                    found.append(f"{where}.<key>" if where else "<key>")
                walk(child, f"{where}.{key}" if where else str(key))
        elif isinstance(node, (list, tuple)):
            for idx, child in enumerate(node):
                walk(child, f"{where}[{idx}]")
        elif isinstance(node, str) and SECRET in node:
            found.append(where or "<root>")

    walk(value, path)
    return found


def refuse(message):
    print(f"REFUSED: {message}", file=sys.stderr)
    sys.exit(2)


def parse_socket_arg(argv):
    if len(argv) < 2 or not argv[1].strip():
        refuse("socket path is required (usage: e2e_privacy_gates.py /path/to/throwaway/krabear.sock). "
               "There is no default: this script seeds history and toggles privacy.")
    path = argv[1]
    if os.path.realpath(path) == os.path.realpath(PRODUCTION_SOCK):
        refuse(f"{path} is the PRODUCTION socket — the canary seeds records and toggles privacy; "
               "use a throwaway backend (scripts/run_e2e_smokes.command starts one).")
    return path


def read_privacy():
    """Текущее значение privacy_mode_enabled или None, если его не удалось определить."""
    value = (call("get_settings", {}).get("result") or {}).get("privacy_mode_enabled")
    return value if isinstance(value, bool) else None


def run(seed_ids):
    print("=== SEED history with the canary secret ===")
    for i in range(4):
        text = f"Обычный текст номер {i}. {SECRET} встречается здесь явно для проверки утечки."
        r = call("add_history_item", {"text": text, "paste_status": "pasted"})
        iid = (r.get("result") or {}).get("id")
        if iid:
            seed_ids.append(iid)
    print(f"  seeded {len(seed_ids)} items containing the secret")
    if not seed_ids:
        print("  FATAL: could not seed")
        return 2

    print("\n=== Enabling privacy_mode_enabled ===")
    call("set_settings", {"privacy_mode_enabled": True})
    # settings cache is 5s TTL — verify it took; without this confirmation the run proves nothing
    if not check(read_privacy() is True, "set_settings privacy_mode_enabled=True took effect"):
        print("  FATAL: privacy switch not confirmed — gated checks would be meaningless")
        return 1

    print("\n=== With privacy ON: NO gated method may echo the secret ===")
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
        check(gate_answered(resp), f"{method}: answered with a result (not an error/empty reply)")
        check(not leaked, f"{method}: secret NOT leaked under privacy"
              + (f"  <<< LEAK in: {', '.join(secret_paths(resp)) or '<serialized>'}" if leaked else ""))

    # Canary validity: with privacy OFF, the secret MUST resurface somewhere
    # (proves the test isn't trivially clean because the secret never made it in).
    print("\n=== Disabling privacy: secret MUST resurface (canary validity) ===")
    call("set_settings", {"privacy_mode_enabled": False})
    check(read_privacy() is False, "privacy_mode_enabled=False restored for canary check")
    probe = call("search_history", {"query": SECRET, "limit": 20})
    check(response_contains_secret(probe), "search_history surfaces the secret with privacy OFF (canary is real)")
    return 0


def main(argv):
    global SOCK
    SOCK = parse_socket_arg(argv)

    # Исходное значение читаем ДО любых изменений. Неизвестно → восстанавливаем в безопасное True.
    orig = read_privacy()
    restore_to = orig if orig is not None else True
    print(f"original privacy_mode_enabled: {orig if orig is not None else 'UNKNOWN (will restore ON)'}")

    seed_ids = []
    rc = 1
    try:
        rc = run(seed_ids)
    finally:
        print(f"\n=== Restoring privacy_mode_enabled={restore_to} and deleting canary items ===")
        try:
            call("set_settings", {"privacy_mode_enabled": restore_to})
        except Exception as exc:
            print(f"  WARNING: could not restore privacy: {exc}", file=sys.stderr)
        for iid in seed_ids:
            try:
                call("delete_history_item", {"id": iid})
            except Exception as exc:
                print(f"  WARNING: could not delete canary item {iid}: {exc}", file=sys.stderr)

    if rc == 2:
        return 2
    print("\n=== PRIVACY-GATE SUMMARY ===")
    all_ok = True
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        all_ok &= ok
    print(f"\n  {sum(1 for _, ok in results if ok)}/{len(results)} checks PASSED")
    good = all_ok and rc == 0
    print("=== " + ("ALL PRIVACY GATES HOLD" if good else "PRIVACY LEAK / GATE FAILURE") + " ===")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
