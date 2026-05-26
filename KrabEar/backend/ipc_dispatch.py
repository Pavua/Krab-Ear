"""Таблица диспетчеризации IPC-методов для BackendService.

Выделено из ``backend/service.py`` в рамках W797 phase 3 (W828).
``BackendService.handle_request`` остаётся в ``service.py``; эта функция
строит таблицу ``{method: handler}`` и кэширует её на экземпляре.

Использование::

    from backend.ipc_dispatch import build_dispatch_table

    handlers = build_dispatch_table(self)   # self = BackendService instance
    handler = handlers.get(method)

"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from backend.service import BackendService


def build_dispatch_table(
    svc: "BackendService",
) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """Строит и возвращает полную таблицу IPC-обработчиков.

    Вызывается один раз при инициализации BackendService и сохраняется
    в ``svc._dispatch_table``.  Все записи — bound-методы или лямбды,
    захватывающие ``svc``; перестройка при изменении атрибутов не нужна
    (сервисы стабильны после ``__init__``).
    """
    return {
        "ping": svc._handle_ping,  # VERIFIED: called from Swift (BackendSupervisor)
        "start_recording": svc._handle_start_recording,  # VERIFIED: called from Swift (main)
        "stop_recording": svc._handle_stop_recording,  # VERIFIED: called from Swift (main)
        "get_recording_state": svc._handle_get_recording_state,  # VERIFIED: called from Swift (main, HistoryPanel)
        "start_call_assist": svc._call_assist.handle_start,  # VERIFIED: called from Swift (HistoryPanel)
        "stop_call_assist": svc._call_assist.handle_stop,  # VERIFIED: called from Swift (HistoryPanel)
        "get_call_assist_state": svc._call_assist.handle_get_state,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_diagnostics": svc._call_assist.handle_diagnostics,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_summary": svc._call_assist.handle_summary,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_quick_phrase": svc._call_assist.handle_quick_phrase,  # VERIFIED: called from Swift (HistoryPanel)
        "list_call_assist_quick_phrases": svc._call_assist.handle_list_quick_phrases,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_cost_estimate": svc._call_assist.handle_cost_estimate,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_timeline": svc._call_assist.handle_timeline,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_timeline_stats": svc._call_assist.handle_timeline_stats,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_timeline_summary": svc._call_assist.handle_timeline_summary,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_timeline_export": svc._call_assist.handle_timeline_export,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_timeline_clear": svc._call_assist.handle_timeline_clear,  # VERIFIED: called from Swift (HistoryPanel)
        "call_assist_timeline_to_history": svc._call_assist.handle_timeline_to_history,  # VERIFIED: called from Swift (HistoryPanel)
        "list_audio_inputs": svc._handle_list_audio_inputs,  # VERIFIED: called from Swift (HistoryPanel)
        "get_history_page": svc._history.handle_get_history_page,  # VERIFIED: called from Swift (HistoryPanel)
        "search_history": svc._history.handle_search_history,  # VERIFIED: called from Swift (HistoryPanel)
        "fuzzy_search": svc._history.handle_fuzzy_search,  # нечёткий поиск по истории транскрипций
        "search_with_highlights": svc._history.handle_search_with_highlights,  # поиск с подсветкой совпадений в результатах
        "search_by_speaker": svc._history.handle_search_by_speaker,
        "delete_history_item": svc._history.handle_delete_history_item,  # VERIFIED: called from Swift (HistoryPanel)
        "set_paste_status": svc._recording_core_svc.handle_set_paste_status,  # VERIFIED: called from Swift (main)
        "get_settings": svc._settings_svc.handle_get_settings,  # VERIFIED: called from Swift (main)
        "set_settings": svc._settings_svc.handle_set_settings,  # VERIFIED: called from Swift (main)
        "compact_history": svc._history.handle_compact_history,  # VERIFIED: called from Swift (main, HistoryPanel)
        "add_history_item": svc._history.handle_add_history_item,  # VERIFIED: called from Swift (main, HistoryPanel)
        "transcribe_paths": svc._handle_transcribe_paths,  # VERIFIED: called from Swift (HistoryPanel)
        "transcribe_paths_async": svc._handle_transcribe_paths_async,  # PR #14: фоновый job + прогресс
        "get_transcribe_progress": svc._handle_get_transcribe_progress,  # PR #14: опрос прогресса job'а
        "cancel_transcribe_job": svc._handle_cancel_transcribe_job,  # PR #14: запрос отмены job'а
        "preview_transcribe_paths": svc._handle_preview_transcribe_paths,  # VERIFIED: called from Swift (HistoryPanel)
        "translate_text": svc._translation.handle_translate_text,  # VERIFIED: called from Swift (main, HistoryPanel)
        "translate_selection": svc._translation.handle_translate_selection,  # Phase 2A: selection-translate workflow
        "get_diagnostics": svc._handle_get_diagnostics,  # диагностика: system, stt, llm, history, settings_cache
        "set_translation_glossary_item": svc._translation.handle_set_translation_glossary_item,  # VERIFIED: called from Swift (HistoryPanel)
        # VERIFIED: called from Swift (HistoryPanel)
        "remove_translation_glossary_item": svc._translation.handle_remove_translation_glossary_item,
        "get_glossary_suggestions": svc._translation.handle_get_glossary_suggestions,  # авто-обучение глоссария: предлагает пары source→target из истории
        "suggest_medical_glossary_terms": svc._glossary_auto_learn.handle_suggest_medical_glossary_terms,  # мед. домен auto-learn: предлагает пары ES↔RU из истории переводов
        "apply_glossary_suggestions": svc._glossary_auto_learn.handle_apply_glossary_suggestions,  # применяет выбранные мед. термины в translation_glossary
        "export_glossary_csv": svc._glossary_svc.handle_export_glossary_csv,  # экспорт глоссария в CSV-строку
        "import_glossary_csv": svc._glossary_svc.handle_import_glossary_csv,  # импорт CSV в translation_glossary (merge|replace)
        "import_history_ndjson": svc._history.handle_import_history_ndjson,  # VERIFIED: called from Swift (HistoryPanel)
        "get_history_stats": svc._history.handle_get_history_stats,  # VERIFIED: called from Swift (HistoryPanel)
        "get_history_overview": svc._history.handle_get_history_overview,  # VERIFIED: called from Swift (HistoryPanel)
        "get_history_item": svc._history.handle_get_history_item,  # полные детали одной записи истории по ID
        "add_tag": svc._history.handle_add_tag,
        "remove_tag": svc._history.handle_remove_tag,
        "get_tags": svc._history.handle_get_tags,
        "search_by_tag": svc._history.handle_search_by_tag,
        "list_all_tags": svc._history.handle_list_all_tags,
        "get_recording_stats": svc._analytics_svc.handle_get_recording_stats,  # recording metadata statistics (W773: delegated to AnalyticsService)
        "get_metrics_dashboard": svc._handle_get_metrics_dashboard,  # real-time metrics dashboard snapshot
        "summarize_text": svc._text_processing_svc.handle_summarize_text,  # VERIFIED: called from Swift (HistoryPanel)
        "summarize_item": svc._text_processing_svc.handle_summarize_item,  # LLM summary для элемента истории по ID
        "extract_action_items": svc._handle_extract_action_items,  # LLM извлечение задач/решений/вопросов по item_id
        "batch_extract_action_items": svc._handle_batch_extract_action_items,  # пакетное извлечение для нескольких item_id
        "get_pending_action_items": svc._handle_get_pending_action_items,  # все items у которых action_items=None
        "get_last_llm_diff": svc._llm_ops_svc.handle_get_last_llm_diff,  # последний word-level diff от LLM rewriter'а (W783: LLMOpsService)

        "get_vocabulary_suggestions": svc._translation.handle_get_vocabulary_suggestions,
        "toggle_favorite": svc._history.handle_toggle_favorite,
        "get_favorites": svc._history.handle_get_favorites,
        "is_favorite": svc._history.handle_is_favorite,
        "export_history": svc._history.handle_export_history,
        "export_history_srt": svc._history.handle_export_history_srt,
        "export_history_csv": svc._history.handle_export_history_csv,
        "batch_export": svc._history.handle_batch_export,  # пакетный экспорт в нескольких форматах
        "export_history_markdown": svc._history.handle_export_history_markdown,
        "export_obsidian": svc._history.handle_export_obsidian,  # Obsidian-совместимый .md экспорт
        "export_history_json": svc._history.handle_export_history_json,
        "export_html_report": svc._history.handle_export_html_report,  # автономный HTML-отчёт с аналитикой
        "generate_html_report": svc._history.handle_export_html_report,  # алиас для Swift UI (Analytics Dashboard)
        "repaste_item": svc._history.handle_repaste_item,
        "get_clipboard_history": svc._history.handle_get_clipboard_history,  # история буфера обмена: последние N вставленных транскрипций
        "cleanup_old_history": svc._history.handle_cleanup_old_history,  # удаляет записи старше N дней
        "get_storage_info": svc._history.handle_get_storage_info,  # размер файлов данных
        "get_transcripts_path": svc._history.handle_get_transcripts_path,  # путь к папке транскриптов
        "backup_history": svc._history.handle_backup_history,  # создаёт timestamped-резервную копию истории
        "get_auto_backup_status": lambda p: svc._auto_backup.get_auto_backup_status(),  # статус авто-резервного копирования
        "configure_auto_export": svc._handle_configure_auto_export,  # настроить расписание авто-экспорта
        "get_export_schedule_status": lambda p: svc._export_scheduler.get_schedule_status(),  # статус расписания авто-экспорта
        "list_auto_exports": lambda p: {"exports": svc._export_scheduler.list_exports()},  # список файлов авто-экспорта
        "restore_history": svc._history.handle_restore_history,  # восстанавливает историю из резервной копии
        "list_backups": svc._history.handle_list_backups,  # список доступных резервных копий
        "get_history_statistics": svc._history.handle_get_history_statistics,  # агрегированная статистика по истории
        "word_frequency_analysis": svc._history.handle_word_frequency_analysis,  # частотный анализ слов по истории
        "apply_profile_preset": svc._settings_svc.handle_apply_profile_preset,  # применяет пресет настроек профиля
        "list_profile_presets": svc._settings_svc.handle_list_profile_presets,  # список доступных пресетов профилей
        "get_notification_preferences": svc._settings_svc.handle_get_notification_preferences,  # настройки уведомлений
        "set_notification_preferences": svc._settings_svc.handle_set_notification_preferences,  # обновление настроек уведомлений
        "export_settings": svc._settings_svc.handle_export_settings,  # экспорт настроек в JSON-файл
        "import_settings": svc._settings_svc.handle_import_settings,  # импорт настроек из JSON-файла
        "list_settings_backups": svc._settings_svc.handle_list_settings_backups,  # список rolling-бэкапов настроек
        "restore_settings_backup": svc._settings_svc.handle_restore_settings_backup,  # восстановить из бэкапа
        "create_manual_settings_backup": svc._settings_svc.handle_create_manual_settings_backup,  # ручной бэкап настроек
        # --- Per-app paste profile memory ---
        "get_paste_profile_for_app": svc._paste_app_memory.handle_get_paste_profile_for_app,  # VERIFIED: called from Swift (PasteService)
        "record_paste_app_profile": svc._paste_app_memory.handle_record_paste_app_profile,  # VERIFIED: called from Swift (PasteService)
        "list_app_profiles": svc._paste_app_memory.handle_list_app_profiles,  # список сохранённых профилей по приложениям

        "get_audio_devices": svc._handle_get_audio_devices,  # список доступных аудиовходов для GUI
        "test_microphone": svc._handle_test_microphone,  # тест микрофона: RMS/peak уровни
        "auto_summarize_batch": svc._history.handle_auto_summarize_batch,  # авто-резюме пакета транскрипций через LLM
        "list_summary_profiles": svc._history.handle_list_summary_profiles,  # список профилей резюмирования
        "add_summary_profile": svc._history.handle_add_summary_profile,  # добавить кастомный профиль резюмирования
        "filter_by_confidence": svc._history.handle_filter_by_confidence,  # фильтрация истории по STT confidence score
        "health_check": svc._handle_health_check,  # агрегированный health check всех подсистем
        # --- Phase B.1: error bus + LLM probe ---
        "report_paste_failure": svc._handle_report_paste_failure,  # Swift→backend paste failure report (ax_denied / app_unsupported)
        "report_hotkey_conflict": svc._handle_report_hotkey_conflict,  # Swift→backend hotkey conflict (chord taken by another app)
        "handshake": svc._handle_handshake,  # Swift→backend handshake on connect: version + capabilities exchange
        "report_reconnect": svc._handle_report_reconnect,  # Swift→backend reconnect telemetry: pushes ipc.reconnect info event
        "list_recent_errors": svc._handle_list_recent_errors,  # ring-буфер KrabError: последние N ошибок
        "clear_recent_errors": svc._handle_clear_recent_errors,  # очистить ring-буфер ошибок
        "handle_error_action": svc._handle_handle_error_action,  # выполнить actionable-действие из toast/diagnostics
        "probe_llm_http": svc._handle_probe_llm_http,  # однократный ping LM Studio HTTP endpoint
        "warmup_stt": svc._stt_mgmt_svc.handle_warmup_stt,  # ручной запуск STT warmup (после смены профиля/модели)
        "warmup_rewriter": svc._handle_warmup_rewriter,  # явный warmup-probe для "Load Model" кнопки
        "analyze_audio_quality": svc._audio_analytics_svc.handle_analyze_audio_quality,  # pre-flight анализ качества аудиофайла
        "analyze_silence": svc._audio_analytics_svc.handle_analyze_silence,  # обнаружение тишины и доли речи в аудиофайле
        "get_error_report": svc._error_reporter.handle_get_error_report,  # последние ошибки из ring-буфера
        "get_error_stats": svc._error_reporter.handle_get_error_stats,  # счётчики ошибок по компоненту/типу/окну
        "send_diagnostics_to_sentry": svc._handle_send_diagnostics_to_sentry,  # экспортирует ring-буфер ошибок в Sentry (breadcrumbs + capture_message)
        "get_memory_stats": svc._handle_get_memory_stats,  # RSS/VSZ для backend/agent/worker процессов (psutil)
        "get_usage_stats": svc._handle_get_usage_stats,
        "get_audio_info": svc._audio_analytics_svc.handle_get_audio_info,  # метаданные аудиофайла  # ежедневная статистика использования: записи, длительность, слова
        "get_system_info": svc._handle_get_system_info,  # мониторинг системных ресурсов: CPU, RAM, диск, GPU
        "find_duplicates": svc._history.handle_find_duplicates,  # обнаружение дублирующихся транскрипций по текстовому сходству
        "set_annotation": svc._history.handle_set_annotation,  # сохранить пользовательскую заметку к записи истории
        "get_annotation": svc._history.handle_get_annotation,  # получить заметку для записи истории
        "search_annotations": svc._history.handle_search_annotations,  # полнотекстовый поиск по заметкам
        "create_collection": svc._collections.handle_create_collection,  # создать коллекцию/папку для организации истории
        "delete_collection": svc._collections.handle_delete_collection,  # удалить коллекцию
        "list_collections": svc._collections.handle_list_collections,  # список всех коллекций
        "add_to_collection": svc._collections.handle_add_to_collection,  # добавить запись истории в коллекцию
        "remove_from_collection": svc._collections.handle_remove_from_collection,  # удалить запись из коллекции
        "rename_collection": svc._collections.handle_rename_collection,  # переименовать коллекцию
        "list_normalization_profiles": svc._handle_list_normalization_profiles,  # список профилей нормализации текста
        "get_collection_items": svc._collections.handle_get_collection_items,  # получить записи истории из коллекции
        "start_chain": svc._chains.handle_start_chain,  # начать цепочку связанных записей
        "add_to_chain": svc._chains.handle_add_to_chain,  # добавить запись в цепочку
        "end_chain": svc._chains.handle_end_chain,  # завершить цепочку
        "get_chain": svc._chains.handle_get_chain,  # получить цепочку с деталями
        "list_chains": svc._chains.handle_list_chains,  # список цепочек
        "merge_chain_text": svc._chains.handle_merge_chain_text,  # объединённый текст цепочки
        "unlink_recording_from_chain": svc._chains.handle_unlink_recording_from_chain,  # убрать запись из цепочки
        "schedule_recording": svc._recording_scheduler.handle_schedule_recording,  # запланировать запись на определённое время
        "cancel_scheduled_recording": svc._recording_scheduler.handle_cancel_scheduled_recording,  # отменить запланированную запись
        "list_scheduled_recordings": svc._recording_scheduler.handle_list_scheduled_recordings,  # список запланированных записей
        "generate_daily_digest": svc._handle_generate_daily_digest,  # ежедневный дайджест транскрипций
        "analyze_quality_trends": svc._audio_analytics_svc.handle_analyze_quality_trends,  # анализ трендов качества
        "compare_periods": svc._handle_compare_periods,  # сравнение двух периодов использования
        "get_activity_calendar": svc._handle_get_activity_calendar,  # GitHub-style activity calendar данные
        "get_recording_insights": svc._handle_get_recording_insights,  # эвристические инсайты по записям (Wave 54: alias was wrongly pointed at _handle_get_recording_stats)
        "get_sentiment_trends": svc._handle_get_sentiment_trends,  # анализ трендов тональности транскрипций за N дней

        "check_integrity": svc._handle_check_integrity,  # проверка целостности данных
        "repair_integrity": svc._handle_repair_integrity,  # исправление проблем целостности данных
        "extract_terms": svc._handle_extract_terms,  # извлечение терминов из текста
        "compare_texts": svc._text_processing_svc.handle_compare_texts,  # сравнение двух текстов/транскрипций
        "get_context_memory": svc._handle_get_context_memory,  # контекстная память STT: слова и темы из последних транскрибаций
        "score_readability": svc._text_processing_svc.handle_score_readability,  # оценка читабельности текста транскрибации
        "score_transcription": svc._handle_score_transcription,  # оценка качества транскрибации (0–100, A–F)
        "get_event_log": svc._event_replay.handle_get_event_log,  # лог событий для отладки (фильтрация по типу/времени)
        "get_event_stats": svc._event_replay.handle_get_event_stats,  # статистика событий: счётчики, скорость/мин
        "replay_events": svc._event_replay.handle_replay_events,  # воспроизведение событий в диапазоне времени
        "get_waveform": svc._audio_analytics_svc.handle_get_waveform,  # генерация waveform-данных для GUI-визуализации
        "get_throttle_stats": svc._handle_get_throttle_stats,  # статистика IPC throttle: вызовы, отклонения
        "check_audio_duplicate": svc._audio_analytics_svc.handle_check_audio_duplicate,  # аудио-фингерпринтинг для обнаружения дубликатов
        "batch": svc._handle_batch,  # пакетное выполнение нескольких IPC-методов за один вызов (макс. 50)
        "get_keyword_cloud": svc._handle_get_keyword_cloud,  # данные облака ключевых слов для визуализации word cloud
        "prepare_share": svc._sharing.handle_prepare_share,  # подготовить пакет для шаринга транскрипций
        "list_shared": svc._sharing.handle_list_shared,  # список сохранённых пакетов шаринга
        "get_shared": svc._sharing.handle_get_shared,  # получить пакет шаринга по share_id
        "revoke_share_link": svc._sharing.handle_revoke_share_link,  # отозвать пакет шаринга по токену (Wave 158)
        "save_transcript_version": svc._transcript_versioning.handle_save_transcript_version,  # сохранить новую версию текста транскрипции
        "get_transcript_versions": svc._transcript_versioning.handle_get_transcript_versions,  # получить все версии транскрипции по item_id
        "revert_transcript_version": svc._transcript_versioning.handle_revert_transcript_version,  # откат транскрипции к указанной версии
        "generate_auto_title": svc._handle_generate_auto_title,  # автоматическая генерация заголовка для транскрибации
        # форматирование текста под целевое приложение (telegram, notes, email и др.)
        "format_for_paste": svc._paste_formatter.handle_format_for_paste,
        "merge_recordings": lambda p: svc._merger.handle_merge_recordings(p, svc.store),  # объединить несколько записей истории в одну
        "preview_merge": lambda p: svc._merger.handle_preview_merge(p, svc.store),  # предпросмотр объединения без сохранения
        "list_paste_formatters": svc._paste_formatter.handle_list_paste_formatters,  # список доступных форматтеров вставки
        "get_learning_stats": svc._handle_get_learning_stats,  # режим изучения языков: статистика прогресса
        "get_analytics_dashboard": svc._handle_get_analytics_dashboard,  # комплексный дашборд аналитики: все метрики за один вызов
        "get_topic_timeline": svc._handle_get_topic_timeline,  # таймлайн смен тем разговора из истории транскрибаций
        "list_config_presets": svc._config_presets.handle_list_config_presets,  # список конфигурационных пресетов (встроенных и кастомных)
        "apply_config_preset": svc._config_presets.handle_apply_config_preset,  # применить конфигурационный пресет — вернуть settings_patch
        "create_config_preset": svc._config_presets.handle_create_config_preset,  # создать кастомный конфигурационный пресет
        "enqueue_transcription": svc._transcription_queue.handle_enqueue,  # добавить аудиофайл в очередь транскрипции с приоритетом
        "cancel_transcription": svc._transcription_queue.handle_cancel,  # отменить задание транскрипции по job_id
        "get_queue_status": svc._transcription_queue.handle_get_status,  # статус задания транскрипции по job_id
        "list_transcription_queue": svc._transcription_queue.handle_list_queue,  # список всех заданий очереди транскрипции
        "detect_emotion": svc._text_processing_svc.handle_detect_emotion,  # эвристическое определение эмоции в тексте транскрипции
        "estimate_recording_cost": svc._handle_estimate_recording_cost,  # оценка вычислительной стоимости обработки записи
        "get_daily_cost_summary": svc._handle_get_daily_cost_summary,  # сводка вычислительных расходов за сегодня
        "check_migration": svc._data_migrator.handle_check_migration,  # проверка необходимости миграции данных
        "run_migration": svc._data_migrator.handle_run_migration,  # выполнение миграции данных между версиями
        "expand_abbreviations": svc._text_processing_svc.handle_expand_abbreviations,  # раскрытие аббревиатур в тексте транскрипции
        "remove_abbreviation": svc._text_processing_svc.handle_remove_abbreviation,  # удалить аббревиатуру
        "list_abbreviations": svc._text_processing_svc.handle_list_abbreviations,  # список аббревиатур для языка
        "profile_noise": svc._audio_analytics_svc.handle_profile_noise,  # профилирование фонового шума: тип, уровень, SNR, рекомендации
        "configure_obsidian_sync": svc._obsidian_sync.handle_configure,  # настроить Obsidian vault для синхронизации транскрипций
        "run_obsidian_sync": svc._obsidian_sync.handle_sync,  # синхронизировать записи истории с Obsidian vault
        "get_obsidian_sync_status": svc._obsidian_sync.handle_get_status,  # статус синхронизации с Obsidian vault
        # зарегистрировать воспроизведение записи (item_id, duration_listened_sec)
        "record_playback": svc._playback_tracker.handle_record_playback,
        # статистика воспроизведения одной записи: play_count, total_listened_sec, last_played
        "get_playback_stats": svc._playback_tracker.handle_get_playback_stats,
        "get_most_replayed": svc._playback_tracker.handle_get_most_replayed,  # топ N наиболее часто воспроизводимых записей
        # прогнать текст через настраиваемый конвейер пост-обработки (пробелы, пунктуация, сущности, аббревиатуры, анонимизация)
        "post_process_text": svc._text_processing_svc.handle_post_process_text,
        "list_post_process_steps": svc._text_processing_svc.handle_list_post_process_steps,  # список доступных шагов пост-обработки текста
        "compare_recordings": svc._handle_compare_recordings,  # сравнение нескольких записей side-by-side: матрица сходства, статистика, общие/уникальные слова
        "select_model": svc._stt_mgmt_svc.handle_select_model,  # умный выбор STT-модели на основе условий записи
        "get_smart_vocabulary_suggestions": svc._handle_get_smart_vocabulary_suggestions,  # предложения для словаря STT на основе паттернов использования
        "get_startup_diagnostics": svc._handle_get_startup_diagnostics,  # диагностика при старте: результаты всех startup-проверок
        # автоматическое обогащение метаданных записи: word_count, emotion, pace, quality, topics и др.
        "enrich_recording": svc._metadata_enricher.handle_enrich_recording,
        "get_shutdown_status": svc._handle_get_shutdown_status,  # статус последнего graceful shutdown: clean, last_shutdown_time
        "check_duplicate": svc._handle_check_duplicate,  # проверка одной транскрипции на дублирование по текстовому сходству
        "run_deduplication": svc._handle_run_deduplication,  # полное сканирование истории на дубликаты
        "get_dedup_stats": svc._handle_get_dedup_stats,  # статистика дедупликатора: проверено, найдено, символов сохранено
        "get_timeline_view": svc._handle_get_timeline_view,  # группировка истории по временным блокам (timeline)
        "get_recent_searches": svc._search_history.handle_get_recent_searches,  # последние поисковые запросы пользователя
        "get_popular_searches": svc._search_history.handle_get_popular_searches,  # наиболее частые поисковые запросы
        "clear_search_history": svc._search_history.handle_clear_search_history,  # очистить историю поисковых запросов
        "archive_items": svc._archive_manager.handle_archive_items,  # переместить записи истории в архив
        "unarchive_items": svc._archive_manager.handle_unarchive_items,  # восстановить записи из архива
        "list_archived": svc._archive_manager.handle_list_archived,  # список архивированных записей
        "get_archive_stats": svc._archive_manager.handle_get_archive_stats,  # статистика архива: количество, размер, oldest/newest
        "generate_stats_report": svc._handle_generate_stats_report,  # полный Markdown-отчёт статистики за период
        "generate_mini_stats_report": svc._handle_generate_mini_stats_report,  # краткий 5-строчный отчёт состояния
        # --- Phase 3 safeguards ---
        "call_estimate_cost": svc._call_cost_estimator.handle_estimate_cost,  # оценить стоимость звонка по провайдеру и стране
        # --- text templates ---
        "get_templates": svc._template_manager.handle_get_templates,  # список шаблонов быстрой вставки текста
        "add_template": svc._template_manager.handle_add_template,  # добавить шаблон текста
        "remove_template": svc._template_manager.handle_remove_template,  # удалить шаблон текста
        "apply_template": svc._template_manager.handle_apply_template,  # применить шаблон (подставить переменные)
        # --- webhooks ---
        "register_webhook": svc._webhook_manager.handle_register_webhook,  # зарегистрировать webhook для событий
        "unregister_webhook": svc._webhook_manager.handle_unregister_webhook,  # отменить регистрацию webhook
        "list_webhooks": svc._webhook_manager.handle_list_webhooks,  # список зарегистрированных webhook-ов
        # --- speaker aliases ---
        "set_speaker_alias": svc._speaker_manager.handle_set_speaker_alias,  # назначить псевдоним для спикера
        "get_speaker_aliases": svc._speaker_manager.handle_get_speaker_aliases,  # список псевдонимов спикеров
        "remove_speaker_alias": svc._speaker_manager.handle_remove_speaker_alias,  # удалить псевдоним спикера
        # --- live subtitles (Sprint 2B) ---
        "live_subs_ingest": svc._live_subs.handle_ingest,  # потоковая STT+translate (частый вызов)
        "live_subs_stop": svc._live_subs.handle_stop,  # flush и сброс буфера
        # --- plugins ---
        "list_plugins": svc._plugin_manager.handle_list_plugins,  # список обнаруженных плагинов
        "get_plugin_info": svc._plugin_manager.handle_get_plugin_info,  # информация о конкретном плагине
        "unload_plugin": svc._plugin_manager.handle_unload_plugin,  # полная выгрузка плагина из памяти
        # --- feature flags ---
        "get_feature_flags": svc._feature_flags.handle_get_feature_flags,  # получить все feature-флаги с описаниями
        "set_feature_flag": svc._feature_flags.handle_set_feature_flag,  # установить значение feature-флага
        # --- hotwords ---
        "add_hotword": svc._hotword_detector.handle_add_hotword,  # добавить горячее слово для отслеживания
        "remove_hotword": svc._hotword_detector.handle_remove_hotword,  # удалить горячее слово
        "get_hotwords": svc._hotword_detector.handle_get_hotwords,  # список горячих слов
        "check_hotwords": svc._hotword_detector.handle_check_hotwords,  # проверить текст на наличие горячих слов
        # --- model cache ---
        "list_cached_models": svc._model_cache_manager.handle_list_cached_models,  # список кэшированных ML-моделей
        "get_model_cache_info": svc._model_cache_manager.handle_get_model_cache_info,  # информация о кэше конкретной модели
        # --- Voice Assistant wake word config (PR 1.5) ---
        # --- openWakeWord adapter (free, Apache 2.0) ---
        "wake_word_list_models": svc._oww_adapter.handle_wake_word_list_models,  # список builtin+custom моделей
        "wake_word_start": svc._oww_adapter.handle_wake_word_start,  # запустить прослушивание
        "wake_word_stop": svc._oww_adapter.handle_wake_word_stop,  # остановить прослушивание
        "wake_word_status": svc._oww_adapter.handle_wake_word_status,  # статус адаптера
        # --- Dual-mode TTS (Silero RU + Kokoro EN + macOS say fallback) ---
        "synthesize_speech": svc._tts.handle_synthesize_speech,  # синтез речи: text, language (ru/en/auto), voice
        "analyze_word_timing": svc._audio_analytics_svc.handle_analyze_word_timing,  # анализ ритма речи по пословным таймстемпам Whisper
        # --- Telegram Bridge (Krab Ear → main Krab userbot) ---
        "send_to_telegram": svc._handle_send_to_telegram,  # отправить транскрипцию в Telegram через main Krab userbot
        # --- Apple Notes integration (Phase D.4) ---
        "create_apple_note": svc._handle_create_apple_note,  # создать заметку в Apple Notes через osascript
        # --- Apple Reminders integration (Phase D.4) ---
        "create_apple_reminder": svc._handle_create_apple_reminder,  # создать напоминание в Apple Reminders через osascript
        # --- Apple Calendar integration (Phase D.4) ---
        "create_calendar_event": svc._handle_create_calendar_event,  # создать событие в Apple Calendar через osascript
        # --- iMessage integration (Phase D.4) ---
        "send_imessage": svc._handle_send_imessage,  # отправить сообщение через iMessage/SMS через osascript
        "list_telegram_chats": svc._handle_list_telegram_chats,  # получить список доступных чатов Telegram через main Krab userbot
        # --- Phase 3: Call Session CRUD (outbound call automation) ---
        "call_session_create": svc._call_session_service.handle_call_session_create,  # создать звонковую сессию
        "call_session_get": svc._call_session_service.handle_call_session_get,  # получить сессию по id
        "call_session_list": svc._call_session_service.handle_call_session_list,  # список сессий с опциональным фильтром по статусу
        "call_session_update_status": svc._call_session_service.handle_call_session_update_status,  # переход статуса сессии
        "call_session_add_transcript": svc._call_session_service.handle_call_session_add_transcript,  # добавить реплику в транскрипт
        "call_session_end": svc._call_session_service.handle_call_session_end,  # завершить сессию: compute duration, total_cost
        # --- STT hotwords (initial_prompt boost) ---
        "add_stt_hotword": svc._stt_mgmt_svc.handle_add_stt_hotword,  # добавить термин в STT hotwords список
        "remove_stt_hotword": svc._stt_mgmt_svc.handle_remove_stt_hotword,  # удалить термин из STT hotwords списка
        "list_stt_hotwords": svc._stt_mgmt_svc.handle_list_stt_hotwords,  # получить весь список STT hotwords
        # --- Recording bookmarks (Cmd+Shift+B) ---
        "add_bookmark": svc._bookmarks.handle_add_bookmark,  # создать закладку на текущей позиции записи
        "list_bookmarks": svc._bookmarks.handle_list_bookmarks,  # список закладок для item_id
        "list_all_bookmarks": svc._bookmarks.handle_list_all_bookmarks,  # все активные закладки
        "delete_bookmark": svc._bookmarks.handle_delete_bookmark,  # удалить закладку (tombstone)
        "jump_to_bookmark": svc._bookmarks.handle_jump_to_bookmark,  # перейти к закладке (эмитит playback.seek)
        # --- Semantic search (opt-in, multilingual embeddings) ---
        "semantic_search": svc._handle_semantic_search,  # семантический поиск по истории через embeddings
        "semantic_search_status": svc._handle_semantic_search_status,  # статус семантического поиска: модель, индекс
        "semantic_search_reindex": svc._handle_semantic_search_reindex,  # переиндексировать всю историю
        "semantic_search_reset": svc._handle_semantic_search_reset,  # сброс зафиксированной ошибки загрузки модели (W884-E3)
        # --- LM Studio model discovery ---
        "list_llm_models": svc._llm_ops_svc.handle_list_llm_models,  # список моделей из LM Studio /v1/models (W783: LLMOpsService)
        # --- Quick word replacement (Cmd+Shift+R) ---
        "replace_word_in_last_transcript": svc._llm_ops_svc.handle_replace_word_in_last_transcript,  # заменить слово в последней транскрипции (W783: LLMOpsService)
        # --- Privacy audit log ---
        "get_privacy_audit_log": svc._handle_get_privacy_audit_log,  # последние записи privacy audit log
        "clear_privacy_audit_log": svc._handle_clear_privacy_audit_log,  # удалить файл privacy audit log
        # --- D.2.3: Scored STT routing decision ---
        "get_stt_routing_decision": svc._stt_mgmt_svc.handle_get_stt_routing_decision,  # scored adapter selection debug
        # --- Default STT hotwords seed ---
    }
