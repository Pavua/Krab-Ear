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

=== METHODS COVERED (38 original + 23 from 2026-07-02/03 recording-mgmt/integrations waves) ===
  SECTION 9f-9k — RecordingScheduler / ConfigPresetsLibrary / TimelineExporter /
  WebhookManager / RecordingChainManager / SummaryProfileManager (see below,
  after 9e/bookmarks_crud). apply_config_preset intentionally NOT exercised here
  (mutates live settings.json — out of scope for a read/write-sentinel smoke).
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
    get_diagnostics             — system/stt/llm/history/settings_cache/wake_word_watchdog
    get_metrics_dashboard       — real-time metrics snapshot
    wake_word_status            — openWakeWord adapter status + heartbeat fields
  MISC
    analyze_speech_pace         — wpm/cpm/pace_category from text+duration
    get_recording_stats         — cumulative recording stats
    compare_recordings          — side-by-side recording comparison
    auto_summarize_batch        — batch LLM summary for ids
  SECTION 9 — Stateful CRUD round-trips (add→list→assert→remove→assert)
    list_stt_hotwords           — baseline + post-add + post-remove list
    add_stt_hotword             — add sentinel word "ЗебраТестE2E"
    remove_stt_hotword          — remove sentinel, verify gone
    set_translation_glossary_item  — add source/target pair
    remove_translation_glossary_item — remove, verify gone (via get_settings)
    get_settings                — read translation_glossary + quick_edit_timeout_sec
    set_settings                — flip quick_edit_timeout_sec sentinel + restore
    create_collection           — create named collection
    list_collections            — verify presence and absence around create/delete
    add_to_collection           — add seeded item_id to collection
    delete_collection           — delete, verify gone
    add_bookmark                — add bookmark on seeded session_id
    list_bookmarks              — verify presence and absence around add/delete
    delete_bookmark             — delete bookmark by id

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
    # wake-word-watchdog spec (2026-07-15): heartbeat watchdog snapshot
    ok &= need("wake_word_watchdog" in res, "get_diagnostics: wake_word_watchdog key present")
    watchdog = res.get("wake_word_watchdog", {})
    ok &= need(isinstance(watchdog, dict), "get_diagnostics: wake_word_watchdog is dict")
    ok &= need("enabled" in watchdog, "get_diagnostics: wake_word_watchdog.enabled present")
    ok &= need(isinstance(watchdog.get("enabled"), bool),
               "get_diagnostics: wake_word_watchdog.enabled is bool")
    ok &= need("wedged" in watchdog, "get_diagnostics: wake_word_watchdog.wedged present")
    return ok

run_check("get_diagnostics", check_get_diagnostics)


def check_wake_word_status():
    r = call("wake_word_status", {})
    res = r.get("result", {})
    ok = r.get("ok") is True
    # last_chunk_ts/listen_started_ts may legitimately be None — the wake-word
    # engine is never started on a throwaway smoke instance. Assert key
    # presence, not truthiness.
    ok &= need("wedged" in res, "wake_word_status: wedged key present")
    ok &= need(isinstance(res.get("wedged"), bool), "wake_word_status: wedged is bool")
    ok &= need("last_chunk_ts" in res, "wake_word_status: last_chunk_ts key present")
    ok &= need("listen_started_ts" in res, "wake_word_status: listen_started_ts key present")
    return ok

run_check("wake_word_status", check_wake_word_status)


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
# SECTION 9 — Stateful CRUD round-trips
# ===========================================================================
# Each round-trip: add/set → list/get → assert present → remove → list/get →
# assert gone.  All test values carry the suffix "E2E_S9" so they are easy to
# spot and the smoke is idempotent (re-running on a dirty store just re-adds
# then re-removes the same sentinel values).
# ===========================================================================
print("\n=== SECTION 9: Stateful CRUD round-trips ===")

# ---------------------------------------------------------------------------
# 9a — STT hotwords
#   add_stt_hotword    params: {word: str}  → {hotwords: list[str], truncated: bool}
#   list_stt_hotwords  params: {}           → {hotwords: list[str], enabled: bool, truncated: bool}
#   remove_stt_hotword params: {word: str}  → {hotwords: list[str]}
# ---------------------------------------------------------------------------
_HOTWORD_TEST = "ЗебраТестE2E"

