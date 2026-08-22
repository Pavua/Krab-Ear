"""TDD-гард: причина пустого результата STT видна снаружи (2026-08-19).

Проблема (найдена разбором логов Voice Gateway, живой лог: `KrabEar STT: ''
(4846ms)`): POST /v1/stt/transcribe отдаёт HTTP 200 с ПУСТЫМ text и тогда,
когда в аудио была тишина, и тогда, когда распознать не смогли (вырожденный
вход / VAD-скип) — клиент не может отличить эти два случая, приходится
гадать по длительности ответа.

Причина внутри УЖЕ ЕСТЬ: `core/engine.py::AudioEngine._empty_transcription_
result(reason, language)` — единственный источник схемы для всех ранних
возвратов «распознавать нечего», с существующими reason'ами "empty_audio"
(вырожденное аудио < _MIN_TRANSCRIBE_SAMPLES) и "vad_skip" (VAD posчитал
окно тишиной). REST-ответ её не пробрасывает — эта причина теряется на
границе backend/rest_server.py::transcribe_audio.

Два уровня гарда:
1. EmptyTranscriptionResultReasonTest — engine.py отдаёт "reason" во
   ВНУТРЕННЕМ dict для ОБОИХ существующих причин; старые ключи не пропадают
   и не меняют значения (докстринг _empty_transcription_result требует этого
   от потребителей, читающих поля без .get()-заглушек).
2. TranscribeReasonRestContractTest — REST-ответ /v1/stt/transcribe
   пробрасывает "reason" ТОЛЬКО когда text пуст и reason известен; не
   засоряет ответ при нормальном распознавании; существующие поля ответа
   (status/text/confidence/duration_ms/engine/model/language/segments/
   diarization/history_id) не переименованы и не изменили значений —
   контракт для живого потребителя (Voice Gateway) не ломается.
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Layer 1 — core/engine.py: _empty_transcription_result несёт reason.
# ---------------------------------------------------------------------------

class EmptyTranscriptionResultReasonTest(unittest.TestCase):
    """_empty_transcription_result — единственный источник схемы пустого
    результата. Новое поле "reason" обязано появиться здесь, а не
    изобретаться заново на REST-стороне.
    """

    def test_reason_present_for_vad_skip(self):
        from core.engine import AudioEngine

        result = AudioEngine._empty_transcription_result("vad_skip", "ru")
        self.assertEqual(result.get("reason"), "vad_skip")

    def test_reason_present_for_empty_audio(self):
        from core.engine import AudioEngine

        result = AudioEngine._empty_transcription_result("empty_audio", None)
        self.assertEqual(result.get("reason"), "empty_audio")

    def test_stt_engine_is_none_when_vad_skips_transcription(self):
        """VAD прервал путь до вызова STT, поэтому имя движка не выдумываем."""
        from core.engine import AudioEngine

        result = AudioEngine._empty_transcription_result("vad_skip", "ru")
        self.assertIn("stt_engine", result)
        self.assertIsNone(result["stt_engine"])

    def test_existing_keys_unchanged(self):
        """Контракт докстринга: потребители читают старые ключи без .get() —
        новый ключ добавляется аддитивно, старые не переименовываются и не
        пропадают, их значения не меняются.
        """
        from core.engine import AudioEngine

        result = AudioEngine._empty_transcription_result("vad_skip", "ru")
        for key in (
            "text", "raw_text", "cleaned_text", "llm_applied", "llm_latency_ms",
            "llm_fallback_reason", "llm_diff", "confidence", "raw_confidence",
            "confidence_adjustments", "duration_ms", "engine", "model",
            "language", "segments", "diarization", "emotion",
        ):
            self.assertIn(key, result, f"ключ {key} исчез из пустого результата")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["raw_text"], "")
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["duration_ms"], 0)
        # Старое (пусть и путающее) поведение "engine" не трогаем этой волной —
        # только добавляем отдельное явное поле "reason" рядом.
        self.assertEqual(result["engine"], "vad_skip")
        self.assertEqual(result["language"], "ru")


# ---------------------------------------------------------------------------
# Layer 2 — REST /v1/stt/transcribe пробрасывает reason аддитивно.
# ---------------------------------------------------------------------------

_REST_AVAILABLE = False
try:
    import flask  # noqa: F401
    from unittest.mock import patch as _patch

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_engine.normalize_audio = MagicMock()
    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.is_idempotent.return_value = False
    _mock_store.load_settings.return_value = {}

    with _patch("core.engine.AudioEngine", return_value=_mock_engine), \
            _patch("backend.state_store.StateStore", return_value=_mock_store), \
            _patch("backend.transcriber.Transcriber", return_value=MagicMock()):
        import backend.rest_server as rs

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    rs = None


# Валидные magic bytes WAV (см. rest_server._validate_audio_magic_bytes):
# "RIFF" @0..3 + "WAVE" @8..11.
_WAV_BYTES = b"RIFF....WAVEfmt "


def _deps_with_transcribe(transcribe_result: dict):
    """StaticDeps с мокнутым transcriber.transcribe — без реального STT/ML."""
    store = MagicMock()
    store.load_vocabulary.return_value = []
    store.is_idempotent.return_value = False
    store.load_settings.return_value = {}
    store.add_history_item.return_value = MagicMock(id="hist-1")

    engine = MagicMock()
    engine.quality_profile = "balanced"
    engine.normalize_audio = MagicMock()

    transcriber = MagicMock()
    transcriber.transcribe.return_value = transcribe_result

    metrics = MagicMock()

    return rs.StaticDeps(
        engine=engine, store=store, transcriber=transcriber, translator=MagicMock(),
        tts_service=MagicMock(), metrics=metrics, event_bus=MagicMock(),
        sse_stream=MagicMock(),
    )


def _post_transcribe(deps):
    client = rs.create_app(
        deps, config_mapping={"TESTING": True, "RATELIMIT_ENABLED": False},
    ).test_client()
    data = {"file": (io.BytesIO(_WAV_BYTES), "audio.wav")}
    return client.post("/v1/stt/transcribe", data=data, content_type="multipart/form-data")


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class TranscribeReasonRestContractTest(unittest.TestCase):
    """VG (и любой другой клиент) должен уметь отличить тишину от сбоя
    распознавания по ЯВНОМУ полю ответа, а не по угадыванию длительности.
    """

    def test_empty_result_with_known_reason_is_exposed(self):
        from core.engine import AudioEngine

        empty_result = AudioEngine._empty_transcription_result("vad_skip", "ru")
        deps = _deps_with_transcribe(empty_result)

        resp = _post_transcribe(deps)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("text"), "")
        self.assertEqual(body.get("reason"), "vad_skip")

    def test_empty_result_second_known_reason_is_exposed(self):
        from core.engine import AudioEngine

        empty_result = AudioEngine._empty_transcription_result("empty_audio", None)
        deps = _deps_with_transcribe(empty_result)

        resp = _post_transcribe(deps)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("text"), "")
        self.assertEqual(body.get("reason"), "empty_audio")

    def test_empty_result_exposes_null_stt_engine_without_changing_legacy_engine(self):
        from core.engine import AudioEngine

        empty_result = AudioEngine._empty_transcription_result("vad_skip", "ru")
        deps = _deps_with_transcribe(empty_result)

        resp = _post_transcribe(deps)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["engine"], "vad_skip")
        self.assertEqual(body["reason"], "vad_skip")
        self.assertIn("stt_engine", body)
        self.assertIsNone(body["stt_engine"])

    def test_normal_recognition_has_no_reason_field(self):
        """Непустой текст — ответ НЕ засоряется полем reason."""
        normal_result = {
            "text": "привет мир", "raw_text": "привет мир", "cleaned_text": "привет мир",
            "confidence": 0.92, "duration_ms": 1200, "engine": "mlx-whisper",
            "model": "whisper-large-v3-turbo", "language": "ru", "segments": [],
            "diarization": {},
        }
        deps = _deps_with_transcribe(normal_result)

        resp = _post_transcribe(deps)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("text"), "привет мир")
        self.assertNotIn("reason", body)
        self.assertEqual(body.get("stt_engine"), "mlx-whisper")

    def test_existing_response_fields_unchanged(self):
        """Контракт для старых клиентов (Voice Gateway): все поля, что были
        в ответе, — те же имена, те же значения; ничего не переименовано.
        """
        normal_result = {
            "text": "тест", "confidence": 0.5, "duration_ms": 900,
            "engine": "mlx-whisper", "model": "m", "language": "ru",
            "segments": [{"start": 0, "end": 1, "text": "тест"}],
            "diarization": {"speakers": 1},
        }
        deps = _deps_with_transcribe(normal_result)

        resp = _post_transcribe(deps)
        body = resp.get_json()

        for key in (
            "status", "text", "confidence", "duration_ms", "engine", "model",
            "language", "segments", "diarization", "history_id",
        ):
            self.assertIn(key, body, f"существующее поле {key} исчезло из ответа")
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["text"], "тест")
        self.assertEqual(body["confidence"], 0.5)
        self.assertEqual(body["duration_ms"], 900)
        self.assertEqual(body["engine"], "mlx-whisper")
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["language"], "ru")
        self.assertEqual(body["segments"], [{"start": 0, "end": 1, "text": "тест"}])
        self.assertEqual(body["diarization"], {"speakers": 1})
        self.assertEqual(body["history_id"], "hist-1")


if __name__ == "__main__":
    unittest.main()
