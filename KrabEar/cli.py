"""Krab Ear CLI — command-line interface for power users.

Usage:
    python -m KrabEar.cli status
    python -m KrabEar.cli history [--limit N]
    python -m KrabEar.cli export [--format srt|md|obsidian] [--output FILE]
    python -m KrabEar.cli stats
    python -m KrabEar.cli health
    python -m KrabEar.cli transcribe FILE
    python -m KrabEar.cli interactive
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    import readline as _readline
    _READLINE_AVAILABLE = True
except ImportError:
    _readline = None  # type: ignore[assignment]
    _READLINE_AVAILABLE = False

# ─── ANSI colors ─────────────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


# ─── IPC transport ───────────────────────────────────────────────────────────

_DEFAULT_SOCKET_PATHS = [
    Path.home() / "Library" / "Application Support" / "KrabEar" / "krabear.sock",
    Path.home() / ".krab_ear_data" / "krabear.sock",
    Path.home() / ".krab_ear_data" / "backend.sock",
]


def _resolve_socket(sock_path: str | None = None) -> Path:
    if sock_path:
        return Path(sock_path).expanduser()
    env = os.environ.get("KRAB_EAR_SOCKET")
    if env:
        return Path(env).expanduser()
    for candidate in _DEFAULT_SOCKET_PATHS:
        if candidate.exists():
            return candidate
    return _DEFAULT_SOCKET_PATHS[0]


def _ipc_call(method: str, params: dict | None = None, sock_path: str | None = None) -> dict[str, Any]:
    """Send a single JSON-RPC request over Unix socket and return the result dict."""
    path = _resolve_socket(sock_path)
    payload = json.dumps({"id": str(uuid.uuid4()), "method": method, "params": params or {}}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(path))
            s.sendall(payload.encode())
            chunks: list[bytes] = []
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).decode().strip()
            return json.loads(raw)
    except FileNotFoundError:
        _die(f"Socket not found: {path}\nIs Krab Ear backend running?")
    except ConnectionRefusedError:
        _die(f"Connection refused at {path}\nIs Krab Ear backend running?")
    except socket.timeout:
        _die("Backend did not respond in time (timeout=10s)")
    except json.JSONDecodeError as e:
        _die(f"Malformed response from backend: {e}")


def _die(msg: str) -> None:
    print(red("Error: ") + msg, file=sys.stderr)
    sys.exit(1)


def _unwrap(resp: dict[str, Any]) -> Any:
    if not resp.get("ok"):
        err = resp.get("error", {})
        _die(f"Backend error [{err.get('code', '?')}]: {err.get('message', resp)}")
    return resp.get("result", {})


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> None:
    resp = _ipc_call("ping", sock_path=getattr(args, "socket", None))
    r = _unwrap(resp)
    print(bold("Krab Ear Backend Status"))
    print(f"  {bold('Service:')}  {green(r.get('service', '?'))}")
    print(f"  {bold('Version:')}  {r.get('version', '?')}")
    print(f"  {bold('Uptime:')}   {r.get('uptime_sec', '?')}s")
    recording = r.get("is_recording", False)
    rec_label = red("● RECORDING") if recording else dim("○ idle")
    print(f"  {bold('Recording:')} {rec_label}")
    print(f"  {bold('History:')}  {r.get('history_count', '?')} items")

    # Extended diagnostics
    resp2 = _ipc_call("get_diagnostics", sock_path=getattr(args, "socket", None))
    if resp2.get("ok"):
        d = resp2["result"]
        sys_info = d.get("system", {})
        stt = d.get("stt", {})
        print()
        print(bold("System"))
        print(f"  Platform:  {sys_info.get('platform', '?')}")
        print(f"  Data dir:  {sys_info.get('data_dir', '?')}")
        print(bold("STT"))
        print(f"  Model:     {stt.get('model', '?')}")
        print(f"  Profile:   {stt.get('quality_profile', '?')}")


def cmd_history(args: argparse.Namespace) -> None:
    limit = getattr(args, "limit", 20) or 20
    resp = _ipc_call("get_history_page", {"page": 0, "page_size": limit}, sock_path=getattr(args, "socket", None))
    r = _unwrap(resp)
    items = r.get("items", [])
    if not items:
        print(dim("No history items found."))
        return
    print(bold(f"Recent History ({len(items)} items)"))
    print()
    for item in items:
        ts = item.get("created_at", item.get("timestamp", ""))
        text = item.get("text", "").replace("\n", " ")
        if len(text) > 100:
            text = text[:97] + "..."
        lang = item.get("lang", "")
        confidence = item.get("confidence")
        conf_str = f"  {dim(f'[{confidence:.0%}]')}" if confidence is not None else ""
        lang_str = f"  {cyan(f'[{lang}]')}" if lang else ""
        print(f"{dim(ts[:19])}  {text}{lang_str}{conf_str}")


def cmd_export(args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "md") or "md"
    output = getattr(args, "output", None)
    sock = getattr(args, "socket", None)

    method_map = {
        "srt": "export_history_srt",
        "md": "export_history_markdown",
        "obsidian": "export_obsidian",
    }
    method = method_map.get(fmt)
    if not method:
        _die(f"Unknown format: {fmt}. Use srt, md, or obsidian.")

    resp = _ipc_call(method, {}, sock_path=sock)
    r = _unwrap(resp)
    content = r.get("content", r.get("data", ""))

    if output:
        Path(output).write_text(content, encoding="utf-8")
        print(green(f"Exported to {output}"))
    else:
        print(content)


def cmd_stats(args: argparse.Namespace) -> None:
    sock = getattr(args, "socket", None)
    resp = _ipc_call("get_history_statistics", {}, sock_path=sock)
    r = _unwrap(resp)

    print(bold("History Statistics"))
    for key, val in r.items():
        label = key.replace("_", " ").title()
        print(f"  {bold(label + ':')}  {val}")

    print()
    resp2 = _ipc_call("get_metrics_dashboard", {}, sock_path=sock)
    if resp2.get("ok"):
        m = resp2["result"]
        print(bold("Metrics Dashboard"))
        stt_m = m.get("stt", {})
        if stt_m:
            print(f"  {bold('STT latency p50:')}   {stt_m.get('latency_p50_ms', '?')} ms")
            print(f"  {bold('STT latency p95:')}   {stt_m.get('latency_p95_ms', '?')} ms")
            print(f"  {bold('STT confidence:')}    {stt_m.get('confidence_avg', '?')}")
        storage = m.get("storage", {})
        if storage:
            print(f"  {bold('History size:')}      {storage.get('history_bytes', '?')} bytes")


def cmd_health(args: argparse.Namespace) -> None:
    resp = _ipc_call("health_check", {}, sock_path=getattr(args, "socket", None))
    r = _unwrap(resp)

    overall = r.get("status", "unknown")
    color_fn = green if overall == "ok" else (yellow if overall == "degraded" else red)
    print(bold("Health Check: ") + color_fn(overall.upper()))

    checks = r.get("checks", {})
    for name, info in checks.items():
        if isinstance(info, dict):
            status = info.get("status", "?")
            detail = info.get("detail", "")
            status_color = green if status == "ok" else (yellow if status == "degraded" else red)
            detail_str = f"  {dim(detail)}" if detail else ""
            print(f"  {bold(name + ':')} {status_color(status)}{detail_str}")
        else:
            print(f"  {bold(name + ':')} {info}")


def cmd_transcribe(args: argparse.Namespace) -> None:
    file_path = args.file
    sock = getattr(args, "socket", None)
    if not Path(file_path).exists():
        _die(f"File not found: {file_path}")

    print(f"Transcribing {cyan(file_path)} ...")
    resp = _ipc_call("transcribe_paths", {"paths": [str(Path(file_path).resolve())]}, sock_path=sock)
    r = _unwrap(resp)
    results = r.get("results", [r])
    for res in results:
        text = res.get("text", res.get("transcript", ""))
        lang = res.get("lang", "")
        confidence = res.get("confidence")
        print()
        if lang:
            print(f"{bold('Language:')} {cyan(lang)}")
        if confidence is not None:
            print(f"{bold('Confidence:')} {confidence:.1%}")
        print(bold("Transcript:"))
        print(text)


# ─── Interactive REPL ────────────────────────────────────────────────────────

_REPL_HELP = """
Available commands:
  status              Show backend status and diagnostics
  history [--limit N] List recent transcription history
  export [--format F] Export history (formats: srt, md, obsidian)
  stats               Show usage statistics
  health              Health check all subsystems
  transcribe FILE     Transcribe an audio file
  search QUERY        Quick search through history
  last                Show last transcription
  clear               Clear the screen
  help                Show this help message
  quit / exit         Exit the REPL
