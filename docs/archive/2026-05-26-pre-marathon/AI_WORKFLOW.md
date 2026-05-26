<!--
Единые правила для GPT/Claude/Gemini и любых других агентов в Krab Ear.
-->

# AI Workflow (Single Source Of Truth)

## 1. Каноничные документы

Любой агент обязан использовать только:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/PRD.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/ROADMAP.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/ARCHITECTURE.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/AI_WORKFLOW.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/MASTER_PLAN.md`
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/docs/AUTONOMOUS_EXECUTION_PLAN.md`

## 2. Непересекаемые инварианты

- Проект полностью отдельный от OpenClaw/Nexus.
- Базовый режим работы: локальный offline STT.
- История транскрибаций: **без лимита** (`history_policy=unlimited`).
- UI читает историю страницами (`history_page_size=50` по умолчанию).
- При сбое вставки текст обязан сохраняться в истории и буфере обмена.

## 3. Правила изменений

- Проверять критический поток: `start -> stop -> transcribe -> paste/history`.
- Не вводить альтернативные PRD/roadmap-файлы в корне.
- Любые устаревшие ветки/документы переносить в архив, а не оставлять рядом с актуальными.
- Любые новые IPC/форматы фиксировать в `PRD.md` и `ARCHITECTURE.md`.

## 4. Legacy

Старая tkinter-ветка перенесена в:
- `/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/_legacy_tkinter_archive_2026-02-11`

Её не использовать для новых решений, кроме точечного референса.

## 5. Поведение при завершении очереди Roadmap

- Если явные спринты закончились, агент продолжает только low-risk задачи (стабильность, тесты, UX-полировки).
- High-risk изменения добавляются в `ROADMAP.md` как `PROPOSAL` и требуют подтверждения пользователя.
