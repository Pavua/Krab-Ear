# Work Split (Проценты и зоны)

Ниже зафиксировано практическое разделение остатка roadmap между агентами.

## Целевое распределение на ближайшие 2-3 цикла

1. **Antigravity: 65%**
2. **Codex: 35%**

Это попадает в твой диапазон 50-70% для Antigravity.

## Что именно входит в 65% Antigravity

1. `Krab Ear native UI/UX` (основной объём):
- layout/адаптивность/history-first UX;
- shortcuts, индикаторы фильтров, onboarding flow;
- polish и визуальная консистентность.

2. `iOS client`:
- SwiftUI + CallKit/PushKit каркас;
- экран сессии, subtitle preview, настройки Gateway.

3. `OpenClaw orchestration`:
- команды звонков и пользовательские ответы;
- UX вокруг call management в OpenClaw.

## Что именно входит в 35% Codex

1. `Krab Ear backend`:
- стабильность IPC и state store;
- тестовая матрица, regression gates.

2. `Krab Voice Gateway backend`:
- API-контракты, Twilio lifecycle, diagnostics, runtime tuning;
- smoke/test automation и операционные скрипты.

3. `Интеграционные контракты`:
- синхронизация API.md;
- acceptance-скрипты, boundary-check, release-checklist.

## Текущее правило приоритета

1. Если задача UI/client/orchestration -> **Antigravity owner**.
2. Если задача backend/platform/contracts -> **Codex owner**.
3. Если задача mixed -> делится на 2 PR/итерации по owner зонам.
