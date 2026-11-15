"""Fail-closed границы раннего recovery, без запуска backend."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from backend.history_service import HistoryService
from backend.timeline_purge_guard import timeline_guard


def test_verification_stat_failure_is_not_absence(tmp_path):
    history = HistoryService.__new__(HistoryService)
    history._semantic_searcher = object()
    history._vocabulary_store = None
    history._webhook_manager = None
    history._translation_cache = None
    errors = []
    with patch('backend.history_service.os.lstat', side_effect=PermissionError('synthetic')):
        history._purge_verify_cleanup(errors, tmp_path)
    assert errors == ['purge_verification']


def test_recovery_failure_precedes_runtime_initialization(tmp_path):
    from backend.service import BackendService
    store = SimpleNamespace(data_dir=tmp_path)
    # Нерегулярный marker: восстановление не может подтвердить безопасный intent.
    (tmp_path / '.purge-pending').mkdir()
    initializer = Mock()
    with patch.object(BackendService, '_init_local_text_stores', initializer):
        with pytest.raises((OSError, RuntimeError)):
            BackendService(store)
    initializer.assert_not_called()
    assert timeline_guard(store).recovery_pending()


def test_build_service_recovers_before_policy_or_compaction(tmp_path):
    from backend.service import build_service
    store = Mock()
    store.data_dir = tmp_path
    with patch('backend.service.StateStore', return_value=store), \
            patch('backend.purge_recovery.recover_pending_purge', side_effect=RuntimeError('synthetic')):
        with pytest.raises(RuntimeError):
            build_service(tmp_path)
    store.initialize_startup_plaintext_policy.assert_not_called()
    store.maybe_compact.assert_not_called()
