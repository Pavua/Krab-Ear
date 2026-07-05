# Sparkle auto-update — дизайн (2026-07-05)

**Статус**: одобрено юзером (вариант B — CI-автоматизация; триггер — тег И workflow_dispatch; хостинг — GitHub Releases).
**Контекст**: у Krab Ear нет механизма автообновлений вообще — DMG-получатели и владелец обновляются только ручной пересборкой. Это последний крупный пункт release-readiness, не зависящий от Apple Developer ID (Sparkle EdDSA-подпись ортогональна codesign identity и работает на self-signed «Krab Ear Dev Local»).

## Решения, принятые юзером

- **Вариант B**: релиз собирает и публикует GitHub Actions (не локальный скрипт). Приватный ключ self-signed сертификата уходит в GitHub Actions Secrets — риск осознан и принят (компрометация ключа значима только для машин, где этот сертификат доверен, т.е. владельца).
- **Триггер**: пуш тега `vX.Y.Z` (основной путь) + `workflow_dispatch` с ручным вводом версии (fallback/тестирование).
- **Хостинг**: GitHub Releases (репо публичный — это ОГРАНИЧЕНИЕ: на приватном репо release-ассеты требуют авторизации, анонимная Sparkle-загрузка сломается; приватность репо — отдельный вопрос, несовместимый с этой схемой без прокси).
- appcast.xml живёт в корне репо на `codex/krab-ear-v2`, отдаётся через `https://raw.githubusercontent.com/Pavua/Krab-Ear/codex/krab-ear-v2/appcast.xml` (HTTPS, бесплатно, без GitHub Pages).

## Компоненты

### 1. One-time setup (руками владельца/сессии, один раз)

- Экспорт сертификата «Krab Ear Dev Local» в `.p12` с паролем → GitHub Secrets: `MACOS_CERT_P12` (base64), `MACOS_CERT_PASSWORD`.
- Генерация пары Sparkle EdDSA (`generate_keys` из Sparkle-дистрибутива) → приватный ключ в Secret `SPARKLE_PRIVATE_KEY`; **публичный** ключ — в `Info.plist` (`SUPublicEDKey`, не секрет, коммитится).
- Sparkle добавляется SPM-зависимостью в `native/KrabEarAgent/Package.swift` (официальный `sparkle-project/Sparkle`, версия 2.x).

### 2. Client-side (Swift-агент)

- `SPUStandardUpdaterController(startingUpdater: true, ...)` — программная инициализация в `AgentAppDelegate`, реальный вызов из `completeStartupAfterBackendReady()` (урок сессии: setup-функция без call site = декоративная проводка; сразу добавить source-контракт тест по образцу `test_setupErrorBus_is_actually_called_from_startup`).
- Пункт «Проверить обновления…» в статус-бар меню (`main+StatusMenu.swift`) — у приложения `LSUIElement=true`, обычного App-меню нет.
- `Info.plist` (в `Krab Ear.app/Contents/` и в staging-ассемблере DMG-скрипта): `SUFeedURL` (raw-URL appcast), `SUPublicEDKey`, `SUEnableAutomaticChecks=true` (интервал — суточный дефолт Sparkle, свой ключ не задаём).
- UI — стандартный Sparkle-диалог (release notes, Install / Skip / Remind Later): ноль кастомного UI-кода. Если позже захочется вписать диалог в Liquid Glass тему — это отдельная agy-задача, НЕ в этой волне (YAGNI).

### 3. CI release-workflow (`.github/workflows/release.yml`, новый)

Триггеры: `push: tags: ['v*.*.*']` и `workflow_dispatch` (input `version`). Runner: `macos-14` (arm64). Шаги:

