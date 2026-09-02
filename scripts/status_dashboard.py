#!/usr/bin/env python3
"""Сборщик состояния экосистемы Краба для наглядной панели.

ЗАЧЕМ
-----
Владелец держит три проекта (Krab Ear / главный Краб / Voice Gateway), в них
идут параллельные сессии Claude и не-Claude воркеры (agy, cursor-agent, codex).
Понять «кто работает, кто умер, где ошибка» из терминала невозможно: воркеры
буферизуют вывод, сессии не видны, CI в браузере.

Скрипт собирает всё в один JSON. Рендер — отдельно (см. --html).

🔴 Принцип: НИКОГДА не выдавать отсутствие данных за успех. Каждый раздел
несёт собственный статус: ok / warn / fail / unknown. «unknown» — честный
исход, когда источник недоступен, и он ОТЛИЧАЕТСЯ от «всё хорошо».
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import time

REPO = pathlib.Path("/Users/pablito/Antigravity_AGENTS/Krab Ear")
WORKER_DIRS = {
    "Krab Ear": REPO / ".remember" / "tmp" / "workers",
    "Главный Краб": pathlib.Path("/Users/pablito/Antigravity_AGENTS/Краб/.remember/tmp/workers"),
    "Voice Gateway": pathlib.Path("/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway/.remember/tmp/workers"),
}
PROJECTS = {
    "Krab Ear": REPO,
    "Главный Краб": pathlib.Path("/Users/pablito/Antigravity_AGENTS/Краб"),
    "Voice Gateway": pathlib.Path("/Users/pablito/Antigravity_AGENTS/Krab Voice Gateway"),
}


def _run(cmd: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def collect_workers() -> dict:
    """Воркеры: живой процесс + размер лога + признак завершения.

    🔴 Ноль байт НЕ означает смерть: cursor-agent буферизует весь вывод до
    конца. Поэтому живость берём из ps, а не из размера файла — иначе панель
    будет врать ровно про рабочего воркера.
    """
    rc, ps_out = _run(["ps", "ax", "-o", "command"])
    alive = {
        "agy": ps_out.count("antigravity") if rc == 0 else None,
        "cursor-agent": ps_out.count("cursor-agent/versions") if rc == 0 else None,
        "codex": ps_out.count("codex exec") if rc == 0 else None,
    }
    logs = []
    for project, wdir in WORKER_DIRS.items():
        if not wdir.is_dir():
            continue
        for f in sorted(wdir.glob("out_*.log"), key=lambda p: -p.stat().st_mtime):
            try:
                body = f.read_text(errors="replace")
            except OSError:
                body = ""
            tail = body[-600:]
            rc_match = re.search(r"DONE rc=(\d+)", tail)
            done = rc_match is not None
            # Что воркер СДЕЛАЛ: считаем маркеры отчёта из наших брифов.
            findings = len(re.findall(r"^ФАЙЛ:|^ЦЕПОЧКА:", body, re.M))
            empty_report = "НАХОДОК НЕТ" in body
            # 🔴 Ноль байт ≠ смерть: cursor-agent копит вывод до конца.
            # Поэтому «идёт» определяем по отсутствию маркера DONE, а не по размеру.
            status = ("fail" if rc_match and rc_match.group(1) != "0"
                      else "ok" if done else "running")
            logs.append({
                "project": project,
                "name": f.stem.replace("out_", ""),
                "engine": ("agy" if "agy" in f.stem else
                           "cursor" if "cur" in f.stem else
                           "codex" if "codex" in f.stem else "?"),
                "task": f.stem.replace("out_", "").split("_", 1)[-1],
                "bytes": f.stat().st_size,
                "age_min": round((time.time() - f.stat().st_mtime) / 60, 1),
                "done": done,
                "exit_code": int(rc_match.group(1)) if rc_match else None,
                "findings": findings,
                "empty_report": empty_report,
                "status": status,
            })
    return {"engines_alive": alive, "logs": logs}


def collect_prs() -> dict:
    rc, out = _run([
        "gh", "pr", "list", "--repo", "Pavua/Krab-Ear", "--state", "open",
        "--json", "number,title,statusCheckRollup,mergeStateStatus", "--limit", "20",
    ], timeout=60)
    if rc != 0:
        return {"status": "unknown", "error": out.strip()[:200], "items": []}
    try:
        raw = json.loads(out)
    except ValueError:
        return {"status": "unknown", "error": "не JSON", "items": []}
    items = []
    for pr in raw:
        checks = pr.get("statusCheckRollup") or []
        fail = sum(1 for c in checks if c.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED"))
        pend = sum(1 for c in checks if c.get("status") == "IN_PROGRESS" or c.get("status") == "QUEUED")
        ok = sum(1 for c in checks if c.get("conclusion") == "SUCCESS")
        items.append({
            "number": pr["number"], "title": pr["title"][:80],
            "ok": ok, "fail": fail, "pending": pend,
            "merge_state": pr.get("mergeStateStatus"),
            "status": "fail" if fail else ("running" if pend else "ok"),
        })
    return {"status": "ok", "items": items}


def collect_done() -> dict:
    """Что реально сделано за сутки — смерженные PR. Это «результат», а не
    «активность»: открытый PR ещё ничего не изменил в продукте."""
    rc, out = _run([
        "gh", "pr", "list", "--repo", "Pavua/Krab-Ear", "--state", "merged",
        "--limit", "12", "--json", "number,title,mergedAt",
    ], timeout=60)
    if rc != 0:
        return {"status": "unknown", "items": []}
    try:
        raw = json.loads(out)
    except ValueError:
        return {"status": "unknown", "items": []}
    today = time.strftime("%Y-%m-%d")
    items = [{"number": p["number"], "title": p["title"][:90],
              "merged": (p.get("mergedAt") or "")[:16].replace("T", " ")}
             for p in raw if (p.get("mergedAt") or "").startswith(today)]
    return {"status": "ok", "items": items}


def collect_sessions() -> dict:
    """Живые сессии Claude по сокетам — их видно, даже если имена не резолвятся."""
    socks = pathlib.Path("/tmp/cc-socks")
    if not socks.is_dir():
        return {"status": "unknown", "count": 0, "note": "каталог сокетов недоступен"}
    live = [p.name for p in socks.glob("*.sock")]
    return {"status": "ok", "count": len(live), "sockets": sorted(live)[:40]}


def collect_machine() -> dict:
    rc, out = _run(["uptime"])
    load = None
    if rc == 0:
        m = re.search(r"load averages?:\s*([\d.]+)", out)
        if m:
            load = float(m.group(1))
    status = "unknown"
    if load is not None:
        status = "ok" if load < 20 else ("warn" if load < 60 else "fail")
    return {"status": status, "load_1min": load, "raw": out.strip()[:120]}


def collect_prod() -> dict:
    """Прод-процессы Krab Ear: backend, REST, Swift-агент."""
    out = {}
    for label, pattern in (("backend", "KrabEar/main.py"),
                           ("rest", "rest_server.py"),
                           ("agent", "KrabEarAgent")):
        rc, res = _run(["pgrep", "-f", pattern])
        pid = res.strip().split("\n")[0] if rc == 0 and res.strip() else None
        info = {"pid": pid, "status": "ok" if pid else "fail"}
        if pid:
            rc2, ps = _run(["ps", "-o", "etime=,rss=", "-p", pid])
            if rc2 == 0 and ps.strip():
                parts = ps.split()
                info["uptime"] = parts[0]
                info["rss_mb"] = round(int(parts[1]) / 1024) if len(parts) > 1 else None
        out[label] = info
    return out


def collect_projects() -> dict:
    """Свежесть хэндоффов соседних проектов — видно, кто чем занят."""
    res = {}
    for name, path in PROJECTS.items():
        rem = path / ".remember"
        if not rem.is_dir():
            res[name] = {"status": "unknown", "note": "нет .remember"}
            continue
        files = sorted(rem.glob("*.md"), key=lambda p: -p.stat().st_mtime)[:3]
        res[name] = {
            "status": "ok",
            "recent": [{"file": f.name,
                        "age_min": round((time.time() - f.stat().st_mtime) / 60, 1)}
                       for f in files],
        }
    return res


# ---------------------------------------------------------------------------
# Рендер и сервер.
#
# 🔴 Почему сервер, а не статический файл: статика — это СНИМОК, её надо
# пересобирать руками, и владелец сразу упёрся именно в это («не совсем
# понятно как обновлять»). Сервер пересобирает данные на КАЖДЫЙ запрос, а
# страница сама себя перезагружает — вкладку достаточно открыть один раз.
# ---------------------------------------------------------------------------

_CSS = """
:root{--ground:#F6F3EF;--card:#FFF;--edge:#E0D9D1;--ink:#1B1F21;--soft:#5C6467;
--faint:#8B9295;--accent:#D9583E;--ok:#2E7D51;--warn:#B57614;--fail:#C13B30;
--idle:#78838A;--okw:#E7F2EB;--warnw:#FBF0DC;--failw:#FAE7E4;--idlew:#EDF0F1}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--ground:#101416;--card:#171D20;--edge:#2A3236;--ink:#E9EEF0;--soft:#A3AEB3;
--faint:#737E83;--accent:#F0755A;--ok:#5CC189;--warn:#E0A93C;--fail:#F2695C;
--idle:#8A959B;--okw:#16281F;--warnw:#2B2214;--failw:#2E1917;--idlew:#1D2427}}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--ink);margin:0;padding:26px 20px 60px;
font-family:"IBM Plex Sans",system-ui,sans-serif;font-size:18px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px 20px}
h1{font-family:"Bricolage Grotesque","IBM Plex Sans",sans-serif;font-weight:800;
font-size:38px;margin:0;letter-spacing:-.02em}
.stamp{font-family:"IBM Plex Mono",monospace;font-size:15px;color:var(--faint);
font-variant-numeric:tabular-nums}
.verdict{padding:20px 24px;border-radius:14px;border:2px solid var(--edge);
border-left-width:10px;background:var(--card)}
.verdict.calm{border-left-color:var(--ok);background:var(--okw)}
.verdict.attention{border-left-color:var(--warn);background:var(--warnw)}
.verdict.bad{border-left-color:var(--fail);background:var(--failw)}
.vt{font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:28px;margin:0}
.vn{color:var(--soft);font-size:17px;margin:5px 0 0}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:var(--card);border:1px solid var(--edge);border-radius:14px;
padding:18px 20px;display:flex;flex-direction:column;gap:12px}
.card h2{font-family:"Bricolage Grotesque",sans-serif;font-weight:600;font-size:13px;
letter-spacing:.1em;text-transform:uppercase;color:var(--faint);margin:0}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;
padding:9px 0;border-bottom:1px solid var(--edge)}
.row:last-child{border-bottom:none;padding-bottom:0}
.nm{font-weight:500}
.meta{font-family:"IBM Plex Mono",monospace;font-size:14px;color:var(--faint);
font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:14px;font-weight:600;
padding:4px 11px;border-radius:999px;border:1.5px solid currentColor;white-space:nowrap}
.pill::before{content:"";width:8px;height:8px;background:currentColor}
.pill.ok{color:var(--ok);background:var(--okw)}.pill.ok::before{border-radius:999px}
.pill.warn{color:var(--warn);background:var(--warnw)}.pill.warn::before{transform:rotate(45deg)}
.pill.fail{color:var(--fail);background:var(--failw)}.pill.fail::before{width:10px;height:3px}
.pill.running{color:var(--accent)}.pill.running::before{border-radius:999px;animation:p 1.5s infinite}
.pill.idle{color:var(--idle);background:var(--idlew)}
@keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
@media(prefers-reduced-motion:reduce){.pill.running::before{animation:none}}
.big{font-family:"IBM Plex Mono",monospace;font-weight:600;font-size:32px;
font-variant-numeric:tabular-nums}
.big.ok{color:var(--ok)}.big.warn{color:var(--warn)}.big.fail{color:var(--fail)}
.unit{font-size:15px;color:var(--faint)}
.sub{font-size:15px;color:var(--soft);margin-top:2px}
footer{border-top:1px solid var(--edge);padding-top:16px;color:var(--soft);font-size:16px;
display:flex;flex-direction:column;gap:7px}
code{font-family:"IBM Plex Mono",monospace;font-size:14px;background:var(--idlew);
padding:2px 6px;border-radius:5px}
.acc{color:var(--accent);font-weight:600}
"""


def _pill(status: str, label: str | None = None) -> str:
    words = {"ok": "готово", "running": "работает", "fail": "сбой",
             "warn": "внимание", "idle": "—", "unknown": "неизвестно"}
    cls = status if status in ("ok", "running", "fail", "warn", "idle") else "idle"
    return f'<span class="pill {cls}">{label or words.get(status, status)}</span>'


def render_html(d: dict, refresh_sec: int = 20) -> str:
    m, prod, w = d["machine"], d["prod"], d["workers"]
    load = m.get("load_1min")

    fails = [x for x in w["logs"] if x["status"] == "fail"]
    running = [x for x in w["logs"] if x["status"] == "running"]
    pr_fail = [p for p in d["prs"]["items"] if p["status"] == "fail"]
    prod_down = [k for k, v in prod.items() if v["status"] != "ok"]

    if prod_down or pr_fail:
        vcls, vt = "bad", "Требует внимания"
        if prod_down:
            vn = "Прод не в порядке: " + ", ".join(prod_down)
        else:
            nums = ", ".join("#" + str(p["number"]) for p in pr_fail)
            vn = f"Красные проверки в PR: {nums}"
    elif (load or 0) > 60 or fails:
        vcls, vt = "attention", "Работает, но есть шум"
        parts = []
        if (load or 0) > 60:
            parts.append(f"машина загружена ({load})")
        if fails:
            parts.append(f"воркеров со сбоем: {len(fails)}")
        vn = "; ".join(parts)
    else:
        vcls, vt = "calm", "Всё спокойно"
        vn = f"Прод целиком жив, красных проверок нет. Воркеров в работе: {len(running)}."

    def rows_prod():
        names = {"backend": "Backend", "rest": "REST", "agent": "Swift-агент"}
        out = []
        for k, v in prod.items():
            meta = f"{v.get('rss_mb', '?')} МБ · {v.get('uptime', '?')}" if v["status"] == "ok" else "процесс не найден"
            out.append(f'<div class="row"><span class="nm">{names.get(k, k)}</span>'
                       f'<span class="meta">{meta}</span>'
                       f'{_pill("ok" if v["status"] == "ok" else "fail", "жив" if v["status"] == "ok" else "НЕТ")}</div>')
        return "".join(out)

    def rows_workers():
        if not w["logs"]:
            return '<div class="sub">Воркеров не запускалось.</div>'
        out = []
        for x in w["logs"][:10]:
            if x["status"] == "running":
                res = f'идёт {x["age_min"]} мин'
            elif x["empty_report"]:
                res = "отчёт пуст (честно)"
            elif x["findings"]:
                res = f'находок: {x["findings"]}'
            else:
                res = f'{x["bytes"] // 1024} КБ'
            out.append(
                f'<div class="row"><div><span class="nm">{x["task"]}</span>'
                f'<div class="sub">{x["project"]} · {x["engine"]}</div></div>'
                f'<span class="meta">{res}</span>{_pill(x["status"])}</div>')
        return "".join(out)

    def rows_engines():
        alive = w["engines_alive"]
        labels = {"agy": "agy · Gemini", "cursor-agent": "cursor · Grok", "codex": "codex · GPT"}
        out = []
        for k, cnt in alive.items():
            st = "ok" if (cnt or 0) > 0 else "idle"
            cnt_txt = cnt if cnt is not None else "?"
            label = "доступен" if st == "ok" else "простаивает"
            out.append(
                f'<div class="row"><span class="nm">{labels.get(k, k)}</span>'
                f'<span class="meta">процессов: {cnt_txt}</span>'
                f'{_pill(st, label)}</div>'
            )
        return "".join(out)

    def rows_prs():
        if not d["prs"]["items"]:
            return '<div class="sub">Открытых PR нет.</div>'
        out = []
        for p in d["prs"]["items"]:
            checks = f'<span style="color:var(--ok)">{p["ok"]}</span>'
            if p["fail"]:
                checks += f' · <span style="color:var(--fail)">{p["fail"]} красных</span>'
            if p["pending"]:
                checks += f' · <span style="color:var(--accent)">{p["pending"]} идут</span>'
            out.append(f'<div class="row"><div><span class="nm">#{p["number"]}</span>'
                       f'<div class="sub">{p["title"]}</div></div>'
                       f'<span class="meta">{checks}</span>{_pill(p["status"])}</div>')
        return "".join(out)

    def rows_done():
        items = d.get("done_today", {}).get("items", [])
        if not items:
            return '<div class="sub">Сегодня ничего не смержено.</div>'
        return "".join(
            f'<div class="row"><div><span class="nm">#{i["number"]}</span>'
            f'<div class="sub">{i["title"]}</div></div>'
            f'<span class="meta">{i["merged"][11:]}</span>{_pill("ok", "в проде")}</div>'
            for i in items)

    load_cls = "ok" if (load or 0) < 20 else ("warn" if (load or 0) < 60 else "fail")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh_sec}">
<title>Пульт Краба</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>{_CSS}</style></head><body><div class="wrap">
<header><h1>Пульт Краба</h1><span class="stamp">{d['generated_at']} · сам обновляется каждые {refresh_sec} с</span></header>
<div class="verdict {vcls}"><p class="vt">{vt}</p><p class="vn">{vn}</p></div>
<div class="grid">
<section class="card"><h2>Прод Krab Ear</h2>{rows_prod()}</section>
<section class="card"><h2>Машина</h2>
<div style="display:flex;align-items:baseline;gap:10px">
<span class="big {load_cls}">{load}</span><span class="unit">средняя загрузка</span></div>
<div class="sub">Норма до 20. Выше 60 — CI теряет связь с GitHub и падает без логов.</div>
<div class="row"><span class="nm">Живых сессий Claude</span>{_pill("idle", str(d['sessions'].get('count', '?')))}</div>
</section>
<section class="card"><h2>Движки воркеров</h2>{rows_engines()}</section>
<section class="card"><h2>Задания воркеров</h2>{rows_workers()}</section>
<section class="card"><h2>Открытые PR</h2>{rows_prs()}</section>
<section class="card"><h2>Сделано сегодня</h2>{rows_done()}</section>
</div>
<footer>
<div><span class="acc">Страница живая:</span> данные пересобираются на каждый запрос, вкладку можно просто оставить открытой.</div>
<div><span class="acc">Ноль байт у воркера — не смерть:</span> cursor-agent копит вывод до конца, поэтому «работает» определяется по отсутствию маркера завершения, а не по размеру файла.</div>
</footer></div></body></html>"""


