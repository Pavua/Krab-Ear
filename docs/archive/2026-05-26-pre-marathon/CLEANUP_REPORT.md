<!--
Отчёт по очистке репозитория.
Нужен, чтобы прозрачно видеть, что осталось рабочим, а что вынесено в архив.
-->

# Cleanup Report

Дата: 2026-02-11

## 1. Что оставлено в рабочем контуре

- `/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/scripts`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/Start Krab Ear.command`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/README.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/*`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear`

## 2. Что вынесено в архив

Вся нецелевая структура перемещена в:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/_ARCHIVE_NON_KRAB_EAR_2026-02-11`

Ключевые блоки в архиве:
- `openclaw_official/` (полный исходник OpenClaw + node_modules)
- `nexus/` и `nexus_backup_before_mega_upgrade/`
- старые launchers, bridge-скрипты, логи, черновые markdown-файлы
- legacy-файлы старого KrabEar (`ear.py`, `ear_ui.py`, `debug_ui.py`, старые venv/pycache)

## 3. Что может быть полезно позже

- `openclaw_official/docs/` и `openclaw_official/skills/` как справочник идей по архитектуре.
- `nexus/IMPROVEMENTS_RU.md` как источник продуктовых гипотез.
- `KrabEar_backup_stable_20260208/` как исторический снимок поведения до рефактора.
- `KrabEar/_legacy_tkinter_archive_2026-02-11/` как reference старой UI-ветки.

## 4. Почему это безопасно

- Ничего не удалено безвозвратно: всё лишнее перемещено, а не уничтожено.
- Standalone-запуск Krab Ear теперь не зависит от архивных папок.
