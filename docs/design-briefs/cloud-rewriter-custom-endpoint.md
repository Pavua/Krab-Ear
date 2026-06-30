# Бриф: добавить «Custom (self-hosted)» провайдер в секцию «Облачная полировка»

## Контекст
Секция «Облачная полировка» (`HistoryPanelController+CloudRewriter.swift`) УЖЕ существует с picker'ом провайдера (OpenAI / Anthropic) + полем API-ключа. Нужно ДОБАВИТЬ третий вариант — **Custom (self-hosted / no-log)** — чтобы приватный юзер указал СВОЙ OpenAI-совместимый endpoint (Ollama, vLLM, или no-log провайдер), а не слал транскрипты в OpenAI/Anthropic. Backend-поддержку делает другой воркер; ты работаешь через `get_settings`/`set_settings` (новых IPC НЕ выдумывай).

## Контракт настроек (ровно эти ключи, буква-в-букву)
- `cloud_rewriter_provider` — теперь String из {`"openai"`, `"anthropic"`, `"custom"`} (добавь третий пункт в picker).
- `cloud_rewriter_base_url` — String (новый). URL OpenAI-совместимого endpoint, напр. `http://localhost:11434/v1`. Показывать/редактировать ТОЛЬКО когда provider == "custom".
- `cloud_rewriter_custom_model` — String (новый). Имя модели на custom-endpoint, напр. `qwen2.5:7b`. Показывать ТОЛЬКО когда provider == "custom".
- `cloud_rewriter_api_key` — String (новый, опциональный). Ключ для custom-endpoint (self-hosted часто без ключа → пустое поле допустимо). Показывать когда provider == "custom".
- `openai_api_key` / `anthropic_api_key` — существующие, для своих провайдеров (не трогай их логику).

## Что построить (добавить в существующую секцию, НЕ создавать новую)
1. В picker'е провайдера добавить пункт **«Custom (свой сервер)»** → пишет `cloud_rewriter_provider = "custom"`.
2. Когда выбран custom — показать (иначе скрыть/задизейблить):
   - Поле **«URL endpoint»** ↔ `cloud_rewriter_base_url` (плейсхолдер `http://localhost:11434/v1`).
   - Поле **«Модель»** ↔ `cloud_rewriter_custom_model` (плейсхолдер `qwen2.5:7b`).
   - Поле **«API-ключ (опционально)»** ↔ `cloud_rewriter_api_key` (NSSecureTextField; пусто = без ключа).
3. **Privacy-note (позитивный)** под custom-полями: «✅ Рекомендуется для приватности: укажите свой self-hosted сервер (Ollama/vLLM) или no-log провайдера — транскрипты идут только туда.» (в отличие от OpenAI/Anthropic, которые логируют).

## Жёсткие правила (CI/конвенции — иначе ревью/сборка завернёт)
- IPC строго **off-main** (AGENT-3): `ipcClient.call` на `DispatchQueue.global`, UI назад через MainActor. Используй уже существующий в файле паттерн (`applySettingsPatch` и пр.).
- **НЕ `runModal()`** (sheets через AlertHelpers если надо).
- **Glyph-gate**: не вводи НОВЫЕ non-ASCII глифы кроме уже встречающихся в `native/` (✅ и ⚠️ уже используются — ок); иконки — SF Symbols.
- `KrabEarTheme`-токены как в соседних секциях. Покажи/скрой custom-поля реактивно при смене picker'а.
- В конце ОБЯЗАТЕЛЬНО `swift build -c release` из native/KrabEarAgent — зелёный. Не выдумывай Swift-API, сверяйся с тем же файлом и соседними.

## Граница
Только Swift-UI в `HistoryPanelController+CloudRewriter.swift` (+ при необходимости ключи в `Models.swift` `AgentSettings` — `cloud_rewriter_base_url`, `cloud_rewriter_custom_model`, `cloud_rewriter_api_key`, буква-в-букву). НЕ трогай Python, НЕ другие секции, НЕ IPC-методы.
