#!/usr/bin/env python3
"""Krab Ear IPC end-to-end smoke client.

Exercises the user-facing IPC surface against a LIVE backend over a Unix socket.
Goal: catch "feature runs but output is wrong/empty" bugs before manual testing.

Run against a LIVE backend (production launchd instance, or a dev instance you
started with `python KrabEar/main.py --data-dir <dir>`):

    python scripts/e2e_ipc_smoke.py [/path/to/krabear.sock]

Default socket: ~/Library/Application Support/KrabEar/krabear.sock (production).
Dev instance socket: <data-dir>/krabear.sock (e.g. /tmp/krab_ear_e2e/krabear.sock).

It SEEDS ~10 history items into whatever store the target backend uses, so run it
against a throwaway/dev instance unless you intend to add those items. Exit 0 if
all method checks pass, 1 otherwise. Asserts OUTPUT SANITY (shape + non-trivial
content where data exists), tolerating privacy gates and legitimate emptiness.

=== METHODS COVERED (28) ===
  SEED / INFRA
    add_history_item            — seeds 10 varied items; collects ids
    ping                        — liveness + uptime fields
    handshake                   — version + capabilities exchange
  ANALYTICS
    get_analytics_dashboard     — full analytics aggregation
    get_sentiment_trends        — daily sentiment + mood trend
    get_keyword_cloud           — word cloud data
    get_activity_calendar       — GitHub-style activity heatmap
    generate_daily_digest       — markdown digest (date=today)
    get_topic_timeline          — topic shift timeline
    get_recording_insights      — heuristic insights
    compare_periods             — two-period comparison (analytics_service)
    get_timeline_view           — history grouped by time blocks
    generate_stats_report       — full Markdown stats report
  ACTION ITEMS / LLM
    extract_action_items        — tasks/decisions/questions from item_id
    summarize_item              — LLM/heuristic summary by item_id
    summarize_text              — lightweight summary of raw text
    get_meeting_report          — meeting report orchestrator
  SEARCH / DUPLICATES
    semantic_search             — embedding search with keyword fallback
    find_duplicates             — duplicate groups by text similarity
    word_frequency_analysis     — word count histogram
    search_history              — full-text history search
  SPEAKER / TRANSLATION
    get_speaker_statistics      — per-speaker stats (privacy-gated)
    translate_text              — RU/ES/EN offline translation
    get_glossary_suggestions    — glossary pairs from translation history
    get_vocabulary_suggestions  — STT vocab suggestions from history
  EXPORT
    export_history              — full Markdown export
    export_history_srt          — SRT for one item
  DIAGNOSTICS
    get_diagnostics             — system/stt/llm/history/settings_cache
    get_metrics_dashboard       — real-time metrics snapshot
  MISC
    analyze_speech_pace         — wpm/cpm/pace_category from text+duration
    get_recording_stats         — cumulative recording stats
    compare_recordings          — side-by-side recording comparison
    auto_summarize_batch        — batch LLM summary for ids

=== METHODS SKIPPED ===
  get_last_llm_diff             — requires active LLM rewriter session; diff is empty
                                  without a real STT+rewrite cycle; no stable fixture
  test_microphone / check_mic_noise — synchronous audio capture; no safe way to call
                                  without grabbing the microphone for 2 s; out of scope
  export_history_srt (standalone) — covered with real id in the export section
  start_recording / stop_recording — require real audio I/O; not for smoke
  live_subs_ingest / live_subs_* — require streaming audio; not for smoke
  call_session_* / call_assist_* — require Telnyx/Twilio credentials; not for smoke
  apple_integration (send_to_telegram / send_imessage etc.) — side-effects on
                                  live services; not for smoke
  purge_all_data                — destructive; would erase all history and break
                                  subsequent checks; explicitly excluded
"""

import json
import socket
import sys
import time
import datetime

import os
SOCK = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
    "~/Library/Application Support/KrabEar/krabear.sock"
)

# ---------------------------------------------------------------------------
# Transport helpers (copied from krab_ear_e2e_client.py)
# ---------------------------------------------------------------------------

