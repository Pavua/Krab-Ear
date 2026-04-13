"""REST API сервер для Krab Ear на базе Flask + flask-smorest.

Обеспечивает транскрибацию через HTTP-запросы, мониторинг здоровья и метрик.
OpenAPI 3.0 документация доступна по адресу /api/docs.
"""

import json
import math
import os
import queue
import time
import uuid
import logging
import functools
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, Response, request, jsonify, stream_with_context, g
from flask_smorest import Api, Blueprint
from flask_sock import Sock
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
from marshmallow import Schema, fields as ma_fields, validate
from werkzeug.utils import secure_filename

from core.config import settings
from core.engine import AudioEngine
from backend.event_bus import bus as event_bus, sse_stream
from backend.service import BackendService
from backend.state_store import StateStore
from backend.transcriber import Transcriber
from backend.metrics_collector import metrics
from backend.api_versioning import api_version_header, get_api_info

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("KrabEar.REST")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB max

# flask-smorest / OpenAPI 3.0 configuration
app.config["API_TITLE"] = "Krab Ear REST API"
app.config["API_VERSION"] = "v1"
app.config["OPENAPI_VERSION"] = "3.0.3"
app.config["OPENAPI_URL_PREFIX"] = "/api"
app.config["OPENAPI_SWAGGER_UI_PATH"] = "/docs"
app.config["OPENAPI_SWAGGER_UI_URL"] = "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"

api = Api(app)
sock = Sock(app)

# Attach API version header to every response.
app.after_request(api_version_header())

# ---------------------------------------------------------------------------
# CORS — разрешает кросс-доменные запросы из браузера.
# Список origins берётся из KRAB_EAR_CORS_ORIGINS (по умолчанию "*").
# ---------------------------------------------------------------------------

def _parse_cors_origins(raw: str):
    """Парсит строку origins: "*" → "*", иначе список через запятую."""
    if raw.strip() == "*":
        return "*"
    return [o.strip() for o in raw.split(",") if o.strip()]


CORS(
    app,
    origins=_parse_cors_origins(settings.CORS_ORIGINS),
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    expose_headers=["X-Request-ID", "Retry-After"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
)

# ---------------------------------------------------------------------------
# Rate limiting — flask-limiter с in-memory хранилищем.
# Отключается через KRAB_EAR_RATE_LIMIT_ENABLED=false (тесты/dev).
# ---------------------------------------------------------------------------

def _rate_limit_exceeded_handler(e):
    """Возвращает 429 JSON с retry_after вместо стандартного HTML."""
    retry_after = 60
    try:
        retry_after = math.ceil(e.description.retry_after.total_seconds())
    except Exception:
        pass
    response = jsonify({"error": "rate_limit_exceeded", "retry_after": retry_after})
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["60 per minute"] if settings.RATE_LIMIT_ENABLED else [],
    storage_uri="memory://",
    enabled=settings.RATE_LIMIT_ENABLED,
    headers_enabled=True,
)

app.register_error_handler(429, _rate_limit_exceeded_handler)

# WebSocket heartbeat interval (seconds)
_WS_HEARTBEAT_SEC = 30
# How long to block waiting for next event before looping (keep < heartbeat)
_WS_POLL_SEC = 5.0


# ---------------------------------------------------------------------------
# Auth decorator — optional Bearer token.
# Если REST_API_KEY пустой, аутентификация пропускается (обратная совместимость).
# ---------------------------------------------------------------------------

