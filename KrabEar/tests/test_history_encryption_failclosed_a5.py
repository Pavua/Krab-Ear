"""A5: новые записи не должны обходить включённое шифрование при сбое ключа."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.state_store import (
    HistoryEncryptionUnavailable,
    StateStore,
    StateStoreSettingsCorruptError,
)
from backend.history_crypto import HistoryCrypto


def test_enabled_encryption_without_key_rejects_new_history_line() -> None:
    """Ломается, если путь append вновь принимает crypto=None как plaintext."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        (data_dir / "settings.json").write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        store = StateStore(data_dir)
        with patch("backend.history_crypto.build_history_crypto", return_value=None):
            with pytest.raises(HistoryEncryptionUnavailable):
                store.add_history_item(text="SYNTHETIC_A5_MARKER")

        history_path = data_dir / "history.ndjson"
        assert not history_path.exists() or "SYNTHETIC_A5_MARKER" not in history_path.read_text(
            encoding="utf-8"
        )


def test_encrypt_error_rejects_new_history_line() -> None:
    """Ломается, если исключение AES-GCM вновь превращается в plaintext fallback."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        (data_dir / "settings.json").write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        store = StateStore(data_dir)
        crypto = HistoryCrypto(os.urandom(32))
        store._history_crypto_initialized = True
        store._history_crypto_instance = crypto
        with patch.object(crypto, "encrypt_line", side_effect=RuntimeError("synthetic crypto failure")):
            with pytest.raises(HistoryEncryptionUnavailable):
                store.add_history_item(text="SYNTHETIC_AES_FAILURE")

        history_path = data_dir / "history.ndjson"
        assert not history_path.exists() or "SYNTHETIC_AES_FAILURE" not in history_path.read_text(
            encoding="utf-8"
        )


def test_enabling_encryption_invalidates_plaintext_writer_cache() -> None:
    """Ломается, если первый plaintext append кэширует crypto=None навсегда."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        store = StateStore(data_dir)
        store.add_history_item(text="SYNTHETIC_BEFORE_ENABLE")
        store.save_settings({"history_encryption_enabled": True})

        crypto = HistoryCrypto(os.urandom(32))
        with patch("backend.history_crypto.build_history_crypto", return_value=crypto):
            store.add_history_item(text="SYNTHETIC_AFTER_ENABLE")

        lines = (data_dir / "history.ndjson").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("{")
        assert lines[1].startswith("ENC1:")


def test_unrelated_settings_save_cannot_turn_corruption_into_encryption_off() -> None:
    """Ломается, если save_settings затирает битый файл дефолтом OFF."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        settings_path = data_dir / "settings.json"
        settings_path.write_text("{synthetic-corruption", encoding="utf-8")
        store = StateStore(data_dir)
        recovered_defaults = store.load_settings()

        with pytest.raises(StateStoreSettingsCorruptError):
            store.save_settings({**recovered_defaults, "stt_language": "es"})

        assert settings_path.read_text(encoding="utf-8") == "{synthetic-corruption"


def test_wrong_key_cannot_compact_away_encrypted_history() -> None:
    """Ломается, если недешифрованная ENC1-строка тихо пропадает из compaction."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        writer = StateStore(data_dir)
        writer._history_crypto_initialized = True
        writer._history_crypto_instance = HistoryCrypto(b"A" * 32)
        writer.add_history_item(text="SYNTHETIC_ENCRYPTED_FOR_COMPACT")
        history_path = data_dir / "history.ndjson"
        before = history_path.read_bytes()

        wrong_key_store = StateStore(data_dir)
        wrong_key_store._history_crypto_initialized = True
        wrong_key_store._history_crypto_instance = HistoryCrypto(b"B" * 32)
        with pytest.raises(HistoryEncryptionUnavailable):
            wrong_key_store.compact()

        assert history_path.read_bytes() == before


def test_missing_settings_with_existing_enc1_never_appends_plaintext() -> None:
    """Ломается, если потеря settings превращает зашифрованный профиль в OFF."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        crypto = HistoryCrypto(b"C" * 32)
        settings_path = data_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        writer = StateStore(data_dir)
        writer._history_crypto_initialized = True
        writer._history_crypto_instance = crypto
        writer.add_history_item(text="SYNTHETIC_FIRST_ENCRYPTED")
        settings_path.unlink()

        fresh_store = StateStore(data_dir)
        with patch("backend.history_crypto.build_history_crypto", return_value=crypto):
            fresh_store.add_history_item(text="SYNTHETIC_AFTER_SETTINGS_LOSS")

        lines = (data_dir / "history.ndjson").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert all(line.startswith("ENC1:") for line in lines)


def test_non_boolean_encryption_flag_does_not_mean_off() -> None:
    """Ломается, если null в настройках приводится к False и пишет plaintext."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        (data_dir / "settings.json").write_text(
            json.dumps({"history_encryption_enabled": None}), encoding="utf-8"
        )
        store = StateStore(data_dir)
        with patch("backend.history_crypto.build_history_crypto", return_value=None):
            with pytest.raises(HistoryEncryptionUnavailable):
                store.add_history_item(text="SYNTHETIC_BAD_FLAG")

        history_path = data_dir / "history.ndjson"
        assert not history_path.exists() or "SYNTHETIC_BAD_FLAG" not in history_path.read_text(
            encoding="utf-8"
        )