def check_stt_hotword_crud():
    ok = True

    # Step 1: initial list (capture baseline)
    r0 = call("list_stt_hotwords", {})
    ok &= need(r0.get("ok") is True, "9a/stt_hotwords: list_stt_hotwords initial ok==True")
    res0 = r0.get("result", {})
    ok &= need("hotwords" in res0, "9a/stt_hotwords: initial response has hotwords key")
    baseline = res0.get("hotwords", [])
    ok &= need(isinstance(baseline, list), "9a/stt_hotwords: hotwords is list")

    # Step 2: add
    r_add = call("add_stt_hotword", {"word": _HOTWORD_TEST})
    ok &= need(r_add.get("ok") is True, "9a/stt_hotwords: add_stt_hotword ok==True")
    res_add = r_add.get("result", {})
    ok &= need("hotwords" in res_add, "9a/stt_hotwords: add response has hotwords key")
    ok &= need("truncated" in res_add, "9a/stt_hotwords: add response has truncated key")
    ok &= need(_HOTWORD_TEST in res_add.get("hotwords", []),
               f"9a/stt_hotwords: added word present in add response")

    # Step 3: list again, assert present
    r1 = call("list_stt_hotwords", {})
    res1 = r1.get("result", {})
    hotwords_after_add = res1.get("hotwords", [])
    ok &= need(_HOTWORD_TEST in hotwords_after_add,
               f"9a/stt_hotwords: {_HOTWORD_TEST!r} visible in list after add")

    # Step 4: remove
    r_rm = call("remove_stt_hotword", {"word": _HOTWORD_TEST})
    ok &= need(r_rm.get("ok") is True, "9a/stt_hotwords: remove_stt_hotword ok==True")
    res_rm = r_rm.get("result", {})
    ok &= need("hotwords" in res_rm, "9a/stt_hotwords: remove response has hotwords key")
    ok &= need(_HOTWORD_TEST not in res_rm.get("hotwords", []),
               "9a/stt_hotwords: word absent from remove response")

    # Step 5: list again, assert gone
    r2 = call("list_stt_hotwords", {})
    res2 = r2.get("result", {})
    ok &= need(_HOTWORD_TEST not in res2.get("hotwords", []),
               f"9a/stt_hotwords: {_HOTWORD_TEST!r} absent in list after remove")

    return ok

run_check("9a/stt_hotwords_crud", check_stt_hotword_crud)


# ---------------------------------------------------------------------------
# 9b — Translation glossary
#   There is NO dedicated list-glossary IPC method.  The glossary is stored as
#   settings["translation_glossary"] (a {source: target} dict); read via
#   get_settings.
#
#   set_translation_glossary_item    params: {source: str, target: str}
#     → {updated: bool, count: int}  |  {updated: False, error: str}
#   remove_translation_glossary_item params: {source: str}
#     → {removed: bool, count: int}
#   get_settings                     params: {}
#     → full settings dict (translation_glossary is a nested dict key)
# ---------------------------------------------------------------------------
_GLOSS_SRC = "ЗебраE2E"
_GLOSS_TGT = "CebraE2E"