def serve(port: int) -> int:
    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            data = {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "machine": collect_machine(), "prod": collect_prod(),
                "workers": collect_workers(), "prs": collect_prs(),
                "sessions": collect_sessions(), "projects": collect_projects(),
                "done_today": collect_done(),
            }
            body = render_html(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass  # без шума в терминале

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"Пульт: http://127.0.0.1:{port}  (Ctrl+C чтобы остановить)")
        httpd.serve_forever()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="выдать JSON вместо текста")
    ap.add_argument("--serve", nargs="?", type=int, const=8777,
                    help="поднять живой пульт на http://127.0.0.1:PORT (по умолчанию 8777)")
    args = ap.parse_args()

    if args.serve:
        return serve(args.serve)

    data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": collect_machine(),
        "prod": collect_prod(),
        "workers": collect_workers(),
        "prs": collect_prs(),
        "sessions": collect_sessions(),
        "projects": collect_projects(),
        "done_today": collect_done(),
    }
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    m = data["machine"]
    print(f"МАШИНА: load {m['load_1min']} [{m['status']}]")
    print("ПРОД:", ", ".join(f"{k}={'жив' if v['status'] == 'ok' else 'НЕТ'}"
                             for k, v in data["prod"].items()))
    w = data["workers"]
    print(f"ДВИЖКИ: {w['engines_alive']}")
    for lg in w["logs"][:8]:
        print(f"  воркер {lg['name']:<20} {lg['status']:<8} {lg['bytes']:>8} байт  {lg['age_min']} мин")
    for pr in data["prs"]["items"]:
        print(f"  PR #{pr['number']:<5} {pr['status']:<8} ok={pr['ok']} fail={pr['fail']} pend={pr['pending']}  {pr['title'][:50]}")
    print(f"СЕССИЙ ЖИВЫХ: {data['sessions'].get('count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
