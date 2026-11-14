"""Возобновление подтверждённого privacy-purge до запуска runtime-служб."""
from contextlib import ExitStack
from pathlib import Path

from backend.timeline_purge_guard import timeline_guard


def recover_pending_purge(store):
    """Повторяет существующие wipe-шаги; при ошибке обычный startup запрещён.

    В новом процессе нет RAM runtime: jobs/recording/live subs не создаём.
    Файловые stores без fallback обязательно подключены. Никаких start(),
    подписок EventBus, аудио, загрузки моделей или сетевых вызовов.
    """
    guard = timeline_guard(store)
    if not guard.recovery_pending():
        return

    def recover():
        from backend.archive_manager import ArchiveManager
        from backend.bookmarks import BookmarkManager
        from backend.call_session_store import CallSessionStore
        from backend.history_service import HistoryService
        from backend.obsidian_sync import ObsidianSyncManager
        from backend.playback_tracker import PlaybackTracker
        from backend.recording_chain import RecordingChainManager
        from backend.semantic_search import SemanticSearcher
        from backend.settings_backup import SettingsBackup
        from backend.speaker_manager import SpeakerManager
        from backend.transcript_versioning import TranscriptVersionManager
        from backend.translation_cache import TranslationCache
        from backend.vocabulary_store import VocabularyStore
        from backend.webhook_manager import WebhookManager

        directory = Path(store.data_dir)
        with ExitStack() as resources:
            history = HistoryService(store)
            history._transcript_versions = TranscriptVersionManager(directory)
            history._recording_chain_mgr = RecordingChainManager(store)
            history._archive_manager = ArchiveManager(store)
            history._bookmarks = BookmarkManager(directory)
            history._call_session_store = CallSessionStore(directory)
            history._speaker_manager = SpeakerManager(directory, store=store)
            history._playback_tracker = PlaybackTracker(directory)
            history._webhook_manager = WebhookManager(directory)
            resources.callback(history._webhook_manager.shutdown)
            history._obsidian_sync = ObsidianSyncManager(directory, event_bus=None)
            history._translation_cache = TranslationCache(str(directory))
            history._vocabulary_store = VocabularyStore(directory)
            history._settings_backup = SettingsBackup()
            history._semantic_searcher = SemanticSearcher(directory, enabled=False)
            return history.handle_purge_all_data({"confirm": True})

    guard.run_purge(recover, only_pending=True)
    if guard.recovery_pending():
        # Не логируем content/ключи, не запускаем часть runtime после partial wipe.
        raise RuntimeError("purge_recovery_required")