def check_translation_glossary_crud():
    ok = True

    # Step 1: read baseline glossary from settings
    r0 = call("get_settings", {})
    ok &= need(r0.get("ok") is True, "9b/glossary: get_settings baseline ok==True")
    settings0 = r0.get("result", {})
    baseline_glossary = settings0.get("translation_glossary", {}) or {}
    ok &= need(isinstance(baseline_glossary, dict), "9b/glossary: translation_glossary is dict")

    # Step 2: add entry
    r_set = call("set_translation_glossary_item", {"source": _GLOSS_SRC, "target": _GLOSS_TGT})
    ok &= need(r_set.get("ok") is True, "9b/glossary: set_translation_glossary_item ok==True")
    res_set = r_set.get("result", {})
    ok &= need(res_set.get("updated") is True,
               f"9b/glossary: set returned updated=True (got {res_set!r})")
    ok &= need("count" in res_set, "9b/glossary: set response has count key")

    # Step 3: read settings, assert entry present
    r1 = call("get_settings", {})
    glossary_after = (r1.get("result", {}) or {}).get("translation_glossary", {}) or {}
    ok &= need(_GLOSS_SRC in glossary_after,
               f"9b/glossary: source {_GLOSS_SRC!r} present in glossary after set")
    ok &= need(glossary_after.get(_GLOSS_SRC) == _GLOSS_TGT,
               f"9b/glossary: target matches {_GLOSS_TGT!r} (got {glossary_after.get(_GLOSS_SRC)!r})")

    # Step 4: remove entry
    r_rm = call("remove_translation_glossary_item", {"source": _GLOSS_SRC})
    ok &= need(r_rm.get("ok") is True, "9b/glossary: remove_translation_glossary_item ok==True")
    res_rm = r_rm.get("result", {})
    ok &= need(res_rm.get("removed") is True,
               f"9b/glossary: remove returned removed=True (got {res_rm!r})")
    ok &= need("count" in res_rm, "9b/glossary: remove response has count key")

    # Step 5: read settings, assert entry gone
    r2 = call("get_settings", {})
    glossary_after_rm = (r2.get("result", {}) or {}).get("translation_glossary", {}) or {}
    ok &= need(_GLOSS_SRC not in glossary_after_rm,
               f"9b/glossary: source {_GLOSS_SRC!r} absent in glossary after remove")

    return ok

run_check("9b/translation_glossary_crud", check_translation_glossary_crud)


# ---------------------------------------------------------------------------
# 9c — Collections
#   create_collection  params: {name: str, description?: str}
#     → {name, description, created_at, item_count}
#   list_collections   params: {}
#     → {collections: [{name, description, created_at, item_count}, ...]}
#     (privacy-gated: {collections: [], reason: "privacy_mode_active"})
#   add_to_collection  params: {collection_name: str, item_id: str}
#     → {name, description, created_at, item_count} (the updated collection dict)
#   delete_collection  params: {name: str}
#     → {deleted: bool, name: str}
#
#   NOTE: item_id validation rejects dots/slashes — SEED_IDS are UUID-like
#   (hex+dashes), which pass the guard.  The smoke uses SEED_IDS[0] if available.
# ---------------------------------------------------------------------------
_COL_NAME = "КоллекцияE2E_S9"

def check_collections_crud():
    ok = True

    # Step 1: list baseline (may be privacy-gated)
    r0 = call("list_collections", {})
    ok &= need(r0.get("ok") is True, "9c/collections: list_collections baseline ok==True")
    res0 = r0.get("result", {})
    ok &= need("collections" in res0, "9c/collections: baseline response has collections key")
    privacy_gated = res0.get("reason") == "privacy_mode_active"
    if privacy_gated:
        need(True, "9c/collections: privacy gate active — list baseline empty, continuing")

    # Step 2: create
    r_create = call("create_collection", {"name": _COL_NAME, "description": "e2e smoke sentinel"})
    ok &= need(r_create.get("ok") is True, "9c/collections: create_collection ok==True")
    res_create = r_create.get("result", {})
    # create_collection returns the collection dict directly (not wrapped)
    ok &= need(res_create.get("name") == _COL_NAME,
               f"9c/collections: created name matches (got {res_create.get('name')!r})")
    ok &= need("item_count" in res_create, "9c/collections: create response has item_count key")
    ok &= need(res_create.get("item_count") == 0,
               "9c/collections: new collection item_count=0")

    # Step 3: list again — assert present (unless privacy-gated)
    r1 = call("list_collections", {})
    res1 = r1.get("result", {})
    if not privacy_gated:
        names_after = [c.get("name") for c in res1.get("collections", [])]
        ok &= need(_COL_NAME in names_after,
                   f"9c/collections: {_COL_NAME!r} visible in list after create")

    # Step 4: optionally add a seeded item (only if we have ids and they pass validation)
    if SEED_IDS:
        seed_id = SEED_IDS[0]
        # Validate: no dots, slashes, backslashes, or null bytes in the id
        unsafe = any(c in seed_id for c in "./\\\x00")
        if not unsafe:
            r_add = call("add_to_collection", {"collection_name": _COL_NAME, "item_id": seed_id})
            ok &= need(r_add.get("ok") is True, "9c/collections: add_to_collection ok==True")
            res_add = r_add.get("result", {})
            # Returns the updated collection dict with item_count incremented
            ok &= need(res_add.get("item_count", 0) >= 1,
                       "9c/collections: item_count ≥1 after add_to_collection")
        else:
            need(True, f"9c/collections: SKIP add_to_collection — seed_id has unsafe chars: {seed_id!r}")

    # Step 5: delete
    r_del = call("delete_collection", {"name": _COL_NAME})
    ok &= need(r_del.get("ok") is True, "9c/collections: delete_collection ok==True")
    res_del = r_del.get("result", {})
    ok &= need(res_del.get("deleted") is True,
               f"9c/collections: deleted=True (got {res_del!r})")
    ok &= need(res_del.get("name") == _COL_NAME,
               "9c/collections: delete response echoes collection name")

    # Step 6: list again — assert gone (skip check when privacy-gated)
    r2 = call("list_collections", {})
    res2 = r2.get("result", {})
    if not privacy_gated:
        names_after_del = [c.get("name") for c in res2.get("collections", [])]
        ok &= need(_COL_NAME not in names_after_del,
                   f"9c/collections: {_COL_NAME!r} absent after delete")

    return ok

