"""Явная инъекция реального authorizer в старые synthetic export fixtures."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.history_service import HistoryService
from backend.plaintext_export_authorization import (
    PlaintextExportAuthorizer, PolicyFingerprint, PolicySnapshot, PolicyState,
)
from backend.state_store import StateStore


def history_service_with_off_policy(store: StateStore, **kwargs) -> HistoryService:
    """Инициализирует только отсутствующую policy synthetic-профиля, сохраняя существующую."""
    from backend.history_service import HistoryService

    if not isinstance(store, StateStore):
        raise TypeError("Для fake store передавайте authorizer явно")
    if not store.settings_path.exists():
        store.save_settings(
            {"history_encryption_enabled": False, "privacy_mode_enabled": False},
            validated_repair=True,
        )
    authorizer = PlaintextExportAuthorizer(
        read_snapshot=store.read_plaintext_policy_snapshot,
        profile_identity=str(store.data_dir.resolve()),
    )
    return HistoryService(store=store, plaintext_export_authorizer=authorizer, **kwargs)


def off_authorizer(*, privacy_provider=None) -> PlaintextExportAuthorizer:
    """Typed OFF только для явно подключённых legacy fake-store fixtures."""
    identity = "/synthetic-plaintext-export-fixture"
    revision = "1234567890abcdef1234567890abcdef"
    fingerprint = PolicyFingerprint(
        profile_identity=identity, st_dev=1, st_ino=1, st_size=1,
        st_mtime_ns=1, st_ctime_ns=1, content_sha256="a" * 64,
        internal_revision=revision,
    )

    def snapshot():
        privacy = False if privacy_provider is None else privacy_provider()
        return PolicySnapshot(
            state=PolicyState.KNOWN_OFF, privacy_mode_enabled=privacy,
            internal_revision=revision, fingerprint=fingerprint, reason=None,
        )

    return PlaintextExportAuthorizer(read_snapshot=snapshot, profile_identity=identity)
