"""Bulk reprocess не удерживает mlx_lock во время transcribe (самоблокировка через пул).

Тот же класс, что починен в превью (`test_preview_mlx_self_block_2026_09_02.py`):
вспомогательный внешний захват блокирует ту самую работу, ради которой брался.

Цепочка самоблокировки:
  1. `BulkReprocessor.reprocess` в потоке A берёт `mlx_lock`;
  2. зовёт `transcriber.transcribe(...)` → `engine.transcribe(...)`;
  3. движок отдаёт адаптер в `ThreadPoolExecutor` — поток B
     (`engine.py`: `_pool.submit(adapter_fn)` и `_executor.submit(self._transcribe_model, ...)`);
  4. адаптер в потоке B берёт ТОТ ЖЕ `mlx_lock` — GigaAM-MLX
     (`stt_gigaam_mlx.py::_infer_chunk`) и whisper (`engine.py::_transcribe_model`) —
     и ждёт поток A.

🔴 `RLock` реентерабелен ТОЛЬКО для своего потока: передача работы в пул ломает
реентерабельность. Для GigaAM-MLX это не просто задержка — его ожидание лока
ограничено 25 с, после чего он бросает `MLXLockTimeoutError` и уступает очередь,
то есть КАЖДАЯ запись пакета теряла 25 с и уезжала на резервный движок.

Отпускать лок безопасно: каждый MLX-путь внутри `engine.transcribe` берёт лок
сам — `_transcribe_model` (whisper + RU-finetune), GigaAM-MLX-адаптер, parakeet,
voxtral, `AudioLanguageID._run_detect`, `set_quality_profile` и пост-STT
`mx.clear_cache()`. Инвариант «любой MLX-инференс под локом» сохраняется;
снимается лишь дублирующий внешний захват.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bulk_reprocess import BulkReprocessor  # noqa: E402


def _make_item_dict(item_id: str, audio_path: str) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    return {
        "id": item_id,
        "ts": ts,
        "text": "Старый текст",
        "confidence": 0.4,
        "audio_path": audio_path,
        "is_protected": False,
        "source_lang": "ru",
    }


def _make_store_mock(items: list[dict]) -> MagicMock:
    from backend.models import HistoryItem
    store = MagicMock()
    store._load_active_items_unlocked = MagicMock(
        return_value=[HistoryItem.from_dict(d) for d in items]
    )
    store._lock = MagicMock(return_value=contextlib.nullcontext())
    store.update_history_item_text = MagicMock(return_value=True)
    return store


class BulkReprocessDoesNotHoldLockDuringTranscribeTests(unittest.TestCase):
    """Главный регресс: во время transcribe() лок обязан быть СВОБОДЕН."""

    def setUp(self) -> None:
        fd, self.audio_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.audio_path) and os.unlink(self.audio_path))

    def _run_reprocess(self, transcribe_side_effect) -> dict:
        store = _make_store_mock([_make_item_dict("id1", self.audio_path)])
        transcriber = MagicMock()
        transcriber.transcribe = MagicMock(side_effect=transcribe_side_effect)
        version_manager = MagicMock()
        version_manager.save_version = MagicMock(return_value={"version_num": 1})

        with patch.object(BulkReprocessor, "_load_audio", return_value="audio_array"):
            br = BulkReprocessor(
                store=store, transcriber=transcriber, version_manager=version_manager,
            )
            return br.reprocess(only_low_confidence=True, threshold=0.7)

    def test_lock_is_free_while_transcriber_works(self) -> None:
        """Проверяем так же, как ломалось в проде — захват ИЗ ДРУГОГО потока.

        Для своего потока RLock реентерабелен, и баг был бы невидим.
        """
        import core.mlx_lock as mlx_mod
        acquired_from_other_thread: list[bool] = []

        def _fake_transcribe(*_a, **_kw):
            # имитируем поток пула, в который движок отдаёт адаптер
            def _probe():
                lk = mlx_mod.mlx_lock()
                got = lk.acquire(timeout=2.0)
                acquired_from_other_thread.append(got)
                if got:
                    lk.release()

            th = threading.Thread(target=_probe)
            th.start()
            th.join(timeout=5.0)
            return {"text": "Новый текст", "confidence": 0.9}

        result = self._run_reprocess(_fake_transcribe)

        self.assertEqual(
            acquired_from_other_thread, [True],
            "поток пула не смог взять mlx_lock — bulk удерживает его во время "
            "transcribe и блокирует собственный инференс",
        )
        self.assertEqual(result["reprocessed"], 1)

    def test_lock_released_even_when_transcriber_raises(self) -> None:
        """Сбой движка на одной записи не должен оставить лок захваченным."""
        import core.mlx_lock as mlx_mod

        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        result = self._run_reprocess(_boom)
        self.assertEqual(result["reprocessed"], 0)

        lk = mlx_mod.mlx_lock()
        got = lk.acquire(timeout=2.0)
        if got:
            lk.release()
        self.assertTrue(got, "после исключения в движке лок остался захваченным")


class BulkReprocessMlxInvariantSourceContractTests(unittest.TestCase):
    """Инвариант, а не место захвата: MLX-путь сам обязан брать лок.

    Старые гарды (W1037 F2, W1635) закрепляли ИМЕННО внешний захват в
    bulk_reprocess.py — то есть тот самый дефект. Инвариант живёт ниже: в
    `_transcribe_model` и в MLX-адаптерах.
    """

    # 🔴 Разбор AST, а не подстроки: честный комментарий про mlx_lock не смеет
    # ронять корректную реализацию (правило проекта, W1618-класс).
    _LOCK_NAMES = {"mlx_lock", "mlx_inter_process_lock"}

    @classmethod
    def _tree(cls, rel: str):
        import ast
        return ast.parse((PROJECT_ROOT / rel).read_text(encoding="utf-8"))

    @staticmethod
    def _called_name(node) -> str | None:
        """Имя вызываемой функции для Call-узла (goo() и mod.goo())."""
        import ast
        if not isinstance(node, ast.Call):
            return None
        fn = node.func
        if isinstance(fn, ast.Name):
            return fn.id
        if isinstance(fn, ast.Attribute):
            return fn.attr
        return None

    @classmethod
    def _lock_calls(cls, tree) -> list[str]:
        import ast
        return [
            name
            for node in ast.walk(tree)
            if (name := cls._called_name(node)) in cls._LOCK_NAMES
        ]

    @classmethod
    def _find_function(cls, tree, name: str):
        import ast
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        return None

    def test_bulk_reprocess_does_not_call_any_mlx_lock(self) -> None:
        found = self._lock_calls(self._tree("backend/bulk_reprocess.py"))
        self.assertEqual(
            found, [],
            "bulk_reprocess снова захватывает GPU-локи вокруг transcribe "
            f"({found}) — это самоблокировка потока пула внутри "
            "engine.transcribe; межпроцессный flock самоблокируется так же "
            "(поток пула открывает НОВЫЙ fd на тот же файл)",
        )

    def test_whisper_inference_still_holds_both_locks(self) -> None:
        """Инвариант живёт ниже по стеку — в самом MLX-вызове, а не у вызывающего."""
        fn = self._find_function(self._tree("core/engine.py"), "_transcribe_model")
        self.assertIsNotNone(fn, "_transcribe_model исчез из engine.py")
        found = set(self._lock_calls(fn))
        self.assertEqual(
            found, self._LOCK_NAMES,
            "_transcribe_model потерял собственный захват — инвариант "
            "«любой MLX-инференс под локом» нарушен, и снятие внешнего "
            f"захвата в bulk_reprocess перестало быть безопасным (нашли: {found})",
        )

    def test_gigaam_mlx_adapter_still_takes_lock_itself(self) -> None:
        fn = self._find_function(
            self._tree("core/pipeline/stt_gigaam_mlx.py"), "_infer_chunk",
        )
        self.assertIsNotNone(fn, "_infer_chunk исчез из GigaAM-MLX адаптера")
        found = set(self._lock_calls(fn))
        self.assertEqual(
            found, self._LOCK_NAMES,
            f"GigaAM-MLX адаптер перестал брать локи сам (нашли: {found})",
        )


if __name__ == "__main__":
    unittest.main()