def test_settings_save_cannot_normalize_invalid_encryption_flag_to_off() -> None:
    """Ломается, если null-флаг из старого файла сохраняется как новый OFF."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        settings_path = data_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"history_encryption_enabled": None}), encoding="utf-8"
        )
        store = StateStore(data_dir)
        with pytest.raises(StateStoreSettingsCorruptError):
            store.save_settings({"history_encryption_enabled": False})
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "history_encryption_enabled": None
        }


def test_missing_settings_cannot_be_recreated_off_beside_enc1_history() -> None:
    """Ломается, если настройка OFF записывается после потери settings."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        settings_path = data_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        writer = StateStore(data_dir)
        writer._history_crypto_initialized = True
        writer._history_crypto_instance = HistoryCrypto(b"D" * 32)
        writer.add_history_item(text="SYNTHETIC_BEFORE_SETTINGS_LOSS")
        settings_path.unlink()

        fresh_store = StateStore(data_dir)
        recovered_defaults = fresh_store.load_settings()
        with pytest.raises(StateStoreSettingsCorruptError):
            fresh_store.save_settings(recovered_defaults)
        assert not settings_path.exists()


def test_disabling_flag_cannot_strand_existing_enc1_history() -> None:
    """Ломается, если OFF записан до способа расшифровать старые строки."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        settings_path = data_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        store = StateStore(data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = HistoryCrypto(b"E" * 32)
        store.add_history_item(text="SYNTHETIC_BEFORE_DISABLE")

        with pytest.raises(StateStoreSettingsCorruptError):
            store.save_settings({"history_encryption_enabled": False})
        assert json.loads(settings_path.read_text(encoding="utf-8")) == {
            "history_encryption_enabled": True
        }


def test_missing_flag_in_existing_settings_cannot_disable_enc1_history() -> None:
    """Ломается, если {} трактуется как OFF рядом с ENC1-журналом."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        settings_path = data_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        crypto = HistoryCrypto(b"F" * 32)
        writer = StateStore(data_dir)
        writer._history_crypto_initialized = True
        writer._history_crypto_instance = crypto
        writer.add_history_item(text="SYNTHETIC_BEFORE_FLAG_LOSS")
        settings_path.write_text("{}", encoding="utf-8")

        fresh_store = StateStore(data_dir)
        with patch("backend.history_crypto.build_history_crypto", return_value=crypto):
            fresh_store.add_history_item(text="SYNTHETIC_AFTER_FLAG_LOSS")
        lines = (data_dir / "history.ndjson").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert all(line.startswith("ENC1:") for line in lines)


def test_read_recovers_when_temporarily_missing_key_returns() -> None:
    """Ломается, если read-path навсегда кэширует crypto=None после отказа."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        (data_dir / "settings.json").write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        crypto = HistoryCrypto(b"G" * 32)
        writer = StateStore(data_dir)
        writer._history_crypto_initialized = True
        writer._history_crypto_instance = crypto
        writer.add_history_item(text="SYNTHETIC_KEY_RECOVERY")

        reader = StateStore(data_dir)
        with patch("backend.history_crypto.build_history_crypto", return_value=None):
            with pytest.raises(HistoryEncryptionUnavailable):
                reader.get_history_page(None, 100)
        with patch("backend.history_crypto.build_history_crypto", return_value=crypto):
            items, _ = reader.get_history_page(None, 100)
        assert [item["text"] for item in items] == ["SYNTHETIC_KEY_RECOVERY"]


def test_disabling_before_first_encrypted_line_clears_cached_crypto() -> None:
    """Ломается, если явный OFF всё ещё пишет ENC1 из старого crypto-кэша."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)
        settings_path = data_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"history_encryption_enabled": True}), encoding="utf-8"
        )
        store = StateStore(data_dir)
        crypto = HistoryCrypto(b"H" * 32)
        with patch("backend.history_crypto.build_history_crypto", return_value=crypto):
            assert store._get_history_crypto() is crypto
        store.save_settings({"history_encryption_enabled": False})

        store.add_history_item(text="SYNTHETIC_AFTER_SAFE_DISABLE")
        line = (data_dir / "history.ndjson").read_text(encoding="utf-8").strip()
        assert line.startswith("{")
