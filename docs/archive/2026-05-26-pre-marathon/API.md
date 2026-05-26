<!--
Публичный API-пакет Krab Ear (локальный IPC).
-->

# Krab Ear IPC API

Транспорт:
- Unix socket (`~/Library/Application Support/KrabEar/krabear.sock`)
- JSON request/response

Формат запроса:
```json
{"id":"req-1","method":"ping","params":{}}
```

Формат ответа (успех):
```json
{"id":"req-1","ok":true,"result":{"status":"ok"}}
```

Формат ответа (ошибка):
```json
{"id":"req-1","ok":false,"error":{"code":"internal_error","message":"..."}}
```

Основные методы:
1. `start_recording`
2. `stop_recording`
3. `get_recording_state`
4. `get_history_page`
5. `search_history`
6. `add_history_item`
7. `delete_history_item`
8. `set_paste_status`
9. `get_settings`
10. `set_settings`
11. `translate_text`
12. `summarize_text`
13. `start_call_assist`
14. `stop_call_assist`
15. `get_call_assist_state`
16. `call_assist_summary`
17. `call_assist_diagnostics`
18. `call_assist_quick_phrase`
19. `list_call_assist_quick_phrases`
20. `call_assist_timeline`
21. `call_assist_timeline_stats`
22. `call_assist_timeline_summary`
23. `call_assist_timeline_export`
24. `call_assist_timeline_clear`
25. `call_assist_timeline_to_history`
26. `call_assist_cost_estimate`
27. `preview_transcribe_paths`
28. `transcribe_paths`
29. `compact_history`
30. `get_history_stats`
31. `get_history_overview`
32. `import_history_ndjson`
33. `get_capabilities`

Ключевые settings-поля:
1. `translation_mode`
2. `translation_style`
3. `translate_and_paste`
4. `hotkey_profile` (`default|meeting|translation`)
5. `update_channel` (`stable|beta`)
6. `call_auto_summary`
7. `history_page_size`
8. `history_focus_mode`
9. `history_text_density` (`normal|compact`)
10. `text_templates`