def require_api_key(f):
    """Decorator: enforce Bearer token auth when REST_API_KEY is configured."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        api_key = settings.REST_API_KEY
        if not api_key:
            # Auth disabled — pass through
            return f(*args, **kwargs)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        token = auth_header[len("Bearer "):]
        if token != api_key:
            return jsonify({"error": "Invalid API key"}), 401
        return f(*args, **kwargs)
    return decorated


ALLOWED_EXTENSIONS = {'.wav', '.mp3', '.ogg', '.m4a', '.flac', '.opus', '.webm', '.mp4', '.aac'}

VALID_QUALITY = {"fast", "balanced", "accurate"}
VALID_CLEANUP = {"off", "soft", "strict"}
VALID_DOMAIN = {"casual", "finance", "code", "conversational", "medical"}

# ---------------------------------------------------------------------------
# Marshmallow schemas
# ---------------------------------------------------------------------------

class HealthResponseSchema(Schema):
    status = ma_fields.String(metadata={"description": "Always 'ok' when alive"})
    service = ma_fields.String(metadata={"description": "Service name constant"})
    profile = ma_fields.String(metadata={"description": "Current AudioEngine quality profile"})


class ReadinessComponentsSchema(Schema):
    stt = ma_fields.Boolean()
    diarization = ma_fields.Boolean()
    translation = ma_fields.Boolean()


class ReadinessResponseSchema(Schema):
    overall_ready = ma_fields.Boolean(metadata={"description": "True when all required components are available"})
    components = ma_fields.Dict(keys=ma_fields.String(), values=ma_fields.Boolean())


class MetricsResponseSchema(Schema):
    latency_p50_ms = ma_fields.Float(load_default=None, dump_default=None)
    latency_p95_ms = ma_fields.Float(load_default=None, dump_default=None)
    latency_p99_ms = ma_fields.Float(load_default=None, dump_default=None)
    confidence_avg = ma_fields.Float(load_default=None, dump_default=None)
    request_count = ma_fields.Integer(load_default=0, dump_default=0)
    error_count = ma_fields.Integer(load_default=0, dump_default=0)
    total_requests = ma_fields.Integer(load_default=0, dump_default=0)
    error_rate = ma_fields.Float(load_default=0, dump_default=0)
    status = ma_fields.String(load_default="waiting_data", dump_default="waiting_data")


class VocabularyResponseSchema(Schema):
    words = ma_fields.List(ma_fields.String(), metadata={"description": "Current vocabulary list"})


class VocabularyPostSchema(Schema):
    words = ma_fields.List(
        ma_fields.String(),
        required=True,
        metadata={"description": "Words to add to vocabulary"},
    )


class VocabularyUpdateResponseSchema(Schema):
    status = ma_fields.String()
    count = ma_fields.Integer(metadata={"description": "Total vocabulary size after merge"})


class ErrorSchema(Schema):
    error = ma_fields.String()


class TranscribeResponseSchema(Schema):
    status = ma_fields.String()
    text = ma_fields.String(load_default="")
    confidence = ma_fields.Float(load_default=0.0)
    duration_ms = ma_fields.Integer(load_default=0)
    engine = ma_fields.String(load_default="mlx-whisper")
    model = ma_fields.String(load_default="")
    language = ma_fields.String(load_default=None, allow_none=True)
    segments = ma_fields.List(ma_fields.Dict(), load_default=list)
    diarization = ma_fields.Dict(load_default=dict)
    history_id = ma_fields.String(load_default="")
    reason = ma_fields.String(load_default=None, allow_none=True)


# ---------------------------------------------------------------------------
# Централизованная инициализация
# Используем один AudioEngine для всех подсистем во избежание перегрузки VRAM
# ---------------------------------------------------------------------------

engine = AudioEngine()
store = StateStore(settings.DATA_DIR)
transcriber = Transcriber(engine=engine)

TEMP_DIR = settings.DATA_DIR / "temp_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Request timing middleware
# ---------------------------------------------------------------------------

@app.before_request
def start_timer():
    g._request_start = time.time()
    g._request_id = str(uuid.uuid4())


@app.after_request
def log_request(response):
    duration_ms = int((time.time() - g.get('_request_start', time.time())) * 1000)
    request_id = g.get('_request_id', str(uuid.uuid4()))

    # Attach request ID to response header for traceability
    response.headers['X-Request-ID'] = request_id

    if settings.LOG_FORMAT == "json":
        log_record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "request_id": request_id,
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "ip": request.remote_addr or "",
            "content_length": request.content_length or 0,
        }
        logger.info(json.dumps(log_record))
    else:
        logger.info(
            "%s %s %s %dms [%s]",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            request_id,
        )
    return response


# ---------------------------------------------------------------------------
# Blueprints
# ---------------------------------------------------------------------------

# ── health / metrics (no version prefix) ────────────────────────────────────
monitoring_blp = Blueprint(
    "monitoring",
    __name__,
    url_prefix="",
    description="Liveness, readiness, and metrics endpoints",
)


@monitoring_blp.route("/info", methods=["GET"])
def api_info():
    """Return API version metadata — supported versions, current default, deprecated."""
    return jsonify(get_api_info())


@monitoring_blp.route("/health", methods=["GET"])
@monitoring_blp.response(200, HealthResponseSchema)
@limiter.limit("120 per minute")
def health():
    """Liveness check — verifies the server process is running."""
    return {"status": "ok", "service": "krab-ear", "profile": engine.quality_profile}


@monitoring_blp.route("/metrics", methods=["GET"])
@monitoring_blp.response(200, MetricsResponseSchema)
@require_api_key
def get_metrics():
    """Return aggregated performance and quality metrics."""
    return metrics.get_summary()


# ---------------------------------------------------------------------------
# Prometheus text-format metrics endpoint
# ---------------------------------------------------------------------------

_SERVER_START_TIME = time.time()

# Prometheus histogram bucket boundaries (seconds)
_PROM_LATENCY_BUCKETS_SEC = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]


def _build_prometheus_text(summary: dict) -> str:
    """Формирует строку в формате Prometheus text exposition format 0.0.4."""
    lines = []

    # ── transcriptions_total ─────────────────────────────────────────────────
    total = summary.get("total_requests", 0)
    lines += [
        "# HELP krab_ear_transcriptions_total Total number of transcription requests",
        "# TYPE krab_ear_transcriptions_total counter",
        f"krab_ear_transcriptions_total {total}",
    ]

    # ── errors_total ─────────────────────────────────────────────────────────
    error_rate = summary.get("error_rate", 0.0)
    errors = round(error_rate * total) if total else 0
    lines += [
        "# HELP krab_ear_errors_total Total number of failed transcription requests",
        "# TYPE krab_ear_errors_total counter",
        f"krab_ear_errors_total {errors}",
    ]

    # ── confidence_avg ───────────────────────────────────────────────────────
    stt = summary.get("stt_metrics", {})
    conf_avg = stt.get("confidence", {}).get("avg", 0.0) if stt else 0.0
    lines += [
        "# HELP krab_ear_confidence_avg Average STT confidence score (0-1)",
        "# TYPE krab_ear_confidence_avg gauge",
        f"krab_ear_confidence_avg {conf_avg:.4f}",
    ]

    # ── uptime_seconds ────────────────────────────────────────────────────────
    uptime = time.time() - _SERVER_START_TIME
    lines += [
        "# HELP krab_ear_uptime_seconds Time since the REST server process started",
        "# TYPE krab_ear_uptime_seconds gauge",
        f"krab_ear_uptime_seconds {uptime:.3f}",
    ]

    # ── stt_latency histogram ────────────────────────────────────────────────
    lines += [
        "# HELP krab_ear_stt_latency_seconds STT transcription latency",
        "# TYPE krab_ear_stt_latency_seconds histogram",
    ]

    latency_ms_data = stt.get("latency_ms", {}) if stt else {}
    p50 = latency_ms_data.get("p50")
    p95 = latency_ms_data.get("p95")
    p99 = latency_ms_data.get("p99")
    avg_ms = latency_ms_data.get("avg")

    # Build approximate bucket counts from percentile data.
    # Uses p50 / p95 / p99 as thresholds to infer cumulative counts.
    window = summary.get("window_size", 0)
    for le in _PROM_LATENCY_BUCKETS_SEC:
        le_ms = le * 1000.0
        if window == 0 or p50 is None:
            count = 0
        elif le_ms < (p50 or 0):
            count = int(window * 0.49)
        elif le_ms < (p95 or 0):
            count = int(window * 0.94)
        elif le_ms < (p99 or 0):
            count = int(window * 0.98)
        else:
            count = window
        lines.append(f'krab_ear_stt_latency_seconds_bucket{{le="{le}"}} {count}')

    lines.append(f"krab_ear_stt_latency_seconds_bucket{{le=\"+Inf\"}} {window}")
    sum_sec = (avg_ms / 1000.0 * window) if (avg_ms is not None and window) else 0.0
    lines += [
        f"krab_ear_stt_latency_seconds_sum {sum_sec:.6f}",
        f"krab_ear_stt_latency_seconds_count {window}",
    ]

    return "\n".join(lines) + "\n"


@monitoring_blp.route("/metrics/prometheus", methods=["GET"])
@require_api_key
def get_metrics_prometheus():
    """Return metrics in Prometheus text exposition format 0.0.4.

    No external dependencies — format generated manually.
    Content-Type: text/plain; version=0.0.4; charset=utf-8
    """
    summary = metrics.get_summary()
    body = _build_prometheus_text(summary)
    return Response(
        body,
        status=200,
        mimetype="text/plain; version=0.0.4; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Health dashboard HTML page
# ---------------------------------------------------------------------------

def _status_dot_color(status: str) -> str:
    """Возвращает CSS-цвет индикатора по строке статуса."""
    green = {"ok", "healthy", "closed"}
    yellow = {"warning", "degraded", "circuit_open", "unavailable", "waiting_data"}
    if status in green:
        return "#4ade80"
    if status in yellow:
        return "#fbbf24"
    return "#f87171"


def _build_dashboard_html() -> str:
    """Строит самодостаточную HTML-страницу дашборда состояния."""
    import platform

    try:
        import psutil  # type: ignore
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        mem_used_gb = round(mem.used / (1024 ** 3), 2)
        mem_total_gb = round(mem.total / (1024 ** 3), 2)
        mem_pct = mem.percent
        disk = psutil.disk_usage("/")
        disk_free_gb = round(disk.free / (1024 ** 3), 2)
        disk_total_gb = round(disk.total / (1024 ** 3), 2)
        sys_info_available = True
    except Exception:
        cpu_pct = mem_used_gb = mem_total_gb = mem_pct = None
        disk_free_gb = disk_total_gb = None
        sys_info_available = False

    # Health checks
    try:
        from backend.health_checker import HealthChecker
        checker = HealthChecker(store=store, transcriber=transcriber)
        health_data = checker.check_all()
    except Exception as exc:
        health_data = {
            "status": "error",
            "checks": {},
            "uptime_sec": round(time.time() - _SERVER_START_TIME, 1),
            "version": "unknown",
            "error": str(exc),
        }

    # Metrics summary
    try:
        metrics_summary = metrics.get_summary()
    except Exception:
        metrics_summary = {}

    uptime_sec = health_data.get("uptime_sec", round(time.time() - _SERVER_START_TIME, 1))
    uptime_str = _format_uptime(uptime_sec)
    overall_status = health_data.get("status", "unknown")
    overall_color = _status_dot_color(overall_status)
    version = health_data.get("version", "unknown")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    py_version = platform.python_version()
    platform_str = platform.platform()

    # Build health check rows
    check_rows = ""
    for name, result in health_data.get("checks", {}).items():
        st = result.get("status", "unknown")
        color = _status_dot_color(st)
        details_parts = []
        for k, v in result.items():
            if k == "status":
                continue
            details_parts.append(f"{k}: {v}")
        details = " | ".join(details_parts) if details_parts else ""
        check_rows += f"""
            <tr>
              <td><span class="dot" style="background:{color}"></span>{name}</td>
              <td><span class="badge" style="background:{color}22;color:{color};border:1px solid {color}44">{st}</span></td>
              <td class="detail">{details}</td>
            </tr>"""

    # Build metrics section
    stt = metrics_summary.get("stt_metrics", {}) or {}
    lat = stt.get("latency_ms", {}) or {}
    conf = stt.get("confidence", {}) or {}
    p50 = lat.get("p50")
    p95 = lat.get("p95")
    p99 = lat.get("p99")
    conf_avg = conf.get("avg")
    total_req = metrics_summary.get("total_requests", 0)
    err_rate = metrics_summary.get("error_rate", 0)

    def _fmt(v, suffix=""):
        return f"{v:.1f}{suffix}" if v is not None else "—"

    metrics_rows = f"""
            <tr><td>Total requests</td><td>{total_req}</td></tr>
            <tr><td>Error rate</td><td>{_fmt(err_rate * 100 if err_rate else None, '%') if err_rate else '0%'}</td></tr>
            <tr><td>Latency p50</td><td>{_fmt(p50, ' ms')}</td></tr>
            <tr><td>Latency p95</td><td>{_fmt(p95, ' ms')}</td></tr>
            <tr><td>Latency p99</td><td>{_fmt(p99, ' ms')}</td></tr>
            <tr><td>Confidence avg</td><td>{_fmt(conf_avg)}</td></tr>"""

    # System info rows
    if sys_info_available:
        sys_rows = f"""
            <tr><td>CPU usage</td><td>{cpu_pct:.1f}%</td></tr>
            <tr><td>Memory</td><td>{mem_used_gb} / {mem_total_gb} GB ({mem_pct:.1f}%)</td></tr>
            <tr><td>Disk free</td><td>{disk_free_gb} / {disk_total_gb} GB</td></tr>"""
    else:
        sys_rows = "<tr><td colspan='2' class='detail'>psutil not available</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>Krab Ear — Health Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg: #0f1117;
      --surface: #1a1d27;
      --border: #2a2d3a;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #6366f1;
    }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      padding: 24px;
      max-width: 1000px;
      margin: 0 auto;
    }}
    header {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 28px;
      flex-wrap: wrap;
    }}
    header h1 {{
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }}
    .overall-badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 600;
      border: 1px solid;
    }}
    .meta {{
      margin-left: auto;
      color: var(--muted);
      font-size: 12px;
      text-align: right;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px;
    }}
    .card h2 {{
      font-size: 13px;
      font-weight: 600;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 14px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    td {{
      padding: 6px 4px;
      vertical-align: middle;
    }}
    tr + tr td {{
      border-top: 1px solid var(--border);
    }}
    td:last-child {{
      text-align: right;
      color: var(--muted);
    }}
    .dot {{
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      margin-right: 8px;
      vertical-align: middle;
      flex-shrink: 0;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 500;
    }}
    .detail {{
      color: var(--muted);
      font-size: 12px;
    }}
    .full-width {{
      grid-column: 1 / -1;
    }}
    .refresh-note {{
      text-align: center;
      color: var(--muted);
      font-size: 12px;
      margin-top: 24px;
    }}
    @media (max-width: 600px) {{
      body {{ padding: 12px; }}
      header {{ gap: 8px; }}
      .meta {{ margin-left: 0; }}
    }}
  </style>
</head>
<body>
  <header>
    <span style="font-size:28px">🦀</span>
    <h1>Krab Ear — Health Dashboard</h1>
    <span class="overall-badge" style="background:{overall_color}22;color:{overall_color};border-color:{overall_color}44">
      <span class="dot" style="background:{overall_color}"></span>
      {overall_status.upper()}
    </span>
    <div class="meta">
      <div>v{version} · Python {py_version}</div>
      <div>Updated: {now_str}</div>
    </div>
  </header>

  <div class="grid">
    <!-- Uptime + identity -->
    <div class="card">
      <h2>Service</h2>
      <table>
        <tr><td>Uptime</td><td>{uptime_str}</td></tr>
        <tr><td>Platform</td><td class="detail" style="font-size:11px">{platform_str}</td></tr>
        <tr><td>Quality profile</td><td>{engine.quality_profile}</td></tr>
      </table>
    </div>

    <!-- System resources -->
    <div class="card">
      <h2>System Resources</h2>
      <table>{sys_rows}
      </table>
    </div>

    <!-- Metrics -->
    <div class="card">
      <h2>Recent Metrics</h2>
      <table>{metrics_rows}
      </table>
    </div>

    <!-- Health checks — full width -->
    <div class="card full-width">
      <h2>Health Checks</h2>
      <table>
        <colgroup>
          <col style="width:22%">
          <col style="width:18%">
          <col>
        </colgroup>
        <tr style="color:var(--muted);font-size:12px">
          <td>Check</td><td>Status</td><td>Details</td>
        </tr>{check_rows}
      </table>
    </div>
  </div>

  <p class="refresh-note">Auto-refreshes every 30 seconds &mdash; <a href="/health/dashboard" style="color:var(--accent)">refresh now</a></p>
</body>
</html>"""
    return html