def call(method: str, params: dict, timeout: int = 30) -> dict:
    """Send a single JSON-RPC request over the Unix socket and return the parsed response."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    req = json.dumps({"id": f"smoke-{method}", "method": method, "params": params}) + "\n"
    s.sendall(req.encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def need(cond: bool, label: str) -> bool:
    """Print a single OK/FAIL assertion line and return the bool."""
    print(f"  {'OK ' if cond else 'FAIL'}  {label}")
    return cond


# ---------------------------------------------------------------------------
# State shared across sections
# ---------------------------------------------------------------------------

SEED_IDS: list[str] = []
RESULTS: list[tuple[str, bool]] = []  # (method_name, pass/fail)


def run_check(method_name: str, fn):
    """Run a check function, catch exceptions, record result."""
    try:
        ok = fn()
        RESULTS.append((method_name, bool(ok)))
    except Exception as exc:
        print(f"  FAIL  {method_name}: EXCEPTION: {exc}")
        RESULTS.append((method_name, False))


# ===========================================================================
# SECTION 0 — Seed realistic history data
# ===========================================================================
print("\n=== SECTION 0: Seed history data ===")

SEED_TEXTS = [
    # RU — long multi-sentence
    ("Сегодня на совещании обсудили квартальные результаты. "
     "Продажи выросли на 15% по сравнению с прошлым кварталом. "
     "Команда договорилась запустить новую маркетинговую кампанию в январе.", "ru", 45.0, "pasted"),
    # ES — medium
    ("Buenos días, necesito confirmar la reunión de mañana a las diez. "
     "Por favor, trae el informe de ventas del último trimestre.", "es", 12.5, "pasted"),
    # EN — medium
    ("Please review the pull request before end of day. "
     "The CI pipeline is green and all tests pass.", "en", 8.0, "pasted"),
    # RU — short
    ("Позвони мне после пяти вечера, когда освобожусь.", "ru", 3.0, "pasted"),
    # ES — long
    ("El informe de auditoría ha sido completado. "
     "Se identificaron tres áreas de mejora: gestión de inventarios, "
     "control de costos y optimización de procesos de producción.", "es", 30.0, "failed"),
    # EN — longer with tech terms
    ("The microservice architecture requires careful consideration of latency "
     "and throughput. We should evaluate gRPC vs REST for the internal API. "
     "Observability must be built in from day one: traces, metrics, logs.", "en", 60.0, "pasted"),
    # RU — meeting-style
    ("Обсудили задачи на следующий спринт. Иван берёт на себя бэкенд, "
     "Мария — фронт, Алексей занимается DevOps. "
     "Дедлайн — следующая пятница.", "ru", 25.0, "pasted"),
    # RU — question-heavy
    ("Когда мы выпустим новую версию приложения? "
     "Есть ли блокирующие задачи? Нужно ли согласование с юридическим отделом?", "ru", 18.0, "pasted"),
    # ES — short one-liner
    ("Reunión cancelada por motivos de fuerza mayor.", "es", 4.0, "failed"),
    # EN — analytics heavy
    ("Q3 revenue reached 2.4 million dollars, up 22 percent year over year. "
     "Customer acquisition cost dropped to 35 dollars. "
     "Net promoter score improved from 42 to 61.", "en", 35.0, "pasted"),
]

for text, lang, dur, paste_status in SEED_TEXTS:
    try:
        r = call("add_history_item", {
            "text": text,
            "lang": lang,
            "duration_sec": dur,
            "paste_status": paste_status,
        })
        item_id = r.get("result", r).get("id")  # handle both {id} and {result:{id}}
        if not item_id:
            # add_history_item returns the HistoryItem dict directly as result
            item_id = (r.get("result") or r).get("id")
        if item_id:
            SEED_IDS.append(item_id)
            print(f"  OK   seeded id={item_id[:12]}... lang={lang} dur={dur}s")
        else:
            print(f"  WARN  add_history_item returned no id: {r}")
    except Exception as exc:
        print(f"  WARN  add_history_item failed: {exc}")

print(f"  INFO  Total seeded: {len(SEED_IDS)} items")

if len(SEED_IDS) < 2:
    print("  FATAL: too few seeded items — cannot run comparison/meeting checks. Aborting.")
    sys.exit(2)

ID_A = SEED_IDS[0]
ID_B = SEED_IDS[1]
ID_LONG = SEED_IDS[0]  # first one is the longest RU text


# ===========================================================================
# SECTION 1 — Ping / liveness
# ===========================================================================
print("\n=== SECTION 1: Ping / liveness ===")

def check_ping():
    r = call("ping", {})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("status" in res, "ping: status present")
    ok &= need(res.get("status") == "ok", f"ping: status='ok' (got {res.get('status')!r})")
    ok &= need("uptime_sec" in res, "ping: uptime_sec present")
    ok &= need(isinstance(res.get("uptime_sec"), (int, float)), "ping: uptime_sec is numeric")
    ok &= need("history_count" in res, "ping: history_count present")
    return ok

run_check("ping", check_ping)


def check_handshake():
    r = call("handshake", {"agent_version": "smoke-0.1", "capabilities": []})
    res = r.get("result", {})
    ok = r.get("ok") is True
    # Real handshake response: backend_version + phase_b_capable/phase_c_capable +
    # swift_version_ack (NO protocol_version — Swift IPCClient reads backend_version).
    ok &= need("backend_version" in res, "handshake: backend_version present")
    ok &= need("phase_b_capable" in res, "handshake: phase_b_capable present")
    return ok

run_check("handshake", check_handshake)


# ===========================================================================
# SECTION 2 — Analytics
# ===========================================================================
print("\n=== SECTION 2: Analytics ===")

def check_get_analytics_dashboard():
    r = call("get_analytics_dashboard", {"days": 30})
    res = r.get("result", {})
    ok = r.get("ok") is True
    # privacy gate is acceptable: ok==True + reason
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_analytics_dashboard: privacy gate (ok==True, no history leaked)")
        return ok
    ok &= need(isinstance(res, dict) and len(res) > 0, "get_analytics_dashboard: non-empty result dict")
    return ok

run_check("get_analytics_dashboard", check_get_analytics_dashboard)


def check_get_sentiment_trends():
    r = call("get_sentiment_trends", {"days": 30})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_sentiment_trends: privacy gate active, shape ok")
        return ok
    # Normal path: SentimentTrendAnalyzer.to_dict() keys
    ok &= need("daily_sentiment" in res, "get_sentiment_trends: daily_sentiment present")
    ok &= need(isinstance(res.get("daily_sentiment"), list), "get_sentiment_trends: daily_sentiment is list")
    ok &= need("mood_trend" in res, "get_sentiment_trends: mood_trend present")
    ok &= need(res.get("mood_trend") in ("improving", "stable", "declining"),
               f"get_sentiment_trends: mood_trend valid (got {res.get('mood_trend')!r})")
    ok &= need("overall_sentiment" in res or "avg_sentiment" in res,
               "get_sentiment_trends: sentiment score present")
    return ok

run_check("get_sentiment_trends", check_get_sentiment_trends)


def check_get_keyword_cloud():
    r = call("get_keyword_cloud", {"limit": 20, "days": 30})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_keyword_cloud: privacy gate active, shape ok")
        return ok
    ok &= need("words" in res, "get_keyword_cloud: words key present")
    ok &= need(isinstance(res.get("words"), list), "get_keyword_cloud: words is list")
    # With seeded data there should be at least some words
    words = res.get("words", [])
    if words:
        first = words[0]
        ok &= need(isinstance(first, dict), "get_keyword_cloud: words[0] is dict")
        ok &= need("word" in first, "get_keyword_cloud: words[0].word present")
        ok &= need("count" in first or "weight" in first, "get_keyword_cloud: words[0] has count/weight")
    return ok

run_check("get_keyword_cloud", check_get_keyword_cloud)


def check_get_activity_calendar():
    r = call("get_activity_calendar", {"months": 3})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_activity_calendar: privacy gate active, shape ok")
        return ok
    # Real shape: weeks = [weekday][week] grid (Swift heatmap reads `weeks`),
    # plus total_active_days / longest_streak / current_streak. `days` is a dict.
    ok &= need("weeks" in res, "get_activity_calendar: weeks key present")
    ok &= need(isinstance(res.get("weeks"), list), "get_activity_calendar: weeks is list")
    ok &= need("total_active_days" in res, "get_activity_calendar: total_active_days present")
    weeks = res.get("weeks", [])
    if weeks:
        ok &= need(isinstance(weeks[0], list), "get_activity_calendar: weeks[0] is a list (weekday row)")
        if weeks[0]:
            cell = weeks[0][0]
            ok &= need("date" in cell and "recordings" in cell,
                       "get_activity_calendar: cell has date+recordings")
    return ok

run_check("get_activity_calendar", check_get_activity_calendar)


def check_generate_daily_digest():
    today = datetime.date.today().isoformat()
    r = call("generate_daily_digest", {"date": today})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "generate_daily_digest: privacy gate active")
        return ok
    # Returns {date, total_recordings, total_duration_min, total_words, languages_used, top_topics, highlights, markdown}
    ok &= need("total_recordings" in res, "generate_daily_digest: total_recordings present")
    ok &= need(isinstance(res.get("total_recordings"), (int, float)),
               "generate_daily_digest: total_recordings numeric")
    ok &= need("markdown" in res or "digest" in res,
               "generate_daily_digest: markdown/digest field present")
    markdown_val = res.get("markdown") or res.get("digest") or ""
    ok &= need(isinstance(markdown_val, str), "generate_daily_digest: markdown is string")
    return ok

run_check("generate_daily_digest", check_generate_daily_digest)


def check_get_topic_timeline():
    r = call("get_topic_timeline", {"limit": 10})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_topic_timeline: privacy gate active")
        return ok
    # Real shape: {segments, total_shifts, current_topic} (NOT "timeline").
    ok &= need("segments" in res, "get_topic_timeline: segments key present")
    ok &= need(isinstance(res.get("segments"), list), "get_topic_timeline: segments is list")
    ok &= need("total_shifts" in res, "get_topic_timeline: total_shifts present")
    ok &= need("current_topic" in res, "get_topic_timeline: current_topic present")
    return ok

run_check("get_topic_timeline", check_get_topic_timeline)


def check_get_recording_insights():
    r = call("get_recording_insights", {"days": 7})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_recording_insights: privacy gate active")
        return ok
    ok &= need("insights" in res, "get_recording_insights: insights key present")
    ok &= need(isinstance(res.get("insights"), list), "get_recording_insights: insights is list")
    ok &= need("count" in res or isinstance(res.get("insights"), list),
               "get_recording_insights: count or insights list present")
    return ok

run_check("get_recording_insights", check_get_recording_insights)


def check_compare_periods():
    # Use last 7 days as period A, 14 days ago as period B
    today = datetime.date.today()
    p1_start = (today - datetime.timedelta(days=7)).isoformat()
    p1_end = today.isoformat()
    p2_start = (today - datetime.timedelta(days=14)).isoformat()
    p2_end = (today - datetime.timedelta(days=7)).isoformat()
    r = call("compare_periods", {
        "period1_start": p1_start,
        "period1_end": p1_end,
        "period2_start": p2_start,
        "period2_end": p2_end,
    })
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "compare_periods: privacy gate active")
        return ok
    # Normal shape: period1, period2, recordings_change_pct, ...
    ok &= need("period1" in res, "compare_periods: period1 key present")
    ok &= need("period2" in res, "compare_periods: period2 key present")
    p1 = res.get("period1", {})
    ok &= need("recordings" in p1, "compare_periods: period1.recordings present")
    ok &= need(isinstance(p1.get("recordings"), (int, float)),
               "compare_periods: period1.recordings numeric")
    return ok

run_check("compare_periods", check_compare_periods)


def check_get_timeline_view():
    r = call("get_timeline_view", {"granularity": "day"})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_timeline_view: privacy gate active")
        return ok
    # Real shape: {blocks, total_blocks, group_by} (NOT "timeline").
    ok &= need("blocks" in res, "get_timeline_view: blocks key present")
    ok &= need(isinstance(res.get("blocks"), list), "get_timeline_view: blocks is list")
    ok &= need("group_by" in res, "get_timeline_view: group_by present")
    return ok

run_check("get_timeline_view", check_get_timeline_view)


def check_generate_stats_report():
    r = call("generate_stats_report", {"days": 30})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "generate_stats_report: privacy gate active")
        return ok
    ok &= need("markdown" in res, "generate_stats_report: markdown key present")
    ok &= need(isinstance(res.get("markdown"), str), "generate_stats_report: markdown is string")
    ok &= need(len(res.get("markdown", "")) > 0, "generate_stats_report: markdown non-empty")
    return ok

run_check("generate_stats_report", check_generate_stats_report)


# ===========================================================================
# SECTION 3 — Action items / LLM / summarize
# ===========================================================================
print("\n=== SECTION 3: Action items / LLM / summarize ===")

def check_extract_action_items():
    r = call("extract_action_items", {"id": ID_A})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "extract_action_items: privacy gate active")
        return ok
    # Returns {action_items, tasks, decisions, questions, priority_tags}
    # The real key from SearchAndAnalysisService is "action_items" (list) + tasks/decisions/questions
    has_keys = any(k in res for k in ("action_items", "tasks", "decisions", "questions"))
    ok &= need(has_keys, "extract_action_items: action_items/tasks/decisions/questions present")
    return ok

run_check("extract_action_items", check_extract_action_items)


def check_summarize_item():
    r = call("summarize_item", {"id": ID_LONG})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("summary" in res, "summarize_item: summary key present")
    ok &= need(isinstance(res.get("summary"), str), "summarize_item: summary is string")
    # Summary should be non-empty for a non-trivial input
    ok &= need(len(res.get("summary", "")) > 0, "summarize_item: summary non-empty")
    return ok

run_check("summarize_item", check_summarize_item)


def check_summarize_text():
    long_text = (
        "Сегодня на встрече обсудили три основные темы: "
        "бюджет на следующий год, кадровые изменения и стратегию выхода на новые рынки. "
        "По итогам дискуссии были приняты решения о заморозке найма и пересмотре KPI."
    )
    r = call("summarize_text", {"text": long_text, "max_sentences": 2})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("summary" in res, "summarize_text: summary key present")
    ok &= need(isinstance(res.get("summary"), str), "summarize_text: summary is string")
    ok &= need(len(res.get("summary", "")) > 0, "summarize_text: summary non-empty")
    return ok

run_check("summarize_text", check_summarize_text)


def check_get_meeting_report():
    r = call("get_meeting_report", {"id": ID_A})
    res = r.get("result", {})
    # ok may be False with fallback_reason if item is short / LLM absent — that's valid
    # We check shape, not truthiness of ok
    ok = True
    ok &= need("id" in res, "get_meeting_report: id key present")
    ok &= need("summary" in res, "get_meeting_report: summary key present")
    ok &= need("action_items" in res, "get_meeting_report: action_items key present")
    ok &= need("decisions" in res, "get_meeting_report: decisions key present")
    ok &= need("questions" in res, "get_meeting_report: questions key present")
    ok &= need("speakers" in res, "get_meeting_report: speakers key present")
    ok &= need("speaker_count" in res, "get_meeting_report: speaker_count key present")
    ok &= need("markdown" in res, "get_meeting_report: markdown key present")
    ok &= need(isinstance(res.get("action_items"), list), "get_meeting_report: action_items is list")
    ok &= need(isinstance(res.get("speakers"), list), "get_meeting_report: speakers is list")
    # If ok==True, summary should be a string
    if res.get("ok"):
        ok &= need(isinstance(res.get("summary"), str), "get_meeting_report: summary is string")
    return ok

run_check("get_meeting_report", check_get_meeting_report)


# ===========================================================================
# SECTION 4 — Search / duplicates
# ===========================================================================
print("\n=== SECTION 4: Search / duplicates ===")

def check_semantic_search():
    r = call("semantic_search", {"query": "квартальные результаты продажи", "top_k": 5, "fallback": True})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "semantic_search: privacy gate active")
        return ok
    ok &= need("results" in res, "semantic_search: results key present")
    ok &= need(isinstance(res.get("results"), list), "semantic_search: results is list")
    ok &= need("mode" in res, "semantic_search: mode key present")
    ok &= need(res.get("mode") in ("semantic", "keyword", "disabled"),
               f"semantic_search: mode valid (got {res.get('mode')!r})")
    results = res.get("results", [])
    if results:
        first = results[0]
        ok &= need("id" in first, "semantic_search: results[0].id present")
        ok &= need("score" in first, "semantic_search: results[0].score present")
    return ok

run_check("semantic_search", check_semantic_search)


def check_find_duplicates():
    # With varied texts, we expect 0 groups; shape should still be valid
    r = call("find_duplicates", {"similarity_threshold": 0.85, "limit": 50})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") in ("privacy_mode_active", "too many items for deduplication"):
        ok &= need(True, f"find_duplicates: gated ({res.get('reason')})")
        return ok
    ok &= need("groups" in res, "find_duplicates: groups key present")
    ok &= need(isinstance(res.get("groups"), list), "find_duplicates: groups is list")
    ok &= need("total_duplicates" in res, "find_duplicates: total_duplicates key present")
    ok &= need(isinstance(res.get("total_duplicates"), (int, float)),
               "find_duplicates: total_duplicates numeric")
    return ok

run_check("find_duplicates", check_find_duplicates)


def check_word_frequency_analysis():
    r = call("word_frequency_analysis", {"limit": 20})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "word_frequency_analysis: privacy gate active")
        return ok
    # Real shape: {top_words, total_words, unique_words, vocabulary_richness,
    # bigrams, by_language} (NOT "words"/"total_unique").
    ok &= need("top_words" in res, "word_frequency_analysis: top_words key present")
    ok &= need(isinstance(res.get("top_words"), list), "word_frequency_analysis: top_words is list")
    ok &= need("unique_words" in res, "word_frequency_analysis: unique_words present")
    top = res.get("top_words", [])
    if top:
        first = top[0]
        ok &= need(isinstance(first, (list, dict)), "word_frequency_analysis: top_words[0] is list/dict pair")
    return ok

run_check("word_frequency_analysis", check_word_frequency_analysis)


def check_search_history():
    r = call("search_history", {"query": "продажи", "limit": 5})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "search_history: privacy gate active")
        return ok
    ok &= need("items" in res, "search_history: items key present")
    ok &= need(isinstance(res.get("items"), list), "search_history: items is list")
    # With seeded RU text containing "продажи", should find at least 1 result
    items = res.get("items", [])
    ok &= need(len(items) >= 1, f"search_history: found {len(items)} results for 'продажи' (expected ≥1)")
    return ok

run_check("search_history", check_search_history)


# ===========================================================================
# SECTION 5 — Speaker / translation / glossary
# ===========================================================================
print("\n=== SECTION 5: Speaker / translation / glossary ===")

def check_get_speaker_statistics():
    r = call("get_speaker_statistics", {})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_speaker_statistics: privacy gate active, shape ok")
        # Privacy gate must still return speakers:[], total_speakers:0
        ok &= need("speakers" in res, "get_speaker_statistics: speakers key in gated response")
        ok &= need("total_speakers" in res, "get_speaker_statistics: total_speakers in gated response")
        return ok
    ok &= need("speakers" in res or "total_speakers" in res,
               "get_speaker_statistics: speakers or total_speakers present")
    return ok

run_check("get_speaker_statistics", check_get_speaker_statistics)


def check_translate_text():
    # Param is translation_mode (not "mode"); translation comes back under "text"
    # (Swift InlineTranslation reads res["text"]). In an offline dev env the HF
    # Marian model may be absent → status="model_unavailable_offline" with empty
    # text; that is an environment artifact, so we assert the KEYS, not non-empty.
    r = call("translate_text", {"text": "Добрый день", "translation_mode": "ru_to_es"})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("text" in res, "translate_text: text key present")
    ok &= need(isinstance(res.get("text"), str), "translate_text: text is string")
    ok &= need("status" in res, "translate_text: status present")
    ok &= need("source_lang" in res, "translate_text: source_lang present")
    ok &= need("target_lang" in res, "translate_text: target_lang present")
    ok &= need("translation_mode" in res, "translate_text: translation_mode present")
    return ok

run_check("translate_text", check_translate_text)


def check_get_glossary_suggestions():
    r = call("get_glossary_suggestions", {"limit": 10})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_glossary_suggestions: privacy gate active")
        return ok
    ok &= need("suggestions" in res, "get_glossary_suggestions: suggestions key present")
    ok &= need(isinstance(res.get("suggestions"), list), "get_glossary_suggestions: suggestions is list")
    suggestions = res.get("suggestions", [])
    if suggestions:
        first = suggestions[0]
        ok &= need("source" in first and "target" in first,
                   "get_glossary_suggestions: suggestions[0] has source+target")
    return ok

run_check("get_glossary_suggestions", check_get_glossary_suggestions)


def check_get_vocabulary_suggestions():
    r = call("get_vocabulary_suggestions", {"limit": 10})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "get_vocabulary_suggestions: privacy gate active")
        return ok
    ok &= need("suggestions" in res, "get_vocabulary_suggestions: suggestions key present")
    ok &= need(isinstance(res.get("suggestions"), list), "get_vocabulary_suggestions: suggestions is list")
    suggestions = res.get("suggestions", [])
    if suggestions:
        first = suggestions[0]
        ok &= need("word" in first, "get_vocabulary_suggestions: suggestions[0].word present")
    return ok

run_check("get_vocabulary_suggestions", check_get_vocabulary_suggestions)


# ===========================================================================
# SECTION 6 — Export
# ===========================================================================
print("\n=== SECTION 6: Export ===")

def check_export_history():
    r = call("export_history", {})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("content" in res, "export_history: content key present")
    ok &= need(isinstance(res.get("content"), str), "export_history: content is string")
    ok &= need(len(res.get("content", "")) > 0, "export_history: content non-empty")
    return ok

run_check("export_history", check_export_history)


def check_export_history_srt():
    # SRT export requires an item_id
    r = call("export_history_srt", {"id": ID_A})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("content" in res, "export_history_srt: content key present")
    ok &= need(isinstance(res.get("content"), str), "export_history_srt: content is string")
    # SRT may be minimal (no speaker_turns on seeded item) but should be a string
    return ok

run_check("export_history_srt", check_export_history_srt)


# ===========================================================================
# SECTION 7 — Diagnostics / metrics
# ===========================================================================
print("\n=== SECTION 7: Diagnostics / metrics ===")

def check_get_diagnostics():
    r = call("get_diagnostics", {})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("system" in res, "get_diagnostics: system key present")
    ok &= need("stt" in res, "get_diagnostics: stt key present")
    ok &= need("history" in res, "get_diagnostics: history key present")
    ok &= need("settings_cache" in res, "get_diagnostics: settings_cache key present")
    ok &= need(isinstance(res.get("system"), dict), "get_diagnostics: system is dict")
    return ok

run_check("get_diagnostics", check_get_diagnostics)


def check_get_metrics_dashboard():
    r = call("get_metrics_dashboard", {})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("session" in res, "get_metrics_dashboard: session key present")
    ok &= need("llm" in res, "get_metrics_dashboard: llm key present")
    ok &= need("config_snapshot" in res, "get_metrics_dashboard: config_snapshot key present")
    session = res.get("session", {})
    ok &= need("recording_active" in session, "get_metrics_dashboard: session.recording_active present")
    ok &= need("metrics" in res, "get_metrics_dashboard: metrics key present")
    return ok

run_check("get_metrics_dashboard", check_get_metrics_dashboard)


# ===========================================================================
# SECTION 8 — Misc feature methods
# ===========================================================================
print("\n=== SECTION 8: Misc features ===")

def check_analyze_speech_pace():
    text = (
        "Добрый день, коллеги. Сегодня мы обсудим результаты квартала. "
        "Продажи выросли, издержки снизились, команда справилась с планом."
    )
    r = call("analyze_speech_pace", {"text": text, "duration_sec": 12.0})
    res = r.get("result", {})
    ok = r.get("ok") is True
    ok &= need("words_per_minute" in res, "analyze_speech_pace: words_per_minute present")
    ok &= need(isinstance(res.get("words_per_minute"), (int, float)),
               "analyze_speech_pace: words_per_minute is numeric")
    ok &= need("pace_category" in res, "analyze_speech_pace: pace_category present")
    ok &= need(res.get("pace_category") in ("slow", "normal", "fast", "very_fast"),
               f"analyze_speech_pace: pace_category valid (got {res.get('pace_category')!r})")
    ok &= need("word_count" in res, "analyze_speech_pace: word_count present")
    return ok

run_check("analyze_speech_pace", check_analyze_speech_pace)


def check_get_recording_stats():
    r = call("get_recording_stats", {})
    res = r.get("result", {})
    ok = r.get("ok") is True
    # Real shape: total_count (NOT total_recordings) + total_duration_sec + ...
    ok &= need("total_count" in res, "get_recording_stats: total_count present")
    ok &= need(isinstance(res.get("total_count"), (int, float)),
               "get_recording_stats: total_count numeric")
    ok &= need("total_duration_sec" in res, "get_recording_stats: total_duration_sec present")
    return ok

run_check("get_recording_stats", check_get_recording_stats)


def check_compare_recordings():
    # Need at least 2 ids for comparison
    if len(SEED_IDS) < 2:
        need(False, "compare_recordings: not enough seeded ids")
        return False
    ids = SEED_IDS[:3] if len(SEED_IDS) >= 3 else SEED_IDS[:2]
    r = call("compare_recordings", {"item_ids": ids})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "compare_recordings: privacy gate active")
        # Shape must match the privacy-gate dict keys
        ok &= need("items" in res, "compare_recordings: items key in gated response")
        ok &= need("text_similarity_matrix" in res,
                   "compare_recordings: text_similarity_matrix in gated response")
        return ok
    ok &= need("items" in res or "text_similarity_matrix" in res,
               "compare_recordings: items or text_similarity_matrix present")
    return ok

run_check("compare_recordings", check_compare_recordings)


def check_auto_summarize_batch():
    if len(SEED_IDS) < 2:
        need(False, "auto_summarize_batch: not enough seeded ids")
        return False
    ids = SEED_IDS[:3] if len(SEED_IDS) >= 3 else SEED_IDS[:2]
    r = call("auto_summarize_batch", {"ids": ids})
    res = r.get("result", {})
    ok = r.get("ok") is True
    if res.get("reason") == "privacy_mode_active":
        ok &= need(True, "auto_summarize_batch: privacy gate active")
        return ok
    # Returns: summary, key_points, items_processed, total_words, llm, fallback
    ok &= need("summary" in res, "auto_summarize_batch: summary key present")
    ok &= need(isinstance(res.get("summary"), str), "auto_summarize_batch: summary is string")
    ok &= need("key_points" in res, "auto_summarize_batch: key_points key present")
    ok &= need(isinstance(res.get("key_points"), list), "auto_summarize_batch: key_points is list")
    ok &= need("items_processed" in res, "auto_summarize_batch: items_processed present")
    ok &= need(isinstance(res.get("items_processed"), (int, float)),
               "auto_summarize_batch: items_processed numeric")
    ok &= need("llm" in res, "auto_summarize_batch: llm flag present")
    ok &= need("fallback" in res, "auto_summarize_batch: fallback flag present")
    return ok

run_check("auto_summarize_batch", check_auto_summarize_batch)


# ===========================================================================
# FINAL SUMMARY
# ===========================================================================
print("\n=== SMOKE SUMMARY ===")
all_ok = True
for name, ok in RESULTS:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    all_ok &= ok

total = len(RESULTS)
passed = sum(1 for _, ok in RESULTS if ok)
failed = total - passed
print(f"\n  {passed}/{total} methods PASSED, {failed} FAILED")
print("=== " + ("ALL SMOKE GREEN" if all_ok else "SMOKE FAILURES DETECTED") + " ===")
sys.exit(0 if all_ok else 1)