run_check("9c/collections_crud", check_collections_crud)


# ---------------------------------------------------------------------------
# 9d — Settings round-trip (safe, reversible scalar)
#   We use `quick_edit_timeout_sec` (float, default 5.0, no credential/key, not
#   destructive).  We read the current value, set it to a recognisably different
#   sentinel (17.0), confirm the change, then restore the original value.
#
#   get_settings  params: {}      → full settings dict (flat)
#   set_settings  params: {key: value}
#     → full settings dict after merge (same shape as get_settings)
#
#   NOTE: get_settings REDACTS sensitive fields to "REDACTED" — we avoid those.
# ---------------------------------------------------------------------------
_SETTINGS_KEY = "quick_edit_timeout_sec"
_SETTINGS_SENTINEL = 17.0  # recognisably different from any plausible real value

def check_settings_round_trip():
    ok = True

    # Step 1: read current value
    r0 = call("get_settings", {})
    ok &= need(r0.get("ok") is True, "9d/settings: get_settings baseline ok==True")
    settings0 = r0.get("result", {})
    original_val = settings0.get(_SETTINGS_KEY, 5.0)  # 5.0 = DEFAULT_SETTINGS default
    ok &= need(isinstance(original_val, (int, float)),
               f"9d/settings: {_SETTINGS_KEY} is numeric (got {original_val!r})")

    # Step 2: set to sentinel
    r_set = call("set_settings", {_SETTINGS_KEY: _SETTINGS_SENTINEL})
    ok &= need(r_set.get("ok") is True, "9d/settings: set_settings sentinel ok==True")
    res_set = r_set.get("result", {})
    ok &= need(isinstance(res_set, dict), "9d/settings: set_settings returns dict result")
    # set_settings returns the merged settings dict
    set_val = res_set.get(_SETTINGS_KEY)
    ok &= need(set_val == _SETTINGS_SENTINEL or (
                   isinstance(set_val, (int, float)) and abs(set_val - _SETTINGS_SENTINEL) < 0.01
               ),
               f"9d/settings: set response reflects sentinel {_SETTINGS_SENTINEL} (got {set_val!r})")

    # Step 3: get_settings again, confirm sentinel persisted
    r1 = call("get_settings", {})
    val_after = (r1.get("result", {}) or {}).get(_SETTINGS_KEY)
    ok &= need(isinstance(val_after, (int, float)) and abs(val_after - _SETTINGS_SENTINEL) < 0.01,
               f"9d/settings: get_settings reflects sentinel after set (got {val_after!r})")

    # Step 4: restore original value
    r_restore = call("set_settings", {_SETTINGS_KEY: original_val})
    ok &= need(r_restore.get("ok") is True, "9d/settings: set_settings restore ok==True")

    # Step 5: confirm restored
    r2 = call("get_settings", {})
    val_restored = (r2.get("result", {}) or {}).get(_SETTINGS_KEY)
    ok &= need(isinstance(val_restored, (int, float)) and abs(val_restored - original_val) < 0.01,
               f"9d/settings: value restored to {original_val} (got {val_restored!r})")

    return ok