1. **Валидация**: версия матчит semver `vX.Y.Z` И строго больше последней версии в appcast.xml (монотонность — требование Sparkle), иначе fail до сборки.
2. **CI-green guard**: через `gh api` проверить, что на собираемом коммите `krab-ear-ci` (ubuntu backend-tests — реальный гейт) завершился success; иначе fail-closed. Собираемый коммит: для тега — коммит под тегом; для `workflow_dispatch` — HEAD `codex/krab-ear-v2` (workflow_dispatch собирает именно эту ветку).
3. Импорт `.p12` во временный keychain раннера (стандартный паттерн `security create-keychain` / `security import`).
4. `swift build -c release` → **ассемблирование `.app` через существующую логику `scripts/build_distribution_dmg.command`** (переиспользовать/вынести shared-функцию, не дублировать: Info.plist, Resources с `bootstrap_backend.command`). Зип строится из свежесобранного бандла — НЕ из закоммиченного parity-бинаря.
5. Штамп версии из тега в `CFBundleShortVersionString` + `CFBundleVersion` (Sparkle сравнивает версии — обязаны монотонно расти; механика `--version` уже есть в DMG-скрипте).
6. `codesign` identity «Krab Ear Dev Local» (та же, что локально — TCC-грант владельца переживает обновление: тот же designated requirement, суть PR #235).
7. `ditto -c -k --keepParent` → `Krab-Ear-vX.Y.Z.zip`.
8. `sign_update` (Sparkle-тул) → `sparkle:edSignature` + `length`.
9. `gh release create vX.Y.Z` с zip-ассетом (+ sha256).
10. Обновить `appcast.xml` (добавить `<item>`: версия, дата, URL ассета, edSignature, length) → коммит в `codex/krab-ear-v2` с **`[skip ci]`** (иначе каждый релиз гоняет полный CI впустую) через встроенный `GITHUB_TOKEN` (`permissions: contents: write`).

### 4. Error handling / fail-closed

- Любой провал (сборка, codesign, sign_update, guard) → workflow красный, НИЧЕГО не публикуется. Неподписанный/непроверенный zip не может попасть в релиз.
- Аварийный откат плохого релиза: новый тег с большей версией (Sparkle не даунгрейдит); в крайнем случае — ручная правка appcast.xml (удалить item) + удаление релиза.

## Границы и не-цели

- **Не решает** доверие на чужих Mac: self-signed = статус-кво DMG (right-click→Open при первой установке). Sparkle-обновления на таких машинах работают после первой установки (EdDSA-подпись проверяет сам Sparkle). Полноценный Gatekeeper-путь = Apple Developer ID (отложен, за владельцем).
- **Не кастомизируем** Sparkle UI в этой волне.
- Обновляется ТОЛЬКО Swift-агент (.app). Python-backend живёт в каталоге проекта и обновляется git pull'ом / bootstrap-инсталлятором — вне скоупа (несовпадение версий agent↔backend уже смягчено `handshake` IPC с логом version mismatch).

## Тестирование

- Unit: генератор appcast-item (валидный XML, обязательные поля) — если генерация будет скриптом в репо (python/bash), тест в обычном CI.
- Source-контракт: Sparkle-инициализация реально вызывается из `completeStartupAfterBackendReady()` (грep main.swift, класс `MainErrorsWiringTests`).
- Smoke: первый прогон workflow через `workflow_dispatch` с тестовой версией; проверка что релиз+appcast корректны.
- Живой e2e (владелец): установленное приложение видит обновление, Sparkle-диалог ставит его, TCC-гранты живы, wake word/hotkey работают после обновления.

## Риски

| Риск | Митигация |
|---|---|
| Приватный ключ подписи в GH Secrets | Осознанно принят; значим только для машин, доверяющих self-signed сертификату |
| appcast-коммит триггерит CI | `[skip ci]` |
| Версия не растёт монотонно | Валидация в workflow: новая версия > последней в appcast |
| Тегнули красный коммит | CI-green guard (шаг 2) |
| raw.githubusercontent кэш (~5 мин) | Некритично для суточной проверки обновлений |
