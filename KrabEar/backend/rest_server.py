"""REST API сервер для Krab Ear на базе Flask + flask-smorest.

Обеспечивает транскрибацию через HTTP-запросы, мониторинг здоровья и метрик.
OpenAPI 3.0 документация доступна по адресу /api/docs.
"""

import os
import time
import uuid
import logging
import functools
from pathlib import Path
from flask import Flask, Response, request, jsonify, stream_with_context, g
from flask_smorest import Api, Blueprint
from marshmallow import Schema, fields as ma_fields, validate
from werkzeug.utils import secure_filename

from core.config import settings
from core.engine import AudioEngine
from backend.event_bus import bus as event_bus, sse_stream
from backend.service import BackendService
from backend.state_store import StateStore
from backend.transcriber import Transcriber
from backend.metrics_collector import metrics

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


@app.after_request
def log_request(response):
    logger.info("%s %s %s %dms", request.method, request.path, response.status_code,
                int((time.time() - g.get('_request_start', time.time())) * 1000))
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


@monitoring_blp.route("/health", methods=["GET"])
@monitoring_blp.response(200, HealthResponseSchema)
def health():
    """Liveness check — verifies the server process is running."""
    return {"status": "ok", "service": "krab-ear", "profile": engine.quality_profile}


@monitoring_blp.route("/metrics", methods=["GET"])
@monitoring_blp.response(200, MetricsResponseSchema)
@require_api_key
def get_metrics():
    """Return aggregated performance and quality metrics."""
    return metrics.get_summary()


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


def create_app():
    """Фабричная функция для запуска через WSGI-сервер (gunicorn и др.)."""
    return app


if __name__ == "__main__":
    # Запуск сервера на локальном интерфейсе
    app.run(host="127.0.0.1", port=5005)