run_check("9d/settings_round_trip", check_settings_round_trip)


# ---------------------------------------------------------------------------
# 9e — Bookmarks
#   add_bookmark   params: {session_id: str, offset_sec: float, note?: str}
#     → {"bookmark": {id, session_id, offset_sec, note, created_at}}
#     | {"ok": False, "reason": "limit_exceeded"} when cap hit
#   list_bookmarks params: {item_id: str}
#     → {"bookmarks": [...], "count": N}
#     | {"bookmarks": [], "count": 0, "reason": "privacy_mode_active"}
#   delete_bookmark params: {id: str}
#     → {"ok": bool}
#
#   NOTE: list_bookmarks uses `item_id` which equals the session_id stored by
#   add_bookmark (the store links them by session_id).  We use a SEED_ID so the
#   bookmark is attached to a real history item.
# ---------------------------------------------------------------------------

def check_bookmarks_crud():
    if not SEED_IDS:
        need(False, "9e/bookmarks: SKIP — no seeded ids available")
        return False

    ok = True
    test_session_id = SEED_IDS[0]  # reuse first seeded item as session anchor

    # Step 1: list bookmarks for this item (baseline — may be privacy-gated)
    r0 = call("list_bookmarks", {"item_id": test_session_id})
    ok &= need(r0.get("ok") is True, "9e/bookmarks: list_bookmarks baseline ok==True")
    res0 = r0.get("result", {})
    ok &= need("bookmarks" in res0, "9e/bookmarks: baseline response has bookmarks key")
    ok &= need("count" in res0, "9e/bookmarks: baseline response has count key")
    privacy_gated = res0.get("reason") == "privacy_mode_active"
    if privacy_gated:
        need(True, "9e/bookmarks: privacy gate active — list empty, continuing")

    # Step 2: add bookmark
    r_add = call("add_bookmark", {
        "session_id": test_session_id,
        "offset_sec": 12.5,
        "note": "E2E smoke sentinel bookmark",
    })
    ok &= need(r_add.get("ok") is True, "9e/bookmarks: add_bookmark ok==True")
    res_add = r_add.get("result", {})

    # Handle DoS cap (limit_exceeded) gracefully
    if res_add.get("ok") is False and res_add.get("reason") == "limit_exceeded":
        need(True, "9e/bookmarks: cap limit reached — skipping add/remove assertion")
        return ok

    bm = res_add.get("bookmark", {})
    ok &= need(isinstance(bm, dict), "9e/bookmarks: add response has bookmark dict")
    bm_id = bm.get("id")
    ok &= need(bool(bm_id), "9e/bookmarks: bookmark id present")
    ok &= need(bm.get("session_id") == test_session_id,
               "9e/bookmarks: bookmark session_id matches")
    ok &= need(bm.get("offset_sec") == 12.5,
               f"9e/bookmarks: offset_sec=12.5 (got {bm.get('offset_sec')!r})")

    # Step 3: list again — assert present (skip when privacy-gated)
    if not privacy_gated:
        r1 = call("list_bookmarks", {"item_id": test_session_id})
        res1 = r1.get("result", {})
        bm_ids_after = [b.get("id") for b in res1.get("bookmarks", [])]
        ok &= need(bm_id in bm_ids_after,
                   f"9e/bookmarks: bookmark {bm_id!r} visible in list after add")

    # Step 4: delete bookmark
    if bm_id:
        r_del = call("delete_bookmark", {"id": bm_id})
        ok &= need(r_del.get("ok") is True, "9e/bookmarks: delete_bookmark ok==True")
        res_del = r_del.get("result", {})
        ok &= need(res_del.get("ok") is True,
                   f"9e/bookmarks: delete returned ok=True (got {res_del!r})")

        # Step 5: list again — assert gone (skip when privacy-gated)
        if not privacy_gated:
            r2 = call("list_bookmarks", {"item_id": test_session_id})
            res2 = r2.get("result", {})
            bm_ids_after_del = [b.get("id") for b in res2.get("bookmarks", [])]
            ok &= need(bm_id not in bm_ids_after_del,
                       f"9e/bookmarks: bookmark {bm_id!r} absent after delete")

    return ok

