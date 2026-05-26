"""SpeakerManager — управление псевдонимами спикеров диаризации Krab Ear.

Позволяет назначить человекочитаемые имена идентификаторам спикеров
(например, SPEAKER_00 → «Паша»). Псевдонимы персистируются в
{data_dir}/speaker_aliases.json.

Voice Fingerprint (расширение):
  Дополнительно поддерживает cross-recording идентификацию спикеров через
  pyannote embedding модель (~512-dim cosine-similarity fingerprint).
  Фингерпринты хранятся в {data_dir}/speaker_fingerprints.json.
  Фича включается через settings.VOICE_FINGERPRINT_ENABLED (False по умолчанию).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

_log = logging.getLogger("KrabEar.Backend.SpeakerManager")

_SPEAKER_TAG_RE = re.compile(r"\[(SPEAKER_\d+)\]")

_EMBEDDING_DIM = 512


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Возвращает косинусное сходство двух 1-D векторов в диапазоне [-1, 1]."""
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class SpeakerManager:
    """Хранит псевдонимы спикеров и применяет их к тексту транскрипции.

    Voice Fingerprint extension:
      - compute_embedding(audio_segment, sample_rate) -> np.ndarray
      - find_matching_speaker(embedding, threshold) -> speaker_id | None
      - register_speaker(name, embedding) -> speaker_id
    """

    _FILENAME = "speaker_aliases.json"
    _FINGERPRINTS_FILENAME = "speaker_fingerprints.json"
    AUTO_REGISTER_MIN_CONFIDENCE: float = 0.50

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        settings_provider: Callable[[], dict] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._aliases: dict[str, str] = {}
        self._fingerprints: dict[str, list[float]] = {}
        self._auto_speaker_counter: int = 0
        self._embedding_model: Any = None
        # Optional callable returning current settings dict; used to check
        # privacy_mode_enabled before persisting biometric voice embeddings.
        self._settings_provider: Callable[[], dict] | None = settings_provider
        if data_dir is not None:
            self._path: Path | None = Path(data_dir) / self._FILENAME
            self._fingerprints_path: Path | None = (
                Path(data_dir) / self._FINGERPRINTS_FILENAME
            )
            self._load()
            self._load_fingerprints()
        else:
            self._path = None
            self._fingerprints_path = None

    def _is_privacy_mode(self) -> bool:
        """Возвращает True если privacy_mode_enabled активен в runtime settings."""
        if self._settings_provider is None:
            return False
        try:
            return bool(self._settings_provider().get("privacy_mode_enabled", False))
        except Exception:
            return False

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._aliases = {str(k): str(v) for k, v in data.items()}
        except Exception as exc:
            _log.warning("Не удалось загрузить псевдонимы спикеров: %s", exc)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._aliases, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            _log.warning("Не удалось сохранить псевдонимы спикеров: %s", exc)

    def _load_fingerprints(self) -> None:
        if self._fingerprints_path is None or not self._fingerprints_path.exists():
            return
        try:
            raw = json.loads(self._fingerprints_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                parsed: dict[str, list[float]] = {}
                counter = 0
                for k, v in raw.items():
                    if isinstance(v, list) and len(v) > 0:
                        parsed[str(k)] = [float(x) for x in v]
                        if str(k).startswith("Speaker_"):
                            try:
                                num = int(str(k).split("_", 1)[1])
                                counter = max(counter, num + 1)
                            except ValueError:
                                pass
                self._fingerprints = parsed
                self._auto_speaker_counter = counter
        except Exception as exc:
            _log.warning("Не удалось загрузить фингерпринты спикеров: %s", exc)

    def _save_fingerprints(self) -> None:
        if self._fingerprints_path is None:
            return
        if self._is_privacy_mode():
            _log.info(
                "speaker_manager: skipping fingerprint persist (privacy mode active)"
            )
            return
        try:
            self._fingerprints_path.parent.mkdir(parents=True, exist_ok=True)
            self._fingerprints_path.write_text(
                json.dumps(self._fingerprints, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            _log.warning("Не удалось сохранить фингерпринты спикеров: %s", exc)

    def set_alias(self, speaker_id: str, name: str) -> None:
        """Назначает псевдоним спикеру. Пустое имя — удаляет псевдоним."""
        speaker_id = speaker_id.strip()
        name = name.strip()
        with self._lock:
            if name:
                self._aliases[speaker_id] = name
            else:
                self._aliases.pop(speaker_id, None)
            self._save()

    def get_alias(self, speaker_id: str) -> str | None:
        with self._lock:
            return self._aliases.get(speaker_id.strip())

    def get_all_aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._aliases)

    def remove_alias(self, speaker_id: str) -> bool:
        speaker_id = speaker_id.strip()
        with self._lock:
            existed = speaker_id in self._aliases
            if existed:
                del self._aliases[speaker_id]
                self._save()
            return existed

    def apply_aliases(self, text: str) -> str:
        """Заменяет [SPEAKER_XX] на [ИмяСпикера] в тексте транскрипции."""
        with self._lock:
            aliases = dict(self._aliases)

        def _replace(m: re.Match) -> str:
            sid = m.group(1)
            name = aliases.get(sid)
            return f"[{name}]" if name else m.group(0)

        return _SPEAKER_TAG_RE.sub(_replace, text)

    def compute_embedding(
        self, audio_segment: np.ndarray, sample_rate: int = 16000
    ) -> np.ndarray:
        """Вычисляет voice embedding через pyannote embedding model."""
        if audio_segment is None or len(audio_segment) == 0:
            return np.zeros(_EMBEDDING_DIM, dtype=np.float32)
        waveform = audio_segment[np.newaxis, :] if audio_segment.ndim == 1 else audio_segment
        if waveform.shape[-1] < int(0.5 * sample_rate):
            return np.zeros(_EMBEDDING_DIM, dtype=np.float32)
        try:
            model = self._load_embedding_model()
            import torch  # noqa: PLC0415
            tensor = torch.tensor(waveform, dtype=torch.float32)
            with torch.no_grad():
                embedding = model({"waveform": tensor, "sample_rate": sample_rate})
            return embedding.numpy().flatten().astype(np.float32)
        except Exception as exc:
            _log.warning("compute_embedding: не удалось вычислить эмбеддинг: %s", exc)
            return np.zeros(_EMBEDDING_DIM, dtype=np.float32)

    def _load_embedding_model(self) -> Any:
        if self._embedding_model is not None:
            return self._embedding_model
        try:
            from pyannote.audio import Model, Inference  # type: ignore  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError("pyannote.audio не установлен") from exc
        try:
            from core.config import settings  # noqa: PLC0415
            hf_token = settings.HF_TOKEN or None
            kwargs: dict[str, Any] = {"token": hf_token} if hf_token else {}
            model = Model.from_pretrained("pyannote/embedding", **kwargs)
            self._embedding_model = Inference(model, window="whole")
            return self._embedding_model
        except Exception as exc:
            raise RuntimeError(f"Не удалось загрузить pyannote embedding model: {exc}") from exc

    def find_matching_speaker(self, embedding: np.ndarray, threshold: float = 0.75) -> str | None:
        """Ищет спикера с наибольшим cosine similarity. Returns None если нет match."""
        if embedding is None or np.linalg.norm(embedding) < 1e-10:
            return None
        with self._lock:
            fingerprints = dict(self._fingerprints)
        if not fingerprints:
            return None
        best_id: str | None = None
        best_score: float = -1.0
        for spk_id, fp_list in fingerprints.items():
            score = _cosine_similarity(embedding, np.array(fp_list, dtype=np.float32))
            if score > best_score:
                best_score = score
                best_id = spk_id
        return best_id if best_score >= threshold else None

    def register_speaker(self, name: str, embedding: np.ndarray) -> str:
        """Регистрирует нового спикера. Returns новый speaker_id."""
        if embedding is None:
            raise ValueError("embedding не может быть None")
        with self._lock:
            speaker_id = f"Speaker_{self._auto_speaker_counter}"
            self._auto_speaker_counter += 1
            name = name.strip()
            if name:
                self._aliases[speaker_id] = name
            self._fingerprints[speaker_id] = embedding.flatten().tolist()
            self._save()
            self._save_fingerprints()
        _log.info("register_speaker: %s name=%r", speaker_id, name or "<>")
        return speaker_id

    def update_fingerprint(self, speaker_id: str, embedding: np.ndarray, *, alpha: float = 0.1) -> bool:
        """Обновляет фингерпринт через EMA. Returns True если обновлён."""
        if embedding is None:
            return False
        with self._lock:
            if speaker_id not in self._fingerprints:
                return False
            old = np.array(self._fingerprints[speaker_id], dtype=np.float32)
            updated = (1.0 - alpha) * old + alpha * embedding.flatten().astype(np.float32)
            self._fingerprints[speaker_id] = updated.tolist()
            self._save_fingerprints()
        return True

    def delete_fingerprint(self, speaker_id: str) -> bool:
        """Удаляет фингерпринт. Returns True если существовал."""
        with self._lock:
            existed = speaker_id in self._fingerprints
            if existed:
                del self._fingerprints[speaker_id]
                self._save_fingerprints()
        return existed

    def get_all_fingerprints(self) -> dict[str, list[float]]:
        with self._lock:
            return {k: list(v) for k, v in self._fingerprints.items()}

    def resolve_speaker_for_segment(
        self, local_speaker_id: str, embedding: np.ndarray,
        threshold: float = 0.75, *, auto_register: bool = True
    ) -> str:
        """Разрешает глобальный speaker_id для сегмента диаризации."""
        if embedding is None or np.linalg.norm(embedding) < 1e-10:
            return local_speaker_id
        matched = self.find_matching_speaker(embedding, threshold=threshold)
        if matched is not None:
            return matched
        if auto_register:
            sid = self.register_speaker("", embedding)
            self.set_alias(sid, sid)
            return sid
        return local_speaker_id

    def handle_set_speaker_alias(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: set_speaker_alias."""
        speaker_id = str(params.get("speaker_id", "")).strip()
        name = str(params.get("name", "")).strip()
        if not speaker_id:
            raise ValueError("Параметр speaker_id обязателен")
        if not name:
            raise ValueError("Параметр name обязателен и не должен быть пустым")
        self.set_alias(speaker_id, name)
        return {"speaker_id": speaker_id, "name": name}

    def handle_get_speaker_aliases(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: get_speaker_aliases."""
        return {"aliases": self.get_all_aliases()}

    def handle_remove_speaker_alias(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: remove_speaker_alias."""
        speaker_id = str(params.get("speaker_id", "")).strip()
        if not speaker_id:
            raise ValueError("Параметр speaker_id обязателен")
        return {"speaker_id": speaker_id, "removed": self.remove_alias(speaker_id)}

    def handle_register_speaker(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: register_speaker."""
        name = str(params.get("name", "")).strip()
        emb_raw = params.get("embedding")
        if not isinstance(emb_raw, list) or len(emb_raw) == 0:
            raise ValueError("Параметр embedding обязателен (list[float])")
        sid = self.register_speaker(name, np.array(emb_raw, dtype=np.float32))
        return {"speaker_id": sid, "name": name}

    def handle_delete_speaker_fingerprint(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: delete_speaker_fingerprint."""
        speaker_id = str(params.get("speaker_id", "")).strip()
        if not speaker_id:
            raise ValueError("Параметр speaker_id обязателен")
        return {"speaker_id": speaker_id, "deleted": self.delete_fingerprint(speaker_id)}

    def handle_list_speaker_fingerprints(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC: list_speaker_fingerprints."""
        fps = self.get_all_fingerprints()
        aliases = self.get_all_aliases()
        return {
            "speakers": [
                {"speaker_id": sid, "name": aliases.get(sid, sid), "embedding_dim": len(fp)}
                for sid, fp in fps.items()
            ],
            "count": len(fps),
        }
