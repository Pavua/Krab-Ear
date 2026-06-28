# Бриф: Settings-UI для облачной полировки транскриптов (cloud-rewriter, opt-in)

## Что это
Новая **opt-in** фича: когда ЛОКАЛЬНЫЙ LLM-rewriter (LM Studio) недоступен, транскрипт можно полировать через ОБЛАЧНЫЙ LLM. Тебе нужно сделать ТОЛЬКО Swift-UI настройки. Backend делает другой воркер — ты работаешь через уже существующие IPC `get_settings` / `set_settings` (новых IPC-методов НЕ выдумывай).

## Контракт настроек (ровно эти ключи, буква-в-букву — НЕ переименовывай)
- `cloud_rewriter_enabled` — Bool, дефолт `false`. Главный тумблер фичи (opt-in).
- `cloud_rewriter_provider` — String, `"openai"` или `"anthropic"`. Какой облачный провайдер.
- `openai_api_key` — String (уже существует в настройках). Ключ для провайдера openai.
- `anthropic_api_key` — String (новый). Ключ для провайдера anthropic.
- `privacy_mode_enabled` — Bool (read-only здесь). Если `true` — фича принудительно не работает (backend так и сделает); в UI показать это как примечание.

## UI — что построить
Добавь новую сворачиваемую секцию **«Облачная полировка»** (sectionId `cloud_rewriter`) в Settings-вкладку, рядом с другими LLM/облачными настройками (исследуй существующие `HistoryPanelController+*.swift` extension-файлы — повтори их паттерн: associated objects, `setupX` → `CollapsibleSectionView`, dual-variant ThemeCardView + CDSettingsCardView если в проекте есть оба стиля).

Контролы:
1. **Тумблер «Включить облачную полировку (fallback)»** ↔ `cloud_rewriter_enabled`.
2. **Picker провайдера** (OpenAI / Anthropic) ↔ `cloud_rewriter_provider`.
3. **Поле API-ключа** (NSSecureTextField) — пишет в `openai_api_key` или `anthropic_api_key` в зависимости от выбранного провайдера. Покажи маску/плейсхолдер, не печатай ключ в логи.
4. **🔴 Предупреждение о приватности (ОБЯЗАТЕЛЬНО, видное)**: текст вроде «⚠️ При включении ваши транскрипты отправляются выбранному облачному провайдеру для полировки. Это нарушает локально-приватный режим. Не включайте для конфиденциального контента. В режиме приватности фича автоматически отключена.» — некрупный, но явный, под тумблером.
5. Если `privacy_mode_enabled == true`: задизейблить тумблер + показать «Недоступно в режиме приватности».

## Жёсткие правила (CI/конвенции проекта — иначе сборка/ревью завернёт)
- **IPC строго off-main** (AGENT-3): любой `ipcClient.call` — на `DispatchQueue.global`, UI-обновления назад через `@MainActor`/`DispatchQueue.main`. НЕ блокируй main thread.
- **НЕ `runModal()`** — только non-blocking sheets через `AlertHelpers` (`presentAlertSheet`/`presentPanelSheet`), если нужны алерты.
- **Glyph-gate**: не вводи новые non-ASCII глифы в коде кроме уже встречающихся в `native/`; для иконок — SF Symbols (`lock.fill`, `cloud.fill` и т.п.), не Unicode-литералы (CoreText-hang класса AGENT-J/M).
- Тема/токены — `KrabEarTheme`, как в соседних секциях.
- В конце: `swift build -c release` ДОЛЖЕН пройти зелёным. Если не уверен в API — сверься с соседним extension-файлом, не выдумывай.

## Граница
Только Swift-UI настройки + проводка к `get_settings`/`set_settings`. НЕ трогай Python, НЕ трогай rewriter-логику, НЕ добавляй IPC-методы. Не делай git-операций кроме работы в своей ветке.
