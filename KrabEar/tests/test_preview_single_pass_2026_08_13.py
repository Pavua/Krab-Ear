"""Превью не имеет права запускать тяжёлый фоллбэк-путь Whisper.

Живой инцидент 2026-08-13 05:13 (диктовка владельца потеряна, бэкенд убит):

    05:13:15  GigaAM не распознал речь — request-local fallback на Whisper
    05:13:22  Realtime preview worker не завершился за 1.5 с
    05:13:23  Смена профиля STT: balanced -> max   ← блокировка на MLX-локе
    05:16:21  handle_request завис дольше 180с (method=stop_recording)

Цепочка: превью получило короткий хвост, GigaAM вернул пусто, превью ушло в
фоллбэк на Whisper, тот повис под нехваткой памяти УДЕРЖИВАЯ mlx_lock —
и финальная транскрибация диктовки заблокировалась на этом же локе навсегда.

Превью косметическое: его текст всё равно заменяется финальной
транскрибацией. Платить за него полной цепочкой моделей нельзя — ровно та же
логика, по которой `single_pass` завели для live-субтитров (спека
2026-08-12-live-subs-single-pass-design.md).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.transcriber import Transcriber  # noqa: E402


class PreviewSinglePassTest(unittest.TestCase):
    """`transcribe_preview` обязано просить однопроходный режим."""

    def _make(self) -> tuple[Transcriber, MagicMock]:
        engine = MagicMock()
        engine.transcribe.return_value = {"text": "привет", "confidence": 0.9}
        tr = Transcriber(engine=engine)
        return tr, engine

    def test_transcribe_preview_requests_single_pass(self) -> None:
        tr, engine = self._make()

        tr.transcribe_preview([0.0], quality_profile="balanced")

        self.assertTrue(engine.transcribe.called, "engine.transcribe не вызван")
        _args, kwargs = engine.transcribe.call_args
        self.assertTrue(
            kwargs.get("single_pass"),
            "превью обязано передавать single_pass=True: без него пустой ответ "
            "GigaAM на коротком хвосте уводит превью в Whisper, который под "
            "нехваткой памяти виснет С УДЕРЖАНИЕМ mlx_lock и блокирует "
            "финальную транскрибацию диктовки владельца",
        )

    def test_preview_still_marked_as_preview(self) -> None:
        """single_pass добавляется, а не подменяет прежнюю семантику превью."""
        tr, engine = self._make()

        tr.transcribe_preview([0.0], quality_profile="balanced")

        _args, kwargs = engine.transcribe.call_args
        self.assertTrue(kwargs.get("is_preview"), "is_preview обязан остаться True")

    def test_full_transcribe_does_not_request_single_pass(self) -> None:
        """Путь ДИКТОВКИ не трогаем: там ретраи по уверенности оправданы —
        пользователь ждёт один результат и готов заплатить секундами."""
        tr, engine = self._make()

        tr.transcribe([0.0])

        _args, kwargs = engine.transcribe.call_args
        self.assertFalse(
            kwargs.get("single_pass", False),
            "финальная транскрибация обязана сохранить полную цепочку моделей",
        )


if __name__ == "__main__":
    unittest.main()