run_check("9e/bookmarks_crud", check_bookmarks_crud)


# ---------------------------------------------------------------------------
# 9f — RecordingScheduler CRUD (schedule → list → cancel → list)
# ---------------------------------------------------------------------------

def check_recording_scheduler_crud():
    ok = True
    start_time = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    ).isoformat(timespec="seconds")

    r_sched = call("schedule_recording", {
        "start_time": start_time, "duration_sec": 600, "label": "E2E smoke sentinel",
    })
    ok &= need(r_sched.get("ok") is True, "9f/scheduler: schedule_recording ok==True")
    schedule = r_sched.get("result", {}).get("schedule", {})
    schedule_id = schedule.get("id")
    ok &= need(bool(schedule_id), "9f/scheduler: schedule has id")
    ok &= need(schedule.get("status") == "pending",
               f"9f/scheduler: status=pending (got {schedule.get('status')!r})")

    r_list = call("list_scheduled_recordings", {})
    res_list = r_list.get("result", {})
    ok &= need("schedules" in res_list and "count" in res_list,
               "9f/scheduler: list response has schedules+count keys")
    ids_after = [s.get("id") for s in res_list.get("schedules", [])]
    ok &= need(schedule_id in ids_after, "9f/scheduler: sentinel visible after schedule")

    if schedule_id:
        r_cancel = call("cancel_scheduled_recording", {"schedule_id": schedule_id})
        ok &= need(r_cancel.get("result", {}).get("cancelled") is True,
                   "9f/scheduler: cancel_scheduled_recording cancelled=True")

        r_list2 = call("list_scheduled_recordings", {})
        pending_after = [
            s.get("id") for s in r_list2.get("result", {}).get("schedules", [])
            if s.get("status") == "pending"
        ]
        ok &= need(schedule_id not in pending_after,
                   "9f/scheduler: sentinel no longer pending after cancel")

    return ok

run_check("9f/recording_scheduler_crud", check_recording_scheduler_crud)


# ---------------------------------------------------------------------------
# 9g — ConfigPresetsLibrary CRUD (list → create → list → export → import → delete → list)
# ---------------------------------------------------------------------------

_PRESET_NAME = "e2e_smoke_preset"

def check_config_presets_crud():
    ok = True

    r_create = call("create_config_preset", {
        "name": _PRESET_NAME, "description": "E2E smoke sentinel",
        "settings_patch": {"quality_profile": "balanced"},
    })
    ok &= need(r_create.get("ok") is True, "9g/presets: create_config_preset ok==True")
    preset = r_create.get("result", {}).get("preset", {})
    ok &= need(preset.get("name") == _PRESET_NAME, "9g/presets: created name matches")
    ok &= need(preset.get("builtin") is False, "9g/presets: builtin==False for custom preset")

    r_list = call("list_config_presets", {})
    names_after = [p.get("name") for p in r_list.get("result", {}).get("presets", [])]
    ok &= need(_PRESET_NAME in names_after, "9g/presets: sentinel visible after create")
    builtin_names = {"interview", "meeting", "translation", "call_recording"}
    ok &= need(bool(builtin_names & set(names_after)),
               f"9g/presets: at least one builtin preset present (got {names_after!r})")

    r_export = call("export_config_preset", {"name": _PRESET_NAME})
    exported_json = r_export.get("result", {}).get("json", "")
    ok &= need(bool(exported_json) and _PRESET_NAME in exported_json,
               "9g/presets: export_config_preset returns json containing preset name")

    r_delete = call("delete_config_preset", {"name": _PRESET_NAME})
    ok &= need(r_delete.get("result", {}).get("deleted") is True,
               "9g/presets: delete_config_preset deleted=True")

    r_import = call("import_config_preset", {"json": exported_json})
    imported = r_import.get("result", {}).get("preset", {})
    ok &= need(imported.get("name") == _PRESET_NAME,
               "9g/presets: import_config_preset restores sentinel by name")

    r_delete2 = call("delete_config_preset", {"name": _PRESET_NAME})
    ok &= need(r_delete2.get("result", {}).get("deleted") is True,
               "9g/presets: cleanup delete after re-import")

    r_list2 = call("list_config_presets", {})
    names_final = [p.get("name") for p in r_list2.get("result", {}).get("presets", [])]
    ok &= need(_PRESET_NAME not in names_final, "9g/presets: sentinel absent after final delete")

    return ok