def _format_uptime(seconds: float) -> str:
    """Форматирует секунды в читаемую строку вида '2d 3h 14m 05s'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs:02d}s")
    return " ".join(parts)


@monitoring_blp.route("/health/dashboard", methods=["GET"])
def health_dashboard():
    """Self-contained HTML health dashboard with auto-refresh every 30 seconds.

    Returns a dark-themed, responsive HTML page showing:
      - Overall service status
      - Health check results (stt_model, llm, disk_space, history_store, audio_devices)
      - System resources (CPU, memory, disk) — requires psutil
      - Recent STT metrics (latency percentiles, confidence, error rate)
      - Uptime and version info

    No external CSS/JS dependencies — everything is inlined.
    """
    html = _build_dashboard_html()
    return Response(html, status=200, mimetype="text/html; charset=utf-8")


# ── v1 APIs ──────────────────────────────────────────────────────────────────
v1_blp = Blueprint(
    "v1",
    __name__,
    url_prefix="/v1",
    description="Krab Ear v1 API — STT, vocabulary, readiness, events",
)


@v1_blp.route("/readiness", methods=["GET"])
@require_api_key
def readiness():
    """Readiness check — verifies all ML components are present and loadable.

    Unlike /health, this performs a real filesystem probe of the HuggingFace
    model cache for each component (STT, diarization, translation).
    Returns 503 when any required component is missing.

    Response 200/503: {"overall_ready": bool, "components": {stt, diarization, translation}}
    """
    report = BackendService._build_readiness_report_static()
    status_code = 200 if report["overall_ready"] else 503
    return jsonify(report), status_code


MAX_VOCABULARY_SIZE = 500
MAX_WORD_LENGTH = 100


@v1_blp.route("/vocabulary", methods=["GET"])
@v1_blp.response(200, VocabularyResponseSchema)
def get_vocabulary():
    """Return the current persistent user vocabulary."""
    return {"words": store.load_vocabulary()}


@v1_blp.route("/vocabulary", methods=["POST"])
@v1_blp.arguments(VocabularyPostSchema)
@v1_blp.response(200, VocabularyUpdateResponseSchema)
def add_vocabulary(args):
    """Add words to the persistent user vocabulary.

    Words are merged into the global vocabulary stored via StateStore.
    Duplicates are silently ignored.
    Maximum vocabulary size: 500 words; maximum word length: 100 characters.
    """
    new_words = args["words"]
    new_words = [str(w).strip()[:MAX_WORD_LENGTH] for w in new_words if str(w).strip()]
    current = store.load_vocabulary()
    updated = list(set(current + new_words))
    if len(updated) > MAX_VOCABULARY_SIZE:
        from flask_smorest import abort
        abort(400, message=f"Vocabulary limit exceeded ({MAX_VOCABULARY_SIZE} words max)")
    store.save_vocabulary(updated)
    return {"status": "ok", "count": len(updated)}


@v1_blp.route("/stt/transcribe", methods=["POST"])
@v1_blp.response(200, TranscribeResponseSchema)
@limiter.limit("10 per minute")
def transcribe_audio():
    """Transcribe an audio file to text.

    Request: multipart/form-data
        - file: audio file (required). Allowed: .wav .mp3 .ogg .m4a .flac .opus .webm .mp4 .aac
        - quality_profile: fast|balanced|accurate (default: balanced)
        - cleanup_profile: off|soft|strict (default: soft)
        - domain: casual|finance|code|conversational|medical (default: casual)
        - lang_hint: ISO 639-1 code (optional, auto-detected when omitted)
        - vocabulary: comma-separated hint words (optional)
        - chat_id + message_id: idempotency key pair (optional)
    """
    chat_id = request.form.get("chat_id")
    message_id = request.form.get("message_id")

    # Идемпотентность
    if chat_id and message_id and store.is_idempotent(chat_id, message_id):
        return jsonify({"status": "skipped", "reason": "duplicate"}), 200

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400
    temp_path = TEMP_DIR / f"{uuid.uuid4().hex[:12]}_{filename}"
    file.save(str(temp_path))

    try:
        engine.normalize_audio(str(temp_path))

        quality = request.form.get("quality_profile", "balanced")
        if quality not in VALID_QUALITY:
            return jsonify({"error": f"Invalid quality_profile: {quality}"}), 400
        cleanup = request.form.get("cleanup_profile", "soft")
        if cleanup not in VALID_CLEANUP:
            return jsonify({"error": f"Invalid cleanup_profile: {cleanup}"}), 400
        domain = request.form.get("domain", "casual")
        if domain not in VALID_DOMAIN:
            return jsonify({"error": f"Invalid domain: {domain}"}), 400
        lang_hint = request.form.get("lang_hint") or None

        req_vocab_raw = request.form.get("vocabulary", "")
        req_vocab = [w.strip() for w in req_vocab_raw.split(",") if w.strip()] if req_vocab_raw else []
        full_vocabulary = list(set(store.load_vocabulary() + req_vocab))

        start_ts = time.monotonic()
        result = transcriber.transcribe(
            str(temp_path),
            quality_profile=quality,
            cleanup_profile=cleanup,
            domain=domain,
            extra_vocabulary=full_vocabulary,
            lang_hint=lang_hint,
        )
        elapsed_sec = time.monotonic() - start_ts

        text = result.get("text", "")
        history_item = store.add_history_item(
            text=text,
            chat_id=chat_id or "",
            message_id=message_id or "",
            source_text=result.get("raw_text", text),
        )

        metrics.record(
            latency_ms=result.get("duration_ms", int(elapsed_sec * 1000)),
            confidence=result.get("confidence", 0.0),
        )

        return jsonify({
            "status": "ok",
            "text": text,
            "confidence": result.get("confidence", 0.0),
            "duration_ms": result.get("duration_ms", int(elapsed_sec * 1000)),
            "engine": result.get("engine", "mlx-whisper"),
            "model": result.get("model", ""),
            "language": result.get("language"),
            "segments": result.get("segments", []),
            "diarization": result.get("diarization", {}),
            "history_id": history_item.id,
        })

    except Exception:
        logger.exception("Ошибка при обработке аудио-запроса")
        metrics.record(0, 0, is_error=True)
        return jsonify({"error": "Internal processing error"}), 500

    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception as e:
                logger.warning("Не удалось удалить временный файл %s: %s", temp_path, e)


@v1_blp.route("/events", methods=["GET"])
def events_stream():
    """Subscribe to real-time STT pipeline events via Server-Sent Events (SSE).

    Opens a long-lived GET connection that emits newline-delimited SSE frames.
    A keepalive comment (`: ping`) is emitted ~every 15 seconds when idle.

    Event types:

        event: stt.final  →  {history_id, text, confidence, duration_sec, language}

        event: stt.failed →  {reason, duration_sec}
    """
    return Response(
        stream_with_context(sse_stream(event_bus)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Compatibility shim: keep /v1/vocabulary working for GET+POST on the same
# path without duplicate-route errors from flask-smorest
# ---------------------------------------------------------------------------

# Register blueprints
api.register_blueprint(monitoring_blp)
api.register_blueprint(v1_blp)


# ---------------------------------------------------------------------------
# WebSocket live-streaming endpoint
# ---------------------------------------------------------------------------

def _handle_ws_connection(ws, bus, type_filter=None):
    """Внутренняя логика WebSocket-соединения — выделена для unit-тестирования.

    Args:
        ws: объект WebSocket с методом .send(str).
        bus: EventBus для подписки.
        type_filter: set строк с допустимыми типами событий, или None — всё.
    """
    q = bus.subscribe()
    logger.info("WS /ws/events: клиент подключился, фильтр=%s", type_filter or "все")

    last_ping = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - last_ping >= _WS_HEARTBEAT_SEC:
                try:
                    ws.send('{"type":"ping"}')
                except Exception:
                    break
                last_ping = time.monotonic()

            try:
                event = q.get(timeout=_WS_POLL_SEC)
            except queue.Empty:
                continue

            if event is None:
                # Сигнал завершения от сервера
                break

            if type_filter and event.get("type") not in type_filter:
                continue

            try:
                ws.send(json.dumps(event))
            except Exception:
                break
    except Exception:
        logger.debug("WS /ws/events: соединение прервано")
    finally:
        bus.unsubscribe(q)
        logger.info("WS /ws/events: клиент отключился")


@sock.route("/ws/events")
def ws_events(ws):
    """WebSocket endpoint для стриминга событий транскрибации в реальном времени.

    Подписывается на EventBus и пересылает все события клиенту в формате JSON.

    Query params:
        types — опциональный фильтр по типам событий через запятую.
                Пример: /ws/events?types=stt.final,translation
                Если не задан — отправляются все события.

    Протокол:
        Server → Client: JSON-строка {type, ts, data}
        Server → Client: {"type": "ping"} каждые 30 секунд (heartbeat)
        Client → Server: любые входящие данные игнорируются
    """
    raw_types = request.args.get("types", "")
    type_filter = {t.strip() for t in raw_types.split(",") if t.strip()} if raw_types else None
    _handle_ws_connection(ws, event_bus, type_filter)


def create_app():
    """Фабричная функция для запуска через WSGI-сервер (gunicorn и др.)."""
    return app


if __name__ == "__main__":
    # Запуск сервера на локальном интерфейсе
    app.run(host="127.0.0.1", port=5005)
