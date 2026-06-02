"""Менеджер кэша ML-моделей Krab Ear.

Сканирует ~/.cache/huggingface/hub/ и предоставляет информацию
о загруженных моделях и занимаемом дисковом пространстве.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class ModelInfo:
    """Информация об одной кэшированной модели HuggingFace."""

    name: str
    """Имя модели в формате 'owner/repo' или оригинальное имя папки."""

    size_mb: float
    """Размер модели на диске в мегабайтах."""

    last_accessed: Optional[str]
    """ISO 8601 timestamp последнего доступа к папке (или None)."""

    cache_path: str
    """Абсолютный путь к папке модели в кэше."""

    def to_dict(self) -> dict:
        return asdict(self)


class ModelCacheManager:
    """Управление кэшем ML-моделей HuggingFace Hub.

    По умолчанию сканирует ~/.cache/huggingface/hub/.
    Путь можно переопределить через конструктор (для тестов).
    """

    _HF_CACHE_DIR = Path.home() / ".cache" / "huggingface" / "hub"

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        size_limit_mb: Optional[float] = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir is not None else self._HF_CACHE_DIR
        self._size_limit_mb = size_limit_mb

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def get_cache_path(self) -> str:
        """Возвращает путь к директории кэша HuggingFace Hub."""
        return str(self._cache_dir)

    def is_model_cached(self, model_name: str) -> bool:
        """Проверяет, присутствует ли модель в локальном кэше.

        Args:
            model_name: Имя репозитория в формате 'owner/repo'
                        или точное имя папки 'models--owner--repo'.
        """
        folder = self._model_folder_name(model_name)
        return (self._cache_dir / folder).exists()

    def list_cached_models(self) -> list[ModelInfo]:
        """Возвращает список всех кэшированных моделей.

        Сканирует папки ``models--*`` в директории кэша.
        """
        if not self._cache_dir.exists():
            return []

        models: list[ModelInfo] = []
        for entry in sorted(self._cache_dir.iterdir()):
            if not entry.is_dir():
                continue
            if not entry.name.startswith("models--"):
                continue
            info = self._build_model_info(entry)
            models.append(info)
        return models

    def get_cache_size(self) -> int:
        """Возвращает суммарный размер всех кэшированных моделей в байтах."""
        total = 0
        if not self._cache_dir.exists():
            return 0
        for entry in self._cache_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("models--"):
                total += int(self._folder_size_mb(entry) * 1024 * 1024)
        return total

    def get_cache_size_total(self) -> float:
        """Возвращает суммарный размер всех кэшированных моделей в МБ."""
        models = self.list_cached_models()
        return sum(m.size_mb for m in models)

    def evict(self, model_id: str) -> bool:
        """Удаляет модель из кэша.

        Args:
            model_id: Имя модели 'owner/repo' или точное имя папки.

        Returns:
            True если ЭТОТ вызов удалил директорию;
            False если модель не найдена ИЛИ параллельный вызов успел удалить её первым (гонка).
        """
        import shutil

        folder = self._model_folder_name(model_id)
        model_path = self._cache_dir / folder
        if model_path.exists():
            try:
                shutil.rmtree(model_path)
                return True
            except FileNotFoundError:
                return False  # Уже удалено параллельным вызовом
        return False

    def is_over_size_limit(self) -> bool:
        """Возвращает True, если кэш превышает установленный лимит (size_limit_mb).

        Если лимит не задан — всегда False.
        """
        if self._size_limit_mb is None:
            return False
        return self.get_cache_size_total() > self._size_limit_mb

    def enforce_size_limit(self) -> list[str]:
        """Удаляет модели (по порядку последнего доступа) пока не уложимся в лимит.

        Returns:
            Список имён удалённых моделей.
        """
        if self._size_limit_mb is None or not self.is_over_size_limit():
            return []
        models = sorted(
            self.list_cached_models(),
            key=lambda m: m.last_accessed or "",
        )
        evicted = []
        for m in models:
            if not self.is_over_size_limit():
                break
            self.evict(m.name)
            evicted.append(m.name)
        return evicted

    def get_cache_info(self) -> dict:
        """Возвращает сводку по кэшу: путь, число моделей, суммарный размер."""
        models = self.list_cached_models()
        return {
            "cache_path": self.get_cache_path(),
            "model_count": len(models),
            "total_size_mb": round(sum(m.size_mb for m in models), 2),
            "models": [m.to_dict() for m in models],
        }

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _model_folder_name(model_name: str) -> str:
        """Преобразует имя репозитория в имя папки кэша.

        'owner/repo' → 'models--owner--repo'
        'models--owner--repo' → возвращается как есть
        """
        if model_name.startswith("models--"):
            return model_name
        return "models--" + model_name.replace("/", "--")

    @staticmethod
    def _folder_size_mb(path: Path) -> float:
        """Рекурсивно подсчитывает размер папки в МБ.

        Симлинки пропускаются: кэш HuggingFace Hub хранит каждый blob один раз
        под ``models--*/blobs/<sha>`` и ссылается на него из
        ``models--*/snapshots/<rev>/<file>`` относительным симлинком. Если
        следовать симлинку (``stat()`` по умолчанию), один и тот же blob
        суммируется дважды (≈2× завышение размера). Считаем только реальные
        файлы (``follow_symlinks=False`` + ``followlinks=False``), чтобы каждый
        blob учитывался ровно один раз.
        """
        total = 0
        try:
            for root, _dirs, files in os.walk(path, followlinks=False):
                for fname in files:
                    fpath = Path(root) / fname
                    try:
                        st = fpath.stat(follow_symlinks=False)
                        # Пропускаем симлинки (snapshot → blob): blob уже посчитан напрямую.
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        total += st.st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total / (1024 * 1024)

    @staticmethod
    def _last_accessed_iso(path: Path) -> Optional[str]:
        """Возвращает время последнего доступа к папке в формате ISO 8601."""
        try:
            atime = path.stat().st_atime
            dt = datetime.fromtimestamp(atime, tz=timezone.utc)
            return dt.isoformat()
        except OSError:
            return None

    @staticmethod
    def _folder_to_model_name(folder_name: str) -> str:
        """'models--owner--repo' → 'owner/repo'."""
        without_prefix = folder_name.removeprefix("models--")
        # Только первый '--' разделяет owner и repo name
        return without_prefix.replace("--", "/", 1)

    def _build_model_info(self, entry: Path) -> ModelInfo:
        name = self._folder_to_model_name(entry.name)
        size_mb = round(self._folder_size_mb(entry), 2)
        last_accessed = self._last_accessed_iso(entry)
        return ModelInfo(
            name=name,
            size_mb=size_mb,
            last_accessed=last_accessed,
            cache_path=str(entry),
        )

    # ------------------------------------------------------------------
    # IPC handlers (формат: принимают params dict, возвращают result dict)
    # ------------------------------------------------------------------

    def handle_list_cached_models(self, params: dict) -> dict:
        """IPC handler: list_cached_models."""
        models = self.list_cached_models()
        return {
            "models": [m.to_dict() for m in models],
            "count": len(models),
        }

    def handle_get_model_cache_info(self, params: dict) -> dict:
        """IPC handler: get_model_cache_info."""
        return self.get_cache_info()
