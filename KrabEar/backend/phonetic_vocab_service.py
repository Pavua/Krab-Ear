"""Сервис IPC для управления фонетическим словарём пользователя.

Хранит записи вида {canonical, variants} в phonetic_vocab.json (в data_dir),
предоставляет три IPC-метода:
    add_phonetic_entry    — добавить/обновить запись (canonical + список вариантов)
    list_phonetic_entries — получить все записи
    remove_phonetic_entry — удалить по canonical

Формат файла: entries, updated_at и версия однократной curated-seed миграции.

Атомарная запись (tmp → replace), thread lock + bounded межпроцессный flock.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
import fcntl
import logging
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from core.parsing_utils import safe_json_loads

logger = logging.getLogger("KrabEar.Backend.PhoneticVocabService")

_PHONETIC_VOCAB_FILENAME = "phonetic_vocab.json"
_MAX_ENTRIES = 200
_MAX_VARIANT_LEN = 200
_MAX_CANONICAL_LEN = 200
_CURATED_SEED_VERSION = 1
_STORE_LOCK_TIMEOUT_SEC = 2.0

# Кураторский seed (W4, утверждён владельцем 17.09 + golden 2026-11-02):
# пары «вариант → канон». Первый запуск пишет файл целиком; существующий
# файл однократно получает недостающие варианты/новые каноны (не W4-удаления).
# Сознательно НЕ включены `maby` (легитимное EN "maybe") и `war`→`work`.
_DEFAULT_ENTRIES: list[dict] = [
    {"canonical": "openclaw", "variants": ["openclow", "open clow", "open clevel"]},
    {"canonical": "трамадол", "variants": ["травмадол", "траммадол", "тромадол", "рамадол"]},
    {"canonical": "прегабалин", "variants": ["пегабалин", "пегбалин", "прегалин", "пригаболин"]},
    {"canonical": "парацетамол", "variants": ["парацетамон", "парацемолом"]},
    {"canonical": "oxicodona", "variants": [
        "oxicodon", "oxycodona", "oxipodona", "oxigodona", "оxicodona",
        "psicodona", "o se podona",
    ]},
    {"canonical": "pregabalina", "variants": ["pregabalin", "pregabalino", "pregabalín", "pegabarina", "пregabalina"]},
    {"canonical": "paracetamol", "variants": ["fartestamol"]},
    {"canonical": "tramadol", "variants": ["ramadol", "tromadol", "dramadol", "tramodol"]},
    {"canonical": "whisper", "variants": ["висперед"]},
    {"canonical": "p0lrd", "variants": ["лрд", "lrd"]},
    {"canonical": "receta médica", "variants": ["Reset America"]},
]

# Каноны W4: в живой файл не возвращаем, если пользователь их удалил.
_ORIGINAL_W4_CANONICALS = frozenset(
    {
        "openclaw",
        "трамадол",
        "прегабалин",
        "парацетамол",
        "oxicodona",
        "pregabalina",
        "paracetamol",
        "tramadol",
        "whisper",
        "p0lrd",
    }
)
_MEDICAL_SEED_MARKERS = frozenset(
    {"openclaw", "oxicodona", "tramadol", "прегабалин", "pregabalina"}
)


def _default_entries() -> List[dict]:
    """Копия seed-записей (variants — новые списки, чтобы не делить мутабельное
    состояние между вызовами/инстансами)."""
    return [
        {"canonical": e["canonical"], "variants": list(e["variants"])}
        for e in _DEFAULT_ENTRIES
    ]


def _merge_curated_defaults(entries: List[dict]) -> bool:
    """Доливает недостающие варианты и новые каноны в уже существующий store.

    Не воскрешает удалённые W4-каноны. Новый канон (receta médica) добавляется
    только если файл уже похож на кураторский medical-seed.
    """
    changed = False
    by_canon = {
        str(entry.get("canonical", "")).casefold(): entry
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("canonical", "")).strip()
    }
    markers_present = bool(by_canon.keys() & _MEDICAL_SEED_MARKERS)
    for curated in _DEFAULT_ENTRIES:
        key = curated["canonical"].casefold()
        existing = by_canon.get(key)
        if existing is None:
            if markers_present and key not in _ORIGINAL_W4_CANONICALS:
                copied = {
                    "canonical": curated["canonical"],
                    "variants": list(curated["variants"]),
                }
                entries.append(copied)
                by_canon[key] = copied
                changed = True
            continue
        variants = existing.get("variants")
        if not isinstance(variants, list):
            existing["variants"] = list(curated["variants"])
            changed = True
            continue
        existing_lower = {
            str(item).casefold() for item in variants if isinstance(item, str)
        }
        for variant in curated["variants"]:
            if variant.casefold() not in existing_lower:
                variants.append(variant)
                existing_lower.add(variant.casefold())
                changed = True
    return changed


class PhoneticVocabService:
    """IPC-сервис для хранения и управления фонетическим словарём пользователя.

    Args:
        data_dir: директория для phonetic_vocab.json.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / _PHONETIC_VOCAB_FILENAME
        self._lock_path = data_dir / "phonetic_vocab.lock"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # После clear_all (privacy-purge) авто-seed в этом процессе подавлен,
        # чтобы удалённый пользователем словарь не «воскресал» при чтении.
        self._seed_suppressed = False

    # ── Storage ──────────────────────────────────────────────────────────────

    @contextmanager
    def _locked_store(self):
        """Одна read-modify-write секция для backend и REST; без fail-open."""
        with self._lock:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError("phonetic vocabulary lock must be an owned regular file")
                deadline = time.monotonic() + _STORE_LOCK_TIMEOUT_SEC
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("phonetic vocabulary store lock unavailable")
                        time.sleep(0.01)
                yield
            finally:
                # close освобождает flock. Пустой lock-файл не unlink: inode общий.
                os.close(fd)

    def _load_document(self) -> dict | None:
        """Читает store с метаданными; None при ошибке, без перезаписи данных."""
        if not self._path.exists():
            return {"entries": []}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("phonetic_vocab.json: не удалось прочитать: %s", exc)
            return None
        payload = safe_json_loads(raw, default=None, context="phonetic_vocab.json")
        if not isinstance(payload, dict):
            logger.warning("phonetic_vocab.json: неожиданный формат, возвращаем пустой список")
            return None
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            logger.warning("phonetic_vocab.json: поле 'entries' не список")
            return None
        payload["entries"] = [
            e for e in entries
            if isinstance(e, dict)
            and isinstance(e.get("canonical"), str) and e["canonical"].strip()
            and isinstance(e.get("variants"), list)
        ]
        return payload

    def _load(self) -> List[dict]:
        """Загружает список записей с диска. Пустой список при ошибке."""
        payload = self._load_document()
        return payload["entries"] if payload is not None else []

    def _save(self, entries: List[dict], *, seed_version: int | None = None) -> None:
        """Атомарная запись (tmp → replace) с сохранением seed/прочих метаданных."""
        payload = self._load_document()
        if payload is None:
            raise RuntimeError("phonetic_vocab.json: отказ перезаписывать непрочитанный store")
        payload.update({
            "entries": entries,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        if seed_version is not None:
            payload["curated_seed_version"] = seed_version
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            logger.error("phonetic_vocab.json: ошибка сохранения: %s", exc)
            raise

    # ── Public read API (called by engine.py via provider callback) ──────────

    def ensure_seeded(self) -> None:
        """Кладёт seed на первом запуске и однократно обновляет старый store.

        Существующий файл не затирается: пользовательские записи остаются,
        удалённые W4-каноны не воскрешаются, clear_all уважается.
        """
        with self._locked_store():
            self._ensure_seeded_locked()

    def _ensure_seeded_locked(self) -> None:
        """Вызывать под _lock; remove мигрирует до удаления в той же секции."""
        if self._seed_suppressed:
            return
        if not self._path.exists():
            self._save(_default_entries(), seed_version=_CURATED_SEED_VERSION)
            logger.info("PhoneticVocab: засеяно %d дефолтных записей", len(_DEFAULT_ENTRIES))
            return
        payload = self._load_document()
        if payload is None:
            return
        version = payload.get("curated_seed_version", 0)
        if type(version) is not int or version < 0:
            logger.warning("phonetic_vocab.json: неверная версия curated seed, миграция пропущена")
            return
        if version >= _CURATED_SEED_VERSION:
            return
        entries = payload["entries"]
        _merge_curated_defaults(entries)
        self._save(entries, seed_version=_CURATED_SEED_VERSION)
        logger.info("PhoneticVocab: однократная миграция curated seed завершена")

    def get_entries(self) -> List[dict]:
        """Возвращает текущий список записей (thread-safe).

        На первом запуске (файл отсутствует) предварительно засевает
        кураторские записи — см. ensure_seeded().
        """
        with self._locked_store():
            self._ensure_seeded_locked()
            return self._load()

    def clear_all(self) -> None:
        """Удаляет phonetic_vocab.json с диска (для privacy-purge).

        Идемпотентен — не бросает исключений если файл уже отсутствует.
        Подавляет авто-seed в текущем процессе (не воскрешаем словарь).
        """
        with self._locked_store():
            self._seed_suppressed = True
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "PhoneticVocabService.clear_all: не удалось удалить файл: %s", exc
                )

    # ── IPC handlers ─────────────────────────────────────────────────────────

    def handle_add_phonetic_entry(self, params: dict[str, Any]) -> dict:
        """IPC: add_phonetic_entry — добавить/обновить запись фонетического словаря.

        Params:
            canonical (str, обязательный): каноническое написание.
            variants  (list[str], обязательный): список неправильных вариантов.

        Returns:
            {"ok": True, "canonical": canonical, "variants": [str, ...]}
        """
        canonical = params.get("canonical", "")
        variants = params.get("variants")

        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("Параметр 'canonical' обязателен и не может быть пустым")
        canonical = canonical.strip()
        if len(canonical) > _MAX_CANONICAL_LEN:
            raise ValueError(
                f"Каноническое написание слишком длинное: {len(canonical)} символов "
                f"(максимум {_MAX_CANONICAL_LEN})"
            )
        if not isinstance(variants, list):
            raise ValueError("Параметр 'variants' обязателен и должен быть списком")
        if not variants:
            raise ValueError("Параметр 'variants' не может быть пустым списком")

        # Валидируем и нормализуем варианты
        clean_variants: List[str] = []
        for v in variants:
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"Вариант {v!r} невалиден: должен быть непустой строкой")
            if len(v) > _MAX_VARIANT_LEN:
                raise ValueError(
                    f"Вариант {v!r} слишком длинный: {len(v)} символов "
                    f"(максимум {_MAX_VARIANT_LEN})"
                )
            clean_variants.append(v.strip())

        with self._locked_store():
            entries = self._load()
            if len(entries) >= _MAX_ENTRIES and not any(
                e["canonical"].lower() == canonical.lower() for e in entries
            ):
                raise RuntimeError(
                    f"Достигнут лимит записей ({_MAX_ENTRIES}). "
                    "Удалите ненужные перед добавлением новых."
                )
            # Дедупликация по canonical (case-insensitive): обновляем существующий,
            # объединяя варианты (merge), дедупликация вариантов без учёта регистра.
            updated = False
            for entry in entries:
                if entry["canonical"].lower() == canonical.lower():
                    # Обновляем canonical (может меняться регистр) и мёрджим варианты
                    entry["canonical"] = canonical
                    existing_lower = {v.lower() for v in entry["variants"]}
                    for v in clean_variants:
                        if v.lower() not in existing_lower:
                            entry["variants"].append(v)
                            existing_lower.add(v.lower())
                    updated = True
                    clean_variants = entry["variants"]
                    break
            if not updated:
                entries.append({"canonical": canonical, "variants": clean_variants})
            self._save(entries)

        logger.info(
            "PhoneticVocab: %s canonical=%r variants=%r",
            "обновлена" if updated else "добавлена",
            canonical,
            clean_variants,
        )
        return {"ok": True, "canonical": canonical, "variants": clean_variants}

    def handle_list_phonetic_entries(self, params: dict[str, Any]) -> dict:
        """IPC: list_phonetic_entries — получить все записи.

        Returns:
            {"ok": True, "entries": [{"canonical": str, "variants": [str, ...]}, ...]}
        """
        entries = self.get_entries()
        return {"ok": True, "entries": entries}

    def handle_remove_phonetic_entry(self, params: dict[str, Any]) -> dict:
        """IPC: remove_phonetic_entry — удалить запись по canonical.

        Params:
            canonical (str, обязательный): каноническое написание для удаления.

        Returns:
            {"ok": True, "canonical": canonical, "removed": bool}
        """
        canonical = params.get("canonical", "")
        if not isinstance(canonical, str) or not canonical.strip():
            raise ValueError("Параметр 'canonical' обязателен")
        canonical = canonical.strip()

        with self._locked_store():
            if self._path.exists():
                self._ensure_seeded_locked()
            entries = self._load()
            before = len(entries)
            entries = [e for e in entries if e["canonical"].lower() != canonical.lower()]
            removed = len(entries) < before
            if removed:
                self._save(entries)

        if not removed:
            raise RuntimeError(f"Запись с canonical {canonical!r} не найдена")

        logger.info("PhoneticVocab: удалена canonical=%r", canonical)
        return {"ok": True, "canonical": canonical, "removed": True}
