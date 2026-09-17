# Убрать декоративное меню «Update Channel» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** удалить из статус-меню пункт «Update Channel» со Stable/Beta-подменю и его мёртвые обработчики — при единственном appcast выбор канала ни на что не влияет.

**Architecture:** удаление, не поведение: пункт меню (12 строк) + 2 `@objc`-хендлера (14 строк). 🔴 Сохранить обязательно: поле `Models.updateChannel` + `toPayload`/`fromPayload` (`Models.swift:92,194,299,380,459,541`) — бэкенд присылает и валидирует `update_channel` (`settings_service.py:480`, `settings_validator.py:70`, `DEFAULT_SETTINGS`), удаление поля сломало бы round-trip настроек. Сохранить: пункт «Проверить обновления…» и весь `main+SparkleUpdater.swift`.

**Tech Stack:** Swift, AppKit NSMenu. Визуал не меняется (пункт просто исчезает) — agy не нужен.

**База:** `origin/codex/krab-ear-v2`. Worktree: `.worktrees/remove-update-channel-menu`, ветка `fix/remove-update-channel-menu`.

**Баны:** список из [`EXECUTOR_PLAYBOOK.md`](../../EXECUTOR_PLAYBOOK.md) §1 целиком. Дополнительно: **не запускать собранный `KrabEarAgent`** (только `swift build`); не трогать `Models.swift`, бэкенд, appcast/Info.plist; нет новых `runModal()`, нет новых Unicode-глифов (только удаление).

---

## Проверенные факты (координатор, 17.09, file:line)

- `native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift:555-566` — блок меню (12 строк, текст ниже — сверить `sed -n '555,566p'` перед удалением).
- `native/KrabEarAgent/Sources/KrabEarAgent/main.swift:1072-1085` — хендлеры `onUpdateChannelStable`/`onUpdateChannelBeta` (только пишут settings + persist + rebuild + notify; селекторы referenced ТОЛЬКО из удаляемого меню — проверено `rg onUpdateChannel`).
- `main+SparkleUpdater.swift:61` — канал нигде не читается, только `SUFeedURL` из Info.plist (appcast один).
- Swift-тесты меню не покрывают (`rg` по `*Tests/` пуст — перепроверить в Task 1).
- Parity-бинарь `Krab Ear.app` / `native/runtime` исполнитель НЕ кладёт (координатор после гейта).

---

### Task 1: Удаление (единственный шаг кода)

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift` (удалить 12 строк)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (удалить 14 строк)

- [ ] **Step 1: Напечатать оба региона и сверить с карточкой** (`sed -n '555,566p' ...StatusMenu.swift`, `sed -n '1070,1087p' main.swift`). Любое расхождение — **стоп**, доложить координатору.

- [ ] **Step 2: Удалить из `main+StatusMenu.swift`** ровно этот блок:

```swift
        let updateChannelItem = NSMenuItem(title: "Update Channel", action: nil, keyEquivalent: "")
        menu.addItem(updateChannelItem)
        let updateChannelSubmenu = NSMenu()
        let stableChannelItem = NSMenuItem(title: "Stable", action: #selector(onUpdateChannelStable), keyEquivalent: "")
        stableChannelItem.target = self
        stableChannelItem.state = settings.updateChannel == "stable" ? .on : .off
        updateChannelSubmenu.addItem(stableChannelItem)
        let betaChannelItem = NSMenuItem(title: "Beta", action: #selector(onUpdateChannelBeta), keyEquivalent: "")
        betaChannelItem.target = self
        betaChannelItem.state = settings.updateChannel == "beta" ? .on : .off
        updateChannelSubmenu.addItem(betaChannelItem)
        menu.setSubmenu(updateChannelSubmenu, for: updateChannelItem)
```

- [ ] **Step 3: Удалить из `main.swift`** ровно этот блок:

```swift
    @objc func onUpdateChannelStable() {
        settings.updateChannel = "stable"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        notify(title: "Krab Ear", body: "Канал обновлений: stable")
    }

    @objc func onUpdateChannelBeta() {
        settings.updateChannel = "beta"
        persistSettingsPayload(settings.toPayload())
        rebuildStatusMenu()
        notify(title: "Krab Ear", body: "Канал обновлений: beta")
    }
```

### Task 2: Гейт

- [ ] **Step 1: Ноль ссылок**

```bash
rg -n 'onUpdateChannel|Update Channel' native/KrabEarAgent/Sources/
```

Ожидаемо: пусто (скоп — только сорсы: закоммиченный parity-бинарь `native/runtime/KrabEarAgent` содержит старые символы и в скоп НЕ входит). Затем:

```bash
rg -n 'updateChannel' native/KrabEarAgent/Sources/
```

Ожидаемо: только `Models.swift` (поле + payload round-trip — так и должно быть).

- [ ] **Step 2: Сборка**

```bash
cd native/KrabEarAgent && swift build -c release
```

Ожидаемо: BUILD COMPLETE, без новых warnings про меню (сверить с `.remember/swift-warnings-baseline.txt` при сомнении).

- [ ] **Step 3: Swift-тесты пакета** (удаление затрагивает меню — гоняем целиком, это единственный Swift-гейт волны):

```bash
cd native/KrabEarAgent && swift test 2>&1 | tail -5
```

Ожидаемо: все тесты зелёные. Если пакет тестов идёт дольше 20 минут — зафиксировать в отчёте и прогнать хотя бы таргет с menu-тестами (найти `rg -ln 'Menu' KrabEarAgentTests/`).

### Task 3: Коммит и PR

- [ ] `git branch --show-current` → `fix/remove-update-channel-menu`
- [ ] `git add` **явными путями**: два Swift-файла
- [ ] Коммит: `fix(ui): убрать декоративное меню Update Channel`
- [ ] PR в `codex/krab-ear-v2`; НЕ мержить (мержит координатор после гейта).

## Definition of Done

- `swift build -c release` зелёный; ноль ссылок на удалённое; Swift-тесты зелёные.
- `Models.updateChannel`, бэкенд, appcast, «Проверить обновления…» не тронуты (`git diff --stat` — только 2 файла, только deletions).
- Собранный агент не запускался; parity-бинарь не кладён.

## Вне scope (записать в отчёт, не чинить)

Если `swift test` найдёт красные тесты, не связанные с меню (флаки/окружение) — списком в отчёт, не чинить. Backend-ключ `update_channel` остаётся валидным (бэкенд не трогаем).