run_check("9g/config_presets_crud", check_config_presets_crud)


# ---------------------------------------------------------------------------
# 9h — TimelineExporter (SVG/JSON/iCal export sanity, no cleanup needed —
#   files land under the throwaway data-dir, destroyed with the container)
# ---------------------------------------------------------------------------

def check_timeline_exporter():
    ok = True
    for method in ("export_timeline_svg", "export_timeline_json", "export_timeline_ical"):
        r = call(method, {"group_by": "day", "limit": 500})
        res = r.get("result", {})
        if "error" in res:
            # privacy_mode or invalid_path — acceptable non-crash outcome
            need(True, f"9h/timeline: {method} returned graceful error={res['error'].get('code')!r}")
            continue
        ok &= need("path" in res and "blocks" in res,
                   f"9h/timeline: {method} response has path+blocks keys")
        ok &= need(bool(res.get("path")), f"9h/timeline: {method} path is non-empty")
    return ok

run_check("9h/timeline_exporter", check_timeline_exporter)


# ---------------------------------------------------------------------------
# 9i — WebhookManager CRUD (list → register → list → unregister → list)
# ---------------------------------------------------------------------------

def check_webhook_manager_crud():
    ok = True

    # NOTE: must be a REAL, DNS-resolvable domain — the SSRF guard fail-closes on
    # unresolvable hosts even at registration time (Gap 3 fix, W1721), so reserved
    # non-resolving TLDs like .invalid/.test are correctly REJECTED here, not a bug.
    # example.com (RFC 2606) always resolves; delivery (if it ever fires async
    # before teardown) is a harmless benign POST to a real, inert domain.
    r_register = call("register_webhook", {
        "url": "https://example.com/e2e-smoke-hook",
        "events": ["transcription.completed"],
    })
    if "error" in r_register:
        need(False, f"9i/webhook: register_webhook top-level error: {r_register['error']!r}")
        return False
    res_register = r_register.get("result", {})
    if res_register.get("ok") is False and res_register.get("reason") == "webhook_limit_reached":
        need(True, "9i/webhook: limit reached — skipping CRUD assertion")
        return ok
    webhook_id = res_register.get("webhook_id")
    ok &= need(bool(webhook_id), "9i/webhook: register_webhook returns webhook_id")

    r_list = call("list_webhooks", {})
    ids_after = [w.get("webhook_id") for w in r_list.get("result", {}).get("webhooks", [])]
    ok &= need(webhook_id in ids_after, "9i/webhook: sentinel visible after register")

    if webhook_id:
        r_unregister = call("unregister_webhook", {"webhook_id": webhook_id})
        ok &= need(r_unregister.get("result", {}).get("removed") is True,
                   "9i/webhook: unregister_webhook removed=True")

        r_list2 = call("list_webhooks", {})
        ids_final = [w.get("webhook_id") for w in r_list2.get("result", {}).get("webhooks", [])]
        ok &= need(webhook_id not in ids_final, "9i/webhook: sentinel absent after unregister")

    return ok

run_check("9i/webhook_manager_crud", check_webhook_manager_crud)


# ---------------------------------------------------------------------------
# 9j — RecordingChainManager CRUD (start → add → get/list → merge → unlink → end)
# ---------------------------------------------------------------------------

