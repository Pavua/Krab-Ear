"""REST API сервер для Krab Ear на базе Flask.

Обеспечивает транскрибацию через HTTP-запросы, мониторинг здоровья и метрик.
"""

import os
import time
import uuid
import logging
from pathlib import Path
from flask import Flask, Response, request, jsonify, stream_with_context, g
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

ALLOWED_EXTENSIONS = {'.wav', '.mp3', '.ogg', '.m4a', '.flac', '.opus', '.webm', '.mp4', '.aac'}


@app.before_request
def start_timer():
    g._request_start = time.time()


@app.after_request
def log_request(response):
    logger.info("%s %s %s %dms", request.method, request.path, response.status_code,
                int((time.time() - g.get('_request_start', time.time())) * 1000))
    return response


VALID_QUALITY = {"fast", "balanced", "accurate"}
VALID_CLEANUP = {"off", "soft", "strict"}
VALID_DOMAIN = {"casual", "finance", "code", "conversational", "medical"}

# Итерация 2: Централизованная инициализация
# Используем один AudioEngine для всех подсистем во избежание перегрузки VRAM
engine = AudioEngine()
store = StateStore(settings.DATA_DIR)
transcriber = Transcriber(engine=engine)

TEMP_DIR = settings.DATA_DIR / "temp_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

@app.route("/health", methods=["GET"])
def health():
    """Liveness check — verifies the server process is running.

    Response 200: {"status": "ok", "service": "krab-ear", "profile": str}
        - status: always "ok" when the process is alive
        - service: constant "krab-ear"
        - profile: current quality profile loaded in the AudioEngine
    """
    return jsonify({"status": "ok", "service": "krab-ear", "profile": engine.quality_profile})

@app.route("/v1/readiness", methods=["GET"])
def readiness():
    """Readiness check — verifies all ML components are present and loadable.

    Unlike /health, this performs a real filesystem probe of the HuggingFace
    model cache for each component (STT, diarization, translation) rather than
    returning an optimistic status.

    Response 200: {"overall_ready": true, "components": {...}}
        - overall_ready: true only when every required component is available
        - components: per-component readiness flags (stt, diarization, translation)
    Response 503: same schema with overall_ready: false when any component is missing
    """
    report = BackendService._build_readiness_report_static()
    status_code = 200 if report["overall_ready"] else 503
    return jsonify(report), status_code

@app.route("/metrics", methods=["GET"])
def get_metrics():
    """Return aggregated performance and quality metrics.

    Metrics are computed over a sliding window by MetricsCollector.

    Response 200: {
        "latency_p50_ms": float,
        "latency_p95_ms": float,
        "latency_p99_ms": float,
        "confidence_avg": float,
        "request_count": int,
        "error_count": int
    }
    """
    return jsonify(metrics.get_summary())

MAX_VOCABULARY_SIZE = 500
MAX_WORD_LENGTH = 100

@app.route("/v1/vocabulary", methods=["GET", "POST"])
def manage_vocabulary():
    """Manage the persistent user vocabulary used to boost rare-word recognition.

    Words are merged into the global vocabulary stored via StateStore and injected
    into every subsequent transcription request as Whisper prompt hints.
    Maximum vocabulary size is 500 words; maximum word length is 100 characters.

    GET
        Response 200: {"words": [str, ...]}

    POST
        Request JSON: {"words": [str, ...]}
            - words: list of terms to add (duplicates silently ignored)
        Response 200: {"status": "ok", "count": int}
            - count: total vocabulary size after merge
        Response 400: {"error": str}
            - when words is not a list, or the merged total exceeds 500
    """
    if request.method == "POST":
        data = request.json or {}
        new_words = data.get("words", [])
        if not isinstance(new_words, list):
            return jsonify({"error": "words must be a list"}), 400

        # Validate each word: strip, truncate, drop empty
        new_words = [str(w).strip()[:MAX_WORD_LENGTH] for w in new_words if str(w).strip()]
        current = store.load_vocabulary()
        updated = list(set(current + new_words))
        if len(updated) > MAX_VOCABULARY_SIZE:
            return jsonify({"error": f"Vocabulary limit exceeded ({MAX_VOCABULARY_SIZE} words max)"}), 400
        store.save_vocabulary(updated)
        return jsonify({"status": "ok", "count": len(updated)})
    
    return jsonify({"words": store.load_vocabulary()})