"""

_REPL_COMMANDS = [
    "status", "history", "export", "stats", "health", "transcribe",
    "search", "last", "clear", "help", "quit", "exit",
]


def _setup_readline() -> None:
    """Configure readline with tab completion and history if available."""
    if not _READLINE_AVAILABLE or _readline is None:
        return
    try:
        def _completer(text: str, state: int) -> str | None:
            matches = [c for c in _REPL_COMMANDS if c.startswith(text)]
            return matches[state] if state < len(matches) else None

        _readline.set_completer(_completer)
        _readline.parse_and_bind("tab: complete")
    except Exception:
        pass


def _repl_last(sock_path: str | None) -> None:
    """Print the most recent transcription entry."""
    resp = _ipc_call("get_history_page", {"page": 0, "page_size": 1}, sock_path=sock_path)
    if not resp.get("ok"):
        print(yellow("Could not retrieve history."))
        return
    items = resp["result"].get("items", [])
    if not items:
        print(dim("No transcriptions found."))
        return
    item = items[0]
    ts = item.get("created_at", item.get("timestamp", ""))
    text = item.get("text", "")
    lang = item.get("lang", "")
    confidence = item.get("confidence")
    print(bold("Last transcription:"))
    if ts:
        print(f"  {bold('Time:')}  {dim(ts[:19])}")
    if lang:
        print(f"  {bold('Lang:')}  {cyan(lang)}")
    if confidence is not None:
        print(f"  {bold('Conf:')}  {confidence:.1%}")
    print(f"  {bold('Text:')}  {text}")


def _repl_search(query: str, sock_path: str | None) -> None:
    """Search history items for QUERY (case-insensitive substring match)."""
    if not query.strip():
        print(yellow("Usage: search QUERY"))
        return
    resp = _ipc_call("get_history_page", {"page": 0, "page_size": 200}, sock_path=sock_path)
    if not resp.get("ok"):
        print(yellow("Could not retrieve history."))
        return
    items = resp["result"].get("items", [])
    q = query.lower()
    hits = [it for it in items if q in it.get("text", "").lower()]
    if not hits:
        print(dim(f"No results for '{query}'."))
        return
    print(bold(f"Search results for '{query}' ({len(hits)} found):"))
    print()
    for item in hits:
        ts = item.get("created_at", item.get("timestamp", ""))
        text = item.get("text", "").replace("\n", " ")
        if len(text) > 120:
            text = text[:117] + "..."
        print(f"{dim(ts[:19])}  {text}")


def _dispatch_repl_line(line: str, sock_path: str | None) -> bool:
    """Parse and execute a single REPL input line.

    Returns True to continue, False to exit the loop.
    """
    parts = line.strip().split()
    if not parts:
        return True
    cmd, rest = parts[0].lower(), parts[1:]

    if cmd in ("quit", "exit"):
        print(dim("Goodbye."))
        return False

    if cmd == "clear":
        print("\033[2J\033[H", end="")
        return True

    if cmd == "help":
        print(_REPL_HELP)
        return True

    if cmd == "last":
        _repl_last(sock_path)
        return True

    if cmd == "search":
        _repl_search(" ".join(rest), sock_path)
        return True

    # For the remaining commands we re-use the existing cmd_* functions by
    # constructing a fake Namespace, which avoids duplicating logic.
    ns = argparse.Namespace(socket=sock_path)

    if cmd == "status":
        try:
            cmd_status(ns)
        except SystemExit:
            pass
        return True

    if cmd == "history":
        # accept optional --limit N
        limit = 20
        if "--limit" in rest:
            try:
                limit = int(rest[rest.index("--limit") + 1])
            except (ValueError, IndexError):
                pass
        ns.limit = limit
        try:
            cmd_history(ns)
        except SystemExit:
            pass
        return True

    if cmd == "export":
        fmt = "md"
        output = None
        if "--format" in rest:
            try:
                fmt = rest[rest.index("--format") + 1]
            except IndexError:
                pass
        if "--output" in rest:
            try:
                output = rest[rest.index("--output") + 1]
            except IndexError:
                pass
        ns.format = fmt
        ns.output = output
        try:
            cmd_export(ns)
        except SystemExit:
            pass
        return True

    if cmd == "stats":
        try:
            cmd_stats(ns)
        except SystemExit:
            pass
        return True

    if cmd == "health":
        try:
            cmd_health(ns)
        except SystemExit:
            pass
        return True

    if cmd == "transcribe":
        if not rest:
            print(yellow("Usage: transcribe FILE"))
            return True
        ns.file = rest[0]
        try:
            cmd_transcribe(ns)
        except SystemExit:
            pass
        return True

    print(yellow(f"Unknown command: {cmd!r}. Type 'help' for a list of commands."))
    return True


def cmd_interactive(args: argparse.Namespace) -> None:
    """Start an interactive REPL for Krab Ear CLI."""
    sock_path = getattr(args, "socket", None)

    _setup_readline()

    prompt = cyan("krab-ear") + dim("> ") if _USE_COLOR else "krab-ear> "

    print(bold("Krab Ear Interactive REPL"))
    print(dim("Type 'help' for available commands, 'quit' to exit."))
    print()

    while True:
        try:
            try:
                line = input(prompt)
            except EOFError:
                # Ctrl+D
                print()
                print(dim("Goodbye."))
                break
            if not _dispatch_repl_line(line, sock_path):
                break
        except KeyboardInterrupt:
            # Ctrl+C — cancel current input but stay in loop
            print()
            continue


# ─── Argument parser ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="krab-ear",
        description="Krab Ear CLI — command-line access to the voice assistant backend",
    )
    parser.add_argument(
        "--socket",
        metavar="PATH",
        default=None,
        help="Unix socket path (default: auto-detect)",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # status
    p_status = sub.add_parser("status", help="Show backend status and diagnostics")
    p_status.set_defaults(func=cmd_status)

    # history
    p_hist = sub.add_parser("history", help="List recent transcription history")
    p_hist.add_argument("--limit", type=int, default=20, metavar="N", help="Number of items (default: 20)")
    p_hist.set_defaults(func=cmd_history)

    # export
    p_exp = sub.add_parser("export", help="Export history to a file or stdout")
    p_exp.add_argument(
        "--format",
        choices=["srt", "md", "obsidian"],
        default="md",
        help="Export format (default: md)",
    )
    p_exp.add_argument("--output", metavar="FILE", default=None, help="Output file path (default: stdout)")
    p_exp.set_defaults(func=cmd_export)

    # stats
    p_stats = sub.add_parser("stats", help="Show usage statistics")
    p_stats.set_defaults(func=cmd_stats)

    # health
    p_health = sub.add_parser("health", help="Health check all subsystems")
    p_health.set_defaults(func=cmd_health)

    # transcribe
    p_tx = sub.add_parser("transcribe", help="Transcribe an audio file")
    p_tx.add_argument("file", metavar="FILE", help="Audio file path")
    p_tx.set_defaults(func=cmd_transcribe)

    # interactive
    p_int = sub.add_parser("interactive", help="Start an interactive REPL session")
    p_int.set_defaults(func=cmd_interactive)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