def check_recording_chain_crud():
    if not SEED_IDS:
        need(False, "9j/chain: SKIP — no seeded ids available")
        return False

    ok = True
    r_start = call("start_chain", {"name": "E2E smoke chain"})
    res_start = r_start.get("result", {})
    if res_start.get("ok") is False:
        need(True, f"9j/chain: start_chain non-fatal error: {res_start.get('error')!r}")
        return ok
    chain_id = res_start.get("chain_id")
    ok &= need(bool(chain_id), "9j/chain: start_chain returns chain_id")

    if not chain_id:
        return ok

    r_add = call("add_to_chain", {"chain_id": chain_id, "item_id": SEED_IDS[0]})
    ok &= need(r_add.get("result", {}).get("ok") is True, "9j/chain: add_to_chain ok==True")

    r_get = call("get_chain", {"chain_id": chain_id})
    res_get = r_get.get("result", {})
    ok &= need(res_get.get("chain_id") == chain_id, "9j/chain: get_chain returns matching chain_id")
    ok &= need("total_duration_sec" in res_get and "total_word_count" in res_get,
               "9j/chain: get_chain has total_duration_sec+total_word_count")

    r_list = call("list_chains", {"limit": 50})
    chain_ids_after = [c.get("chain_id") for c in r_list.get("result", {}).get("chains", [])]
    ok &= need(chain_id in chain_ids_after, "9j/chain: sentinel visible in list_chains")

    r_merge = call("merge_chain_text", {"chain_id": chain_id})
    ok &= need("text" in r_merge.get("result", {}), "9j/chain: merge_chain_text has text key")

    r_unlink = call("unlink_recording_from_chain", {"chain_id": chain_id, "item_id": SEED_IDS[0]})
    ok &= need(r_unlink.get("result", {}).get("ok") is True,
               "9j/chain: unlink_recording_from_chain ok==True")

    r_end = call("end_chain", {"chain_id": chain_id})
    ok &= need(r_end.get("result", {}).get("ok") is True, "9j/chain: end_chain ok==True")

    return ok

run_check("9j/recording_chain_crud", check_recording_chain_crud)


# ---------------------------------------------------------------------------
# 9k — SummaryProfileManager (list baseline builtins → add custom → list verify)
#   No delete method exists (upsert-among-customs contract) — sentinel is left
#   in the throwaway store, destroyed with the container.
# ---------------------------------------------------------------------------

_SUMMARY_PROFILE_NAME = "e2e_smoke_profile"

def check_summary_profiles():
    ok = True
    r_list = call("list_summary_profiles", {})
    profiles = r_list.get("result", {}).get("profiles", [])
    names = [p.get("name") for p in profiles]
    builtin_names = {"brief", "detailed", "bullet_points", "meeting_notes", "telegram"}
    ok &= need(builtin_names.issubset(set(names)),
               f"9k/summary_profiles: all 5 builtins present (got {names!r})")

    r_add = call("add_summary_profile", {
        "name": _SUMMARY_PROFILE_NAME, "prompt": "E2E smoke sentinel system prompt.",
        "max_tokens": 200,
    })
    profile = r_add.get("result", {}).get("profile", {})
    ok &= need(profile.get("name") == _SUMMARY_PROFILE_NAME,
               "9k/summary_profiles: add_summary_profile returns matching name")
    ok &= need(profile.get("system_prompt") == "E2E smoke sentinel system prompt.",
               "9k/summary_profiles: system_prompt echoes the prompt param")
    ok &= need(profile.get("builtin") is False,
               "9k/summary_profiles: builtin==False for custom profile")

    r_list2 = call("list_summary_profiles", {})
    names_after = [p.get("name") for p in r_list2.get("result", {}).get("profiles", [])]
    ok &= need(_SUMMARY_PROFILE_NAME in names_after,
               "9k/summary_profiles: sentinel visible after add")

    return ok

run_check("9k/summary_profiles", check_summary_profiles)

# SKIPPED round-trips (method not in dispatch table or requires non-smoke state):
#   list_translation_glossary / get_translation_glossary — NOT in dispatch table;
#     glossary state is read via get_settings["translation_glossary"] (used in 9b above).
#   rename_collection — not in dispatch table (delete+recreate is the supported flow).
#   start_recording / stop_recording — require real audio I/O; out of scope for smoke.


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
