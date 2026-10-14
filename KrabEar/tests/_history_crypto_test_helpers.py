"""Явный synthetic provider для setup тестов policy/locks, без Keychain."""
from unittest.mock import patch


def save_settings_with_synthetic_key(store, settings, **kwargs):
    """Настоящий commit; только acquisition получает постоянный fixture AES-key."""
    from backend.history_crypto import HistoryCrypto
    with patch("backend.history_crypto.build_history_crypto", return_value=HistoryCrypto(b"F" * 32)):
        return store.save_settings(settings, **kwargs)
