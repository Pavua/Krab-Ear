"""REST API сервер для Krab Ear на базе Flask.

Обеспечивает транскрибацию через HTTP-запросы, мониторинг здоровья и метрик.
"""

import os
import time
import logging
from pathlib import Path
from flask import Flask, Response, request, jsonify, stream_with_context
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

# Итерация 2: Централизованная инициализация
# Используем один AudioEngine для всех подсистем во избежание перегрузки VRAM
engine = AudioEngine()
store = StateStore(settings.DATA_DIR)
transcriber = Transcriber(engine=engine)

TEMP_DIR = settings.DATA_DIR / "temp_uploads"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

@app.route("/health", methods=["GET"])
def health():
    """Проверка доступности сервиса."""
    return jsonify({"status": "ok", "service": "krab-ear", "profile": engine.quality_profile})

@app.route("/v1/readiness", methods=["GET"])
def readiness():
    """Реальная проверка готовности компонентов (STT, diarization, translation).

    В отличие от /health не возвращает оптимистичный статус:
    каждый компонент проверяется через filesystem probe кэша HuggingFace.
    """
    report = BackendService._build_readiness_report_static()
    status_code = 200 if report["overall_ready"] else 503
    return jsonify(report), status_code

@app.route("/metrics", methods=["GET"])
def get_metrics():
    """Возвращает агрегированные метрики производительности и качества."""
    return jsonify(metrics.get_summary())

@app.route("/v1/vocabulary", methods=["GET", "POST"])
def manage_vocabulary():
    """Управление пользовательским словарем (для улучшения распознавания редких слов)."""
    if request.method == "POST":
        data = request.json or {}
        new_words = data.get("words", [])
        if not isinstance(new_words, list):
            return jsonify({"error": "words must be a list"}), 400
        
        current = store.load_vocabulary()
        updated = list(set(current + [str(w).strip() for w in new_words]))
        store.save_vocabulary(updated)
        return jsonify({"status": "ok", "count": len(updated)})
    
    return jsonify({"words": store.load_vocabulary()})

@app.route("/v1/stt/transcribe", methods=["POST"])
def transcribe_audio():
    """Основной эндпоинт транскрибации. Принимает аудиофайл и метаданные."""
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
    temp_path = TEMP_DIR / f"{int(time.time())}_{filename}"
    file.save(str(temp_path))
    
    is_error = False
    try:
        # 3. Подготовка и транскрибация
        engine.normalize_audio(str(temp_path))
        
        quality = request.form.get("quality_profile", "balanced")
        cleanup = request.form.get("cleanup_profile", "soft")
        domain = request.form.get("domain", "casual")
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
            "history_id": history_item.id,
        })

    except Exception as e:
        is_error = True
        logger.exception("Ошибка при обработке аудио-запроса")
        metrics.record(0, 0, is_error=True)
        return jsonify({"error": str(e)}), 500
        
    finally:
        # 6. Гарантированная очистка временных ресурсов
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception as e:
                logger.warning("Не удалось удалить временный файл %s: %s", temp_path, e)

@app.route("/v1/events", methods=["GET"])
def events_stream():
    """Server-Sent Events stream для событий STT pipeline.

    Клиент подключается один раз и получает поток событий:
      event: stt.completed
      data: {"history_id": "...", "text": "...", "duration_sec": 1.2, ...}

      event: stt.failed
      data: {"reason": "...", "duration_sec": 0.0}

    Keepalive-комментарий отправляется каждые ~15 секунд при отсутствии событий.
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
