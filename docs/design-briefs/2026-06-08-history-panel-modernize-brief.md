# ТЗ для Antigravity (agy / Gemini 3.1 Pro) — визуальная модернизация History-панели Krab Ear

> **Дата:** 2026-06-08 · **Автор ТЗ:** Claude · **Исполнитель:** `agy` (Antigravity, Google AI Pro), model `Gemini 3.1 Pro (High)`
> **Граница:** инженерный контракт + ограничения здесь; **визуальный язык решает Gemini.**

## 0. Цель

Таблица истории сейчас — голый `NSTableView` с 3 колонками (Время/Вставка/Текст) на **дефолтных string-cell'ах** (кастомного `NSTableCellView` нет, строки 28pt, без alternating rows). Выглядит как сырой grid. Нужно: современные **кастомные строки** в стиле Liquid Glass — читаемая иерархия (текст транскрипта крупно, дата/статус/уверенность — вторично, аккуратными бейджами), и подтянуть визуал filters/search/secondary-action баров. Сохранить ВСЮ логику данных/выделения/копирования. Один проход.

## 1. 🔴 ЖЁСТКИЕ ОГРАНИЧЕНИЯ

1. **НЕ менять модель данных и data source:** `items: [HistoryItem]` (HistoryPanelController.swift:83), `numberOfRows(in:)`, идентификаторы колонок, фильтрацию/поиск, пагинацию (`loadMore`/`loadAll` в +History.swift). Можно/нужно добавить `tableView(_:viewFor:row:)` с кастомным `NSTableCellView` — это штатный способ стилизации строк — но `HistoryItem`-поля и порядок строк не трогать.
2. **НЕ ломать выделение/копирование/действия:** primaryActionsRow (copy, pasteSelected, delete, jumpToLatest, loadMore), выбор строки (`selectedRow`), контекстные действия. Row height можно увеличить, но selection/keyboard navigation должны работать.
3. **НЕ переименовывать** существующие переменные/`@objc` обработчики (`tableView`, `searchField`, `historyFiltersSection`, `historyOverviewLabel`, `historyStatusLabel`, кнопки рядов).
4. **НЕ менять** `sectionId` у CollapsibleSectionView в History-табе (персистентность). НЕ трогать backend/Python/тесты/main.swift проводку.
5. **Только токены `KrabEarTheme.*`** (см. раздел 3), без хардкод чисел цвета/шрифта/отступа. Новый токен — добавить в KrabEarTheme.swift в том же стиле.
6. **Анимации** только через `KrabEarTheme.Motion.animate`. **Любые NSAlert/NSOpenPanel/NSSavePanel — только `presentAlertSheet`/`presentPanelSheet` из AlertHelpers.swift, НИКОГДА `runModal()`** (CI-гард).
7. **SF Symbols, не Unicode-глифы** (был баг рендера Unicode в системном шрифте — AGENT-J).
8. **Build с первого раза:** `cd native/KrabEarAgent && swift build -c release` зелёный, без новых warning'ов. Если ошибки — чинить и пересобирать до зелёного.

## 2. Файлы

**Редактировать:**
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift` — `tableView` setup (~строки 131, 1431-1465: колонки, rowHeight, delegate/dataSource). Добавить кастомный cell view + `viewFor`.
- `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+ApplyTheme+HistoryTab.swift` — сборка History-таба (filters, search card, table card, action rows).
- `native/KrabEarAgent/Sources/KrabEarAgent/KrabEarTheme.swift` — при необходимости новые токены / переиспользуемый row-cell класс.

**Можно:** `HistoryPanelController+History.swift` (если row-rendering требует helper'а). **НЕ трогать:** `+HistoryEnhancements.swift` логику кнопок (только если визуальный ряд), backend, тесты.

## 3. Theme-токены (только эти)

**Colors:** windowBackground, cardBackground, accent, textPrimary, textSecondary, textDisabled, border, separator, success, error, warning, overlayShadow.
**Typography:** display(17 reg), sectionTitle(13 semibold), body(13 reg), caption(11 reg), captionMedium(11 medium), monospace(11 mono) — у Typography есть `.tabular()` для цифр.
**Metrics:** tight(4), standard(8), comfortable(12), spacious(24), cardCornerRadius(12), innerCornerRadius(8), controlHeight(24).
**Motion:** Duration.{micro .15, short .25, standard .40, long .70}, Easing.{easeOut…}, `Motion.animate(duration:easing:animations:)`.
**Interaction:** hoverOverlayAlpha .10, pressedScale .98, pressedOverlayAlpha .15, disabledOpacity .40.
**Elevation:** applyCard(to:), applyPopup(to:). **Компоненты:** ThemeCardView, CollapsibleSectionView, ThemePrimaryButton/ThemeSecondaryButton.

## 4. Направление (ЧТО лучше — КАК решает Gemini)

`HistoryItem` поля: `id, ts, text, pasteStatus, sourceText, translatedText, translationMode, translationStatus, confidence, actionItems, decisions, questions` (Models.swift:392-445).

1. **Кастомная строка** (`NSTableCellView` подкласс): текст транскрипта — ведущая строка (`body`, до 2 строк, truncate), под/рядом — мета: дата (`caption`/`monospace.tabular`, `textSecondary`), бейдж paste-статуса (ok=`success`/failed=`error`/pending=`warning`, капсула как в Settings-редизайне), бейдж уверенности (`confidence` → процент, цвет по порогу), индикатор перевода если `translationMode != off`. Увеличить rowHeight под двухстрочную компоновку.
2. **Единый бейдж-компонент** — переиспользовать стиль капсул из Settings-редизайна (`captionMedium.tabular`, фон token-цвета с пониженной альфой, опц. SF Symbol). Если он вынесен в helper — переиспользовать; иначе сделать общий.
3. **Hover/selection** строки — мягкая подсветка через Interaction-токены; выделение читаемое на Liquid Glass (полупрозрачный accent fill, не системный синий поверх стекла).
4. **Filters / search / quick-presets бары** — выровнять ритм (`Metrics.standard`), search-поле и пресет-кнопки в карточке (ThemeCardView), иконки пресетов SF Symbols.
5. **Empty-state** (история пуста / фильтр без результатов) — аккуратная заглушка: SF Symbol + `caption` подпись `textSecondary`, по центру, вместо пустого grid.
6. **Заголовок/итоги** (`historyOverviewLabel`/`historyStatusLabel`) — типографика по токенам, цифры `monospace.tabular`.

## 5. Критерии приёмки

- [ ] `swift build -c release` зелёный, без новых warning'ов.
- [ ] Data source/выделение/копирование/пагинация/поиск работают как раньше (имена/обработчики целы).
- [ ] sectionId History-секций неизменны; нет новых `runModal()`; токены вместо хардкода; анимации через Motion.animate; SF Symbols.
- [ ] Строки показывают текст+дату+бейджи статуса/уверенности; empty-state присутствует.

## 6. Формат ответа

Полные изменённые `.swift` файлы (или точные диффы) + вставки новых токенов/классов в KrabEarTheme.swift. Один проход, без вопросов — при неоднозначности выбирать максимально согласованный с Liquid Glass и токенами вариант. После — Claude review + `swift build` + bundle parity.
