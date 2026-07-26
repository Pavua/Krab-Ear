"""Continuous spill сырого аудио записи на диск (R1 Фаза 1).

Во время записи AudioRecorder дописывает каждый чанк в
``<data_dir>/rescue/<session_id>.f32.part`` (+ JSON-сайдкар с параметрами).
При любой смерти процесса аудио уже на диске; восстановление —
``backend/recording_rescue.py``. Ошибки диска НИКОГДА не роняют запись:
писатель самоотключается с одним WARN (fail-open в сторону диктовки).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger("KrabEar.Backend.RecordingSpill")

# Короче этого восстановленный файл считается мусором (щелчок старта записи).
_MIN_RESCUE_SEC = 0.5
_BYTES_PER_SAMPLE = 4  # float32
_SAFE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class RecordingSpillWriter:
    """Односессионный append-only писатель spill-файла.

    Потоко-дисциплина: append() зовёт ТОЛЬКО worker-тред AudioRecorder
    (строго вне recorder._lock); open/close/discard — lifecycle-код под
    recorder._lifecycle_lock. Собственный лок не нужен.
    """

    def __init__(
        self,
        rescue_dir: Path,
        sample_rate: int,
        channels: int,
        source: str = "unknown",
        session_id: str | None = None,
    ) -> None:
        # R2 F7: generation token и rescue-файл используют одну идентичность.
        # Значение остаётся basename, а не произвольным путём: этот API теперь
        # принимает внешний id и не должен позволять выйти из rescue_dir.
        resolved_session_id = (
            str(session_id)
            if session_id is not None and str(session_id)
            else uuid.uuid4().hex
        )
        if _SAFE_SESSION_ID_RE.fullmatch(resolved_session_id) is None:
            raise ValueError("Некорректный session_id spill-файла")
        self.session_id = resolved_session_id
        self._rescue_dir = Path(rescue_dir)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.source = str(source)
        self.part_path = self._rescue_dir / f"{self.session_id}.f32.part"
        self._meta_path = self._rescue_dir / f"{self.session_id}.meta.json"
        self._fh = None
        # Право удаления возникает только после успешной эксклюзивной
        # резервации meta+part. Совпавший путь сам по себе не даёт ownership.
        self._owns_paths = False
        self.failed = False

    def open(self) -> bool:
        meta_created = False
        try:
            self._rescue_dir.mkdir(parents=True, exist_ok=True)
            meta_fh = self._meta_path.open("x", encoding="utf-8")
            meta_created = True
            with meta_fh:
                json.dump(
                    {
                        "sample_rate": self.sample_rate,
                        "channels": self.channels,
                        "source": self.source,
                        "started_at_iso": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    },
                    meta_fh,
                    ensure_ascii=False,
                )
            self._fh = self.part_path.open("xb")
            self._owns_paths = True
            return True
        except Exception:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            # Удаляем только sidecar, созданный ЭТОЙ попыткой. При коллизии
            # существующие meta/part принадлежат первому writer-у.
            if meta_created:
                try:
                    self._meta_path.unlink(missing_ok=True)
                except Exception:
                    pass
            self.failed = True
            logger.warning("RecordingSpill: open() провалился — spill выключен "
                           "для этой записи", exc_info=True)
            return False

    def append(self, chunk: "np.ndarray") -> None:
        if self.failed or self._fh is None:
            return
        try:
            self._fh.write(np.ascontiguousarray(
                chunk.reshape(-1), dtype=np.float32).tobytes())
            # Python-буфер не переживает kill -9; flush → данные в page cache ОС,
            # который переживает смерть процесса. Один syscall на 0.1с-чанк.
            self._fh.flush()
        except Exception:
            self.failed = True
            logger.warning("RecordingSpill: ошибка дозаписи — spill выключен "
                           "для этой записи", exc_info=True)

    def close(self) -> None:
        """Закрыть fd; файлы ОСТАВИТЬ (главный сценарий спасения)."""
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                logger.debug("RecordingSpill: close() error", exc_info=True)
            self._fh = None

    def discard(self) -> None:
        """close + удалить файлы. Идемпотентно; зовётся после персиста в history."""
        self.close()
        if not self._owns_paths:
            return
        all_removed = True
        for p in (self.part_path, self._meta_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                all_removed = False
                logger.debug("RecordingSpill: discard unlink error", exc_info=True)
        # При частичной I/O-ошибке сохраняем право на retry. Пока хотя бы один
        # файл существует, новый writer всё равно не зарезервирует тот же token.
        # После полного успеха старый объект больше не вправе трогать этот путь.
        if all_removed:
            self._owns_paths = False


def finalize_part_to_wav(part_path: Path) -> Path | None:
    """Собрать ``<id>.rescued.wav`` из ``<id>.f32.part`` + сайдкара.

    Возвращает путь к WAV или None (ошибка / слишком коротко). Успех и
    «слишком коротко» удаляют исходные файлы; отсутствие сайдкара оставляет
    всё как есть (не знаем формат — не наш мусор).
    """
    part_path = Path(part_path)
    meta_path = part_path.with_name(part_path.name.replace(".f32.part", ".meta.json"))
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        sample_rate = int(meta["sample_rate"])
        channels = int(meta["channels"])
    except Exception:
        logger.warning("RecordingSpill: finalize без сайдкара — пропуск %s",
                       part_path.name, exc_info=True)
        return None

    def _cleanup() -> None:
        for p in (part_path, meta_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    try:
        raw = part_path.read_bytes()
        usable = len(raw) - (len(raw) % _BYTES_PER_SAMPLE)
        samples = np.frombuffer(raw[:usable], dtype=np.float32)
        if samples.size < _MIN_RESCUE_SEC * sample_rate * channels:
            logger.info("RecordingSpill: %s короче %.1fс — удаляю как мусор",
                        part_path.name, _MIN_RESCUE_SEC)
            _cleanup()
            return None
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        wav_path = part_path.with_name(
            part_path.name.replace(".f32.part", ".rescued.wav"))
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16.tobytes())
        _cleanup()
        return wav_path
    except Exception:
        logger.warning("RecordingSpill: finalize %s провалился",
                       part_path.name, exc_info=True)
        return None