@app.route("/v1/stt/transcribe", methods=["POST"])
def transcribe_audio():
    """Transcribe an audio file to text.

    Request: multipart/form-data
        - file: audio file (required)
            Allowed formats: .wav .mp3 .ogg .m4a .flac .opus .webm .mp4 .aac
            Maximum size: 500 MB
        - quality_profile: str (fast|balanced|accurate, default: balanced)
        - cleanup_profile: str (off|soft|strict, default: soft)
        - domain: str (casual|finance|code|conversational|medical, default: casual)
        - lang_hint: str (ISO 639-1 language code, optional — auto-detected when omitted)
        - vocabulary: str (comma-separated extra hint words, optional, merged with global vocab)
        - chat_id: str (idempotency namespace, optional)
        - message_id: str (idempotency key within chat_id, optional)

    If both chat_id and message_id are provided and the pair was seen before,
    the request is skipped without re-transcribing (idempotent replay protection).

    Response 200 (transcribed):
        {
            "status": "ok",
            "text": str,
            "confidence": float,          # 0.0–1.0
            "duration_ms": int,
            "engine": str,                # e.g. "mlx-whisper"
            "model": str,                 # model variant used
            "language": str,              # detected ISO 639-1 code
            "segments": [...],            # per-segment timestamps (may be empty)
            "diarization": {...},         # speaker labels (may be empty dict)
            "history_id": str             # StateStore record ID
        }
    Response 200 (skipped duplicate):
        {"status": "skipped", "reason": "duplicate"}
    Response 400: {"error": str}
        - missing file, unsupported extension, invalid parameter value
    Response 500: {"error": "Internal processing error"}
    """
    chat_id = request.form.get("chat_id")
    message_id = request.form.get("message_id")
    
    # 1. Проверка на дубликаты (идемпотентность)
    if chat_id and message_id and store.is_idempotent(chat_id, message_id):
        return jsonify({"status": "skipped", "reason": "duplicate"}), 200

    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    # 2. Безопасное сохранение временного файла
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400
    temp_path = TEMP_DIR / f"{uuid.uuid4().hex[:12]}_{filename}"
    file.save(str(temp_path))
    
    is_error = False
    try:
        # 3. Подготовка и транскрибация
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
        lang_hint = request.form.get("lang_hint") or None  # None = авто-определение whisper'ом

        # Интеграция словарей
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
        
        # 4. Сохранение результата в историю
        text = result.get("text", "")
        history_item = store.add_history_item(
            text=text,
            chat_id=chat_id or "",
            message_id=message_id or "",
            source_text=result.get("raw_text", text)
        )

        # 5. Сбор метрик
        metrics.record(
            latency_ms=result.get("duration_ms", int(elapsed_sec * 1000)),
            confidence=result.get("confidence", 0.0)
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

    except Exception as e:
        logger.exception("Ошибка при обработке аудио-запроса")
        metrics.record(0, 0, is_error=True)
        logger.exception("Transcription error")
        return jsonify({"error": "Internal processing error"}), 500
        
    finally:
        # 6. Гарантированная очистка временных ресурсов
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception as e:
                logger.warning("Не удалось удалить временный файл %s: %s", temp_path, e)

@app.route("/v1/events", methods=["GET"])
def events_stream():
    """Subscribe to real-time STT pipeline events via Server-Sent Events (SSE).

    The client opens a single long-lived GET connection and receives a stream of
    newline-delimited SSE frames.  A keepalive comment (`: ping`) is emitted
    approximately every 15 seconds when there are no events.

    Response: text/event-stream (HTTP 200, chunked)
        Headers set:
            Cache-Control: no-cache
            X-Accel-Buffering: no   (disables nginx proxy buffering)

    Event types emitted on the stream:

        event: stt.final
        data: {
            "history_id": str,
            "text": str,
            "confidence": float,
            "duration_sec": float,
            "language": str
        }

        event: stt.failed
        data: {
            "reason": str,
            "duration_sec": float
        }

    Usage (curl):
        curl -N http://127.0.0.1:5005/v1/events
    """
    return Response(
        stream_with_context(sse_stream(event_bus)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    # Запуск сервера на локальном интерфейсе
    app.run(host="127.0.0.1", port=5005)
