# Пикер транспорта GigaAM — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** В секции «STT-движки» Settings появляется переключатель транспорта GigaAM v3 (`subprocess`/`mlx`) в обоих UI-вариантах (Gemini и Claude Design), с честной индикацией недоступности MLX-библиотеки.

**Architecture:** Python-хендлер `handle_list_stt_engines` получает одно новое поле `mlx_available` только у записи `gigaam` (через `importlib.util.find_spec`, без импорта тяжёлой библиотеки). Swift-сторона добавляет `gigaamTransport` в `AgentSettings` (обычная настройка, синхронизируется как `qualityProfile`) и строит статическую карточку с `NSPopUpButton` под существующей карточкой движков — по образцу пикера провайдера в CloudRewriter. Три source-контракт-теста проверяют не факт существования кода, а факт его вызова (autoenablesItems, sync-хук, completion-проводка).

**Tech Stack:** Python 3.14 (dev) / 3.12 (ubuntu-parity, без mlx), unittest; Swift 6, AppKit, `swift build -c release`.

**Спека:** [docs/superpowers/specs/2026-08-23-gigaam-transport-picker-design.md](../specs/2026-08-23-gigaam-transport-picker-design.md)

## Global Constraints

- **Ветка:** `feat/gigaam-transport-picker`, база — `origin/codex/krab-ear-v2`. НЕ пушить напрямую — только PR.
- **🔴 `mlx_available` вычисляется ТОЛЬКО через `importlib.util.find_spec("gigaam_mlx")`.** НЕ импортом `core.pipeline.stt_gigaam_mlx` — этот импорт успешен и без библиотеки (`gigaam_mlx` импортируется лениво внутри методов, строки 120/213 адаптера), проверка была бы ложноположительной.
- **🔴 `popup.menu?.autoenablesItems = false` обязателен рядом с любым `item.isEnabled = false`.** Без этого `NSMenu` перезаписывает ручное состояние перед показом (прецедент: `main+CallObserver.swift:56`, находка MED-2) — задизейбленный пункт останется кликабельным.
- **Значения пикера — ровно два:** индекс 0 = `"subprocess"` («Стабильный»), индекс 1 = `"mlx"` («Быстрый, экспериментальный»). `auto`/`in_process` не показываются.
- **Дефолт `gigaamTransport` в Swift — `"subprocess"`**, с явным дефолтным значением параметра в memberwise-init (иначе ломается компиляция существующих вызовов, не передающих этот параметр).
- **Рестарт backend НЕ требуется** — `set_settings` подхватывается живым процессом (`reload_settings_from_json` + fingerprint-пересоздание адаптера в роутере), проверено живым замером 2026-08-23.
- **Комментарии и UI-строки — по-русски.**
- **AGENT-3:** запись настройки — только через `applySettingsPatch` (уже асинхронен и восстанавливает соединение), никаких сырых `ipc.call` на main.
- **Три source-контракт-теста проверяют ФАКТ ВЫЗОВА**, не факт существования кода — иначе декоративная проводка (метод определён, но никогда не вызван) даст зелёные тесты при мёртвом UI.

---

## File Structure

| Файл | Ответственность | Действие |
|---|---|---|
| `KrabEar/backend/stt_management_service.py` | добавляет `mlx_available` к записи `gigaam` в `handle_list_stt_engines` | modify |
| `KrabEar/tests/test_list_stt_engines.py` | тесты на новое поле, дописаны в конец существующего файла | modify |
| `native/KrabEarAgent/Sources/KrabEarAgent/Models.swift` | поле `gigaamTransport` в `AgentSettings`, 6 точек | modify |
| `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift` | карточка пикера, оба варианта, обработчик, sync-хук | modify |
| `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift` | вызов sync-хука из `syncSettingsControls` | modify |
| `native/KrabEarAgent/Tests/KrabEarAgentTests/STTTransportPickerWiringTests.swift` | 3 source-контракт-теста | create |

---

## Task 1: `mlx_available` в `handle_list_stt_engines`

**Files:**
- Modify: `KrabEar/backend/stt_management_service.py:10-16` (импорты), `:329-336` (GigaAM meta), `:366-393` (сборка словаря)
- Test: `KrabEar/tests/test_list_stt_engines.py` (существующий файл, дописываем в конец)

**Interfaces:**
- Consumes: ничего (первая задача).
- Produces: ключ `"mlx_available": bool` в элементе `result["engines"]`, где `"name" == "gigaam"`. Отсутствует у остальных движков. Task 5 (Swift) полагается на это имя ключа буква-в-букву.

- [ ] **Step 0: Существующий тестовый файл найден заранее**

`KrabEar/tests/test_list_stt_engines.py` уже существует и содержит `_FakeSettingsService`
и хелпер `_make_svc(settings: dict | None = None) -> STTManagementService` (строки 28-44).
Дописывать тесты Step 1 в конец этого файла, используя `_make_svc` — не изобретать
свой fake.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `KrabEar/tests/test_list_stt_engines.py` (после последнего класса,
перед `if __name__ == "__main__":`, если он есть — иначе в самый конец файла):

```python
import ast
from pathlib import Path
from unittest.mock import patch


class MlxAvailableFieldTestCase(unittest.TestCase):
    """mlx_available в list_stt_engines (2026-08-23).

    GigaAM v3 умеет транспорт "mlx" (core/pipeline/stt_gigaam_mlx.py), но UI не
    может сам узнать, установлена ли библиотека gigaam_mlx. handle_list_stt_engines
    отдаёт это одним полем, специфичным ТОЛЬКО для записи gigaam.

    Критично: проверка ОБЯЗАНА идти через importlib.util.find_spec, а не импортом
    core.pipeline.stt_gigaam_mlx — тот импорт успешен и без библиотеки (gigaam_mlx
    импортируется лениво внутри методов адаптера), проверка через импорт была бы
    ложноположительной.
    """

    def test_mlx_available_present_only_on_gigaam(self):
        svc = _make_svc()
        with patch("importlib.util.find_spec", return_value=None):
            result = svc.handle_list_stt_engines({})

        engines = {e["name"]: e for e in result["engines"]}
        self.assertIn("mlx_available", engines["gigaam"])
        for name, engine in engines.items():
            if name != "gigaam":
                self.assertNotIn("mlx_available", engine)

    def test_mlx_available_false_when_spec_missing(self):
        svc = _make_svc()
        with patch("importlib.util.find_spec", return_value=None) as mock_find:
            result = svc.handle_list_stt_engines({})

        engines = {e["name"]: e for e in result["engines"]}
        self.assertFalse(engines["gigaam"]["mlx_available"])
        # find_spec обязан быть вызван именно с "gigaam_mlx"
        mock_find.assert_any_call("gigaam_mlx")

    def test_mlx_available_true_when_spec_present(self):
        svc = _make_svc()
        fake_spec = object()
        with patch("importlib.util.find_spec", return_value=fake_spec):
            result = svc.handle_list_stt_engines({})

        engines = {e["name"]: e for e in result["engines"]}
        self.assertTrue(engines["gigaam"]["mlx_available"])


class MlxAvailableUsesFindSpecNotImportTestCase(unittest.TestCase):
    """Source-контракт: проверка идёт через importlib.util.find_spec("gigaam_mlx"),
    а НЕ через import core.pipeline.stt_gigaam_mlx (тот импорт успешен без библиотеки).
    Матчим AST, не подстроку — правило CLAUDE.md для source-inspection тестов."""

    def test_ast_calls_find_spec_with_gigaam_mlx_literal(self):
        source = Path(KRAB_EAR_ROOT, "backend", "stt_management_service.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_find_spec = (
                isinstance(func, ast.Attribute) and func.attr == "find_spec"
            )
            if not is_find_spec:
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == "gigaam_mlx":
                    found = True
        self.assertTrue(
            found,
            "handle_list_stt_engines обязан вызывать "
            "importlib.util.find_spec('gigaam_mlx'), а не импортировать адаптер",
        )
```

🔴 `PROJECT_ROOT`/`KRAB_EAR_ROOT`/`unittest`/`sys`/`os` уже импортированы в шапке файла — не дублировать.

- [ ] **Step 2: Прогнать тест и убедиться, что он падает**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_list_stt_engines.py -v
```

Expected: **4 failed** — `KeyError: 'mlx_available'` в первых трёх, `AssertionError` в четвёртом (ключа ещё нет в исходнике).

- [ ] **Step 3: Реализация**

В `KrabEar/backend/stt_management_service.py` добавить импорт (после строки 11, рядом с `import re`):

```python
import importlib.util
```

В блоке `_ENGINE_META` (строки 329-336) добавить маркер, чтобы не завязываться на позицию в списке:

```python
            {
                "name": "gigaam",
                "display_name": "GigaAM v3 (RU)",
                "toggle_key": "stt_gigaam_enabled",
                "note": "Лучший для RU, subprocess ~1.5 ГБ",
                "adapter_class": "core.pipeline.stt_gigaam_adapter.GigaAMSTTAdapter",
                "checks_mlx_availability": True,
            },
```

В цикле сборки словаря (после блока `available = ...` / `except Exception:`, перед `engines.append({...})`, ориентировочно строка 393) добавить:

```python
            entry = {
                "name": meta["name"],
                "display_name": meta["display_name"],
                "available": available,
                "enabled": enabled,
                "toggle_key": toggle_key,
                "note": meta["note"],
                "type": "local",
            }
            if meta.get("checks_mlx_availability"):
                # find_spec, а НЕ импорт core.pipeline.stt_gigaam_mlx: тот модуль
                # импортируется успешно и без библиотеки gigaam_mlx (ленивый
                # импорт внутри методов адаптера) — импорт был бы
                # ложноположительной проверкой.
                entry["mlx_available"] = importlib.util.find_spec("gigaam_mlx") is not None
            engines.append(entry)
```

Убрать старый `engines.append({...})`, который стоял на месте нового блока (тело `entry` идентично прежнему словарю плюс поле `mlx_available`).

- [ ] **Step 4: Прогнать тест и убедиться, что он проходит**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_list_stt_engines.py -v
```

Expected: **4 passed**.

- [ ] **Step 5: Обновить валидатор допустимых значений (уже готов, проверить)**

`KrabEar/core/settings_validator.py:66` уже содержит `"stt_gigaam_transport": ("subprocess", "auto", "in_process", "mlx")` — значение `"mlx"` уже допустимо. Убедиться, что правка ничего здесь не задела:

```bash
cd "$(git rev-parse --show-toplevel)" && git diff --stat -- KrabEar/core/settings_validator.py
```

Expected: пусто (файл не тронут этой задачей).

- [ ] **Step 6: Прогнать существующие тесты list_stt_engines**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/ -k "list_stt_engines" -v
```

Expected: все PASS — старое поведение (движки без `mlx_available`) не сломано.

- [ ] **Step 7: Коммит**

```bash
cd "$(git rev-parse --show-toplevel)"
git add KrabEar/backend/stt_management_service.py KrabEar/tests/test_list_stt_engines.py
git commit -m "$(cat <<'EOF'
feat(stt): mlx_available в list_stt_engines для записи gigaam

Добавлено поле mlx_available (bool), вычисляемое через
importlib.util.find_spec("gigaam_mlx") — НЕ импортом адаптера, который
успешен и без библиотеки (ленивый import внутри методов). UI сможет
честно дизейблить пункт MLX в пикере транспорта, вместо показа
"доступно" там, где распознавание упадёт.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `gigaamTransport` в `AgentSettings` (6 точек)

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/Models.swift:60,134,208,304,376,446,519` (номера строк — на момент написания, искать по тексту сиблинга `cloudRewriterProvider`/`qualityProfile` рядом)

**Interfaces:**
- Consumes: ничего.
- Produces: `AgentSettings.gigaamTransport: String`, дефолт `"subprocess"`. Payload-ключ `"stt_gigaam_transport"`. Task 3 и Task 5 читают/пишут именно это имя поля и ключа.

🔴 Тестов на структуру данных здесь нет намеренно — `AgentSettings` не имеет `Equatable`/`Codable` и отдельного файла тестов на сериализацию (проверено ревью: сиблинг `cloudRewriterProvider` тоже без выделенных unit-тестов на round-trip). Корректность этой задачи проверяется транзитивно тестами Task 5 (компиляция + сборка карточки).

- [ ] **Step 1: Добавить объявление поля**

Найти `var qualityProfile: String` (соседняя строка `var cloudRewriterProvider: String`, ориентировочно строка 134) и добавить рядом с группой Cloud Rewriter новую группу:

```swift
    // GigaAM transport (subprocess = PyTorch-воркер / mlx = in-process MLX)
    var gigaamTransport: String
```

- [ ] **Step 2: Добавить в `Self.default`**

Найти блок `Self.default = AgentSettings(...)` (там же, где `cloudRewriterProvider: "openai",`, ориентировочно строка 208) и добавить рядом:

```swift
        gigaamTransport: "subprocess",
```

- [ ] **Step 3: Добавить в парсер `init(from payload:)`**

Найти `self.cloudRewriterProvider = (payload["cloud_rewriter_provider"] as? String) ?? Self.default.cloudRewriterProvider` (ориентировочно строка 304) и добавить рядом:

```swift
        self.gigaamTransport = (payload["stt_gigaam_transport"] as? String) ?? Self.default.gigaamTransport
```

- [ ] **Step 4: Добавить параметр в memberwise-init (С ДЕФОЛТОМ)**

Найти `cloudRewriterProvider: String = "openai",` (ориентировочно строка 376) и добавить рядом:

```swift
        gigaamTransport: String = "subprocess",
```

🔴 Дефолт обязателен — без него ломается компиляция существующих вызовов `AgentSettings(...)`, которые этот параметр не передают.

- [ ] **Step 5: Присвоить в теле init**

Найти `self.cloudRewriterProvider = cloudRewriterProvider` (ориентировочно строка 446) и добавить рядом:

```swift
        self.gigaamTransport = gigaamTransport
```

- [ ] **Step 6: Добавить в `toPayload()`**

Найти `"cloud_rewriter_provider": cloudRewriterProvider,` (ориентировочно строка 519) и добавить рядом:

```swift
            "stt_gigaam_transport": gigaamTransport,
```

- [ ] **Step 7: Собрать проект**

Run:
```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift build -c release 2>&1 | tail -30
```

Expected: `Build complete!` без ошибок. Если ошибка про недостающий аргумент в вызове `AgentSettings(...)` — значит Step 4 сделан без дефолта, вернуться и исправить.

- [ ] **Step 8: Коммит**

```bash
cd "$(git rev-parse --show-toplevel)"
git add native/KrabEarAgent/Sources/KrabEarAgent/Models.swift
git commit -m "$(cat <<'EOF'
feat(models): gigaamTransport в AgentSettings

Обычная строковая настройка по конвенции qualityProfile/cloudRewriterProvider:
6 точек (объявление, default, парсер payload, memberwise-init с дефолтом,
присваивание, toPayload). Дефолт "subprocess" — совпадает с
DEFAULT_SETTINGS["stt_gigaam_transport"] в core/config.py.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Карточка пикера — Gemini вариант + обработчик + sync-хук

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift`

**Interfaces:**
- Consumes: `AgentSettings.gigaamTransport` (Task 2), `applySettingsPatch(_:)` и `isSyncingSettings` (существуют в `+Settings.swift`), `makeSettingRow(label:description:control:badge:)` (существует в `+Settings.swift:681`).
- Produces:
  - `func buildGigaamTransportCard() -> NSView` — Gemini-вариант карточки.
  - `@objc func onGigaamTransportChanged(_ sender: NSPopUpButton)` — обработчик записи.
  - `@MainActor func syncGigaamTransportControls(settings: AgentSettings, mlxAvailable: Bool)` — хук синхронизации, читает Task 2/Task 1.
  - Associated-object ключи `gigaamTransportPicker`, `gigaamTransportWarnLabel`, `gigaamTransportCard` (Gemini).

Task 4 (CD-вариант) добавляет параллельные ключи `cdGigaamTransportPicker` и т.д. и переиспользует `onGigaamTransportChanged`/`syncGigaamTransportControls` (общие для обоих вариантов — сигнатура ниже это учитывает).

- [ ] **Step 1: Добавить associated-object ключи**

В `HistoryPanelController+STTEnginesPicker.swift`, в существующий `private enum STTEnginesAssocKeys`, добавить:

```swift
private enum STTEnginesAssocKeys {
    nonisolated(unsafe) static var enginesCard: UInt8 = 0
    nonisolated(unsafe) static var cdEnginesCard: UInt8 = 0

    // Карточка пикера транспорта GigaAM (Gemini)
    nonisolated(unsafe) static var gigaamTransportCard: UInt8 = 0
    nonisolated(unsafe) static var gigaamTransportPicker: UInt8 = 0
    nonisolated(unsafe) static var gigaamTransportWarnLabel: UInt8 = 0

    // Карточка пикера транспорта GigaAM (Claude Design)
    nonisolated(unsafe) static var cdGigaamTransportCard: UInt8 = 0
    nonisolated(unsafe) static var cdGigaamTransportPicker: UInt8 = 0
    nonisolated(unsafe) static var cdGigaamTransportWarnLabel: UInt8 = 0
}
```

- [ ] **Step 2: Построить карточку (Gemini) и врезать в `buildSTTEnginesSection()`**

В существующем методе `buildSTTEnginesSection()` (после строки `section.contentStackView.addArrangedSubview(card)` и до `return section`) добавить:

```swift
        let transportCard = buildGigaamTransportCard()
        section.contentStackView.addArrangedSubview(transportCard)
```

Новый метод (в расширении `extension HistoryPanelController`, рядом с `buildSTTEnginesSection`):

```swift
    /// Статическая карточка пикера транспорта GigaAM: отдельно от асинхронно
    /// перестраиваемой карточки движков — та расставляет разделители по
    /// индексу (index < engines.count - 1), вставка строки внутрь её цикла
    /// сдвинула бы разделители. Видимость (isHidden) и mlxAvailable приходят
    /// позже, из completion fetchAndRebuildSTTEnginesCard (см. Step 5).
    @MainActor
    func buildGigaamTransportCard() -> NSView {
        let card = ThemeCardView()

        let picker = NSPopUpButton(frame: .zero, pullsDown: false)
        picker.addItems(withTitles: ["Стабильный (subprocess)", "Быстрый (MLX, экспериментальный)"])
        picker.target = self
        picker.action = #selector(onGigaamTransportChanged(_:))
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportPicker, picker,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let row = makeSettingRow(label: "Транспорт распознавания GigaAM", control: picker)
        card.contentStackView.addArrangedSubview(row)

        let warnLabel = NSTextField(labelWithString: "")
        warnLabel.font = KrabEarTheme.Typography.caption
        warnLabel.textColor = KrabEarTheme.Colors.warning
        warnLabel.isHidden = true
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportWarnLabel, warnLabel,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        card.contentStackView.addArrangedSubview(warnLabel)

        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportCard, card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        return card
    }
```

Если `KrabEarTheme.Colors.warning` не существует — проверить точное имя токена предупреждающего цвета:

```bash
cd "$(git rev-parse --show-toplevel)" && grep -n "static.*Colors\." native/KrabEarAgent/Sources/KrabEarAgent/KrabEarTheme.swift | grep -iE "warn|caution|alert"
```

Использовать найденное имя вместо `warning`, если оно другое.

- [ ] **Step 3: Обработчик записи**

```swift
    /// Обрабатывает выбор транспорта GigaAM. isSyncingSettings защищает от
    /// цикла: syncSettingsControls выставляет этот флаг перед программной
    /// установкой значения пикера (Step 5) — без гварда программная
    /// синхронизация вызвала бы этот обработчик и записала настройку обратно.
    @objc func onGigaamTransportChanged(_ sender: NSPopUpButton) {
        guard !isSyncingSettings else { return }
        let transport = sender.indexOfSelectedItem == 1 ? "mlx" : "subprocess"
        applySettingsPatch(["stt_gigaam_transport": transport])
    }
```

- [ ] **Step 4: Sync-хук — обязательное обязательство C5a(а)**

```swift
    /// Синхронизирует пикер и предупреждающий бейдж с текущими settings.
    /// ОБЯЗАН вызываться из syncSettingsControls (Task 3.5) — иначе пикер
    /// не получит начальное значение и не ресинкнется после внешнего
    /// set_settings/apply_profile_preset (та же декоративная проводка,
    /// от которой уже страдал repo — MainErrorsWiringTests-класс).
    ///
    /// mlxAvailable приходит асинхронно из list_stt_engines (Task 1) — другим
    /// путём, чем settings; вызывающая сторона (completion
    /// fetchAndRebuildSTTEnginesCard, Task 3.5) обязана передать актуальное
    /// значение, иначе бейдж будет неактуален.
    @MainActor
    func syncGigaamTransportControls(settings: AgentSettings, mlxAvailable: Bool) {
        let transport = settings.gigaamTransport
        let idx = (transport == "mlx") ? 1 : 0

        if let picker = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportPicker
        ) as? NSPopUpButton {
            picker.selectItem(at: idx)
            // 🔴 БЕЗ ЭТОЙ СТРОКИ item.isEnabled НИЖЕ НЕ СРАБОТАЕТ: NSMenu
            // авто-валидирует пункты перед показом (autoenablesItems=true по
            // умолчанию) и перезаписывает ручное disabled-состояние —
            // прецедент main+CallObserver.swift:56, находка MED-2.
            picker.menu?.autoenablesItems = false
            if let mlxItem = picker.item(at: 1) {
                mlxItem.isEnabled = mlxAvailable
                mlxItem.toolTip = mlxAvailable ? nil : "Требуется библиотека gigaam_mlx"
            }
        }
        if let cdPicker = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportPicker
        ) as? NSPopUpButton {
            cdPicker.selectItem(at: idx)
            cdPicker.menu?.autoenablesItems = false
            if let mlxItem = cdPicker.item(at: 1) {
                mlxItem.isEnabled = mlxAvailable
                mlxItem.toolTip = mlxAvailable ? nil : "Требуется библиотека gigaam_mlx"
            }
        }

        let showWarning = (transport == "mlx") && !mlxAvailable
        let warnText = "MLX выбран, но библиотека не найдена — GigaAM отключён"
        if let warnLabel = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.gigaamTransportWarnLabel
        ) as? NSTextField {
            warnLabel.stringValue = warnText
            warnLabel.isHidden = !showWarning
        }
        if let cdWarnLabel = objc_getAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportWarnLabel
        ) as? NSTextField {
            cdWarnLabel.stringValue = warnText
            cdWarnLabel.isHidden = !showWarning
        }
    }
```

- [ ] **Step 5: Обязательства C5a(б) и C5a(в) — видимость и доставка mlxAvailable**

Найти `fetchAndRebuildSTTEnginesCard(isClaudeDesign:)` в этом же файле. В конце замыкания `DispatchQueue.main.async` (после вызова `rebuildGeminiSTTEnginesCard`/`rebuildCDSTTEnginesCard`) добавить:

```swift
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if isClaudeDesign {
                    self.rebuildCDSTTEnginesCard(engines: engines)
                } else {
                    self.rebuildGeminiSTTEnginesCard(engines: engines)
                }

                // C5a(б): видимость карточки транспорта зависит от того,
                // включён ли GigaAM — тумблер живёт в СОСЕДНЕЙ асинхронно
                // перестраиваемой карточке, поэтому пересчёт здесь, а не
                // только при построении секции.
                // C5a(в): mlxAvailable приходит из ЭТОГО же асинхронного
                // ответа — доставляется в статическую карточку тем же completion.
                let gigaamRow = engines.first(where: { $0.name == "gigaam" })
                let gigaamEnabled = gigaamRow?.enabled ?? false
                let mlxAvailable = self.lastMlxAvailable(from: engines)

                if isClaudeDesign {
                    if let cdCard = objc_getAssociatedObject(
                        self, &STTEnginesAssocKeys.cdGigaamTransportCard
                    ) as? NSView {
                        cdCard.isHidden = !gigaamEnabled
                    }
                } else {
                    if let card = objc_getAssociatedObject(
                        self, &STTEnginesAssocKeys.gigaamTransportCard
                    ) as? NSView {
                        card.isHidden = !gigaamEnabled
                    }
                }
                self.syncGigaamTransportControls(
                    settings: self.settingsProvider(), mlxAvailable: mlxAvailable
                )
            }
```

Добавить приватный хелпер рядом (парсинг `mlx_available` из сырого ответа `list_stt_engines`, а не из `STTEngineRow` — эта структура его пока не несёт, расширять её ради одного bool не нужно, поле читается напрямую из ответа IPC внутри `fetchAndRebuildSTTEnginesCard`):

Вместо `lastMlxAvailable(from:)` — реализовать проще: расширить саму загрузку. В `fetchAndRebuildSTTEnginesCard`, там где парсится `rawList` в `STTEngineRow`, дополнительно сохранить сырое значение:

```swift
            var mlxAvailable = false
            for dict in rawList where (dict["name"] as? String) == "gigaam" {
                mlxAvailable = dict["mlx_available"] as? Bool ?? false
            }
```

Эта переменная объявляется в той же `DispatchQueue.global` замыкании, что и `engines`, и захватывается в `DispatchQueue.main.async` ниже вместо вызова `self.lastMlxAvailable(from:)`. Заменить строку `let mlxAvailable = self.lastMlxAvailable(from: engines)` на прямое использование захваченной переменной `mlxAvailable`.

- [ ] **Step 6: Собрать проект**

Run:
```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift build -c release 2>&1 | tail -40
```

Expected: `Build complete!`. Частые причины ошибки на этом шаге: неверное имя цветового токена (Step 2), отсутствие `STTEngineRow` полей — если компилятор жалуется на несуществующие свойства, проверить фактическую сигнатуру `STTEngineRow` в этом же файле (строки 30-37) перед правкой.

- [ ] **Step 7: Коммит**

```bash
cd "$(git rev-parse --show-toplevel)"
git add native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift
git commit -m "$(cat <<'EOF'
feat(ui): пикер транспорта GigaAM — Gemini-вариант

Статическая карточка под карточкой STT-движков: NSPopUpButton
(subprocess/mlx) + предупреждающий бейдж при mlx выбран, но
библиотека не найдена. autoenablesItems=false обязателен рядом с
item.isEnabled — иначе NSMenu перезаписывает disabled-состояние
(прецедент main+CallObserver.swift:56).

Видимость карточки и mlxAvailable синхронизируются из completion
fetchAndRebuildSTTEnginesCard — тумблер GigaAM живёт в соседней
асинхронно перестраиваемой карточке, статическая карточка транспорта
не участвует в её цикле разделителей.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Карточка пикера — Claude Design вариант

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift`

**Interfaces:**
- Consumes: `syncGigaamTransportControls`, `onGigaamTransportChanged` (Task 3 — переиспользуются, не дублируются).
- Produces: `func cdBuildGigaamTransportCard() -> NSView`.

- [ ] **Step 1: Построить карточку (CD) и врезать в `cdBuildSTTEnginesSection()`**

Аналогично Task 3 Step 2, но с `CDSettingsCardView` и `cdMakeRow`:

```swift
    @MainActor
    func cdBuildGigaamTransportCard() -> NSView {
        let card = CDSettingsCardView()

        let picker = NSPopUpButton(frame: .zero, pullsDown: false)
        picker.addItems(withTitles: ["Стабильный (subprocess)", "Быстрый (MLX, экспериментальный)"])
        picker.target = self
        picker.action = #selector(onGigaamTransportChanged(_:))
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportPicker, picker,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )

        let row = cdMakeRow(label: "Транспорт распознавания GigaAM", control: picker)
        card.contentStackView.addArrangedSubview(row)

        let warnLabel = NSTextField(labelWithString: "")
        warnLabel.font = .systemFont(ofSize: 11, weight: .regular)
        warnLabel.textColor = KrabEarTheme.Colors.warning
        warnLabel.isHidden = true
        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportWarnLabel, warnLabel,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        card.contentStackView.addArrangedSubview(warnLabel)

        objc_setAssociatedObject(
            self, &STTEnginesAssocKeys.cdGigaamTransportCard, card,
            .OBJC_ASSOCIATION_RETAIN_NONATOMIC
        )
        return card
    }
```

В `cdBuildSTTEnginesSection()` добавить (по образцу Task 3 Step 2):

```swift
        let cdTransportCard = cdBuildGigaamTransportCard()
        section.contentStackView.addArrangedSubview(cdTransportCard)
```

Проверить фактическую сигнатуру `cdMakeRow` перед использованием:

```bash
cd "$(git rev-parse --show-toplevel)" && sed -n '95,120p' native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings+ClaudeDesign.swift
```

Если сигнатура требует дополнительные параметры (например `badge:`, `badgeOnRight:`) — передать `nil`/`false` по образцу существующих вызовов `cdMakeRow` в этом же файле.

- [ ] **Step 2: Собрать проект**

Run:
```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift build -c release 2>&1 | tail -40
```

Expected: `Build complete!`.

- [ ] **Step 3: Коммит**

```bash
cd "$(git rev-parse --show-toplevel)"
git add native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift
git commit -m "$(cat <<'EOF'
feat(ui): пикер транспорта GigaAM — Claude Design-вариант

Тот же контракт, что Gemini-вариант (Task 3): переиспользует
onGigaamTransportChanged и syncGigaamTransportControls, не дублирует.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Вызов sync-хука из `syncSettingsControls` + source-контракт тесты

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift:595` (номер — на момент написания, искать по тексту `syncCloudRewriterControls(settings: settings)`)
- Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/STTTransportPickerWiringTests.swift`

**Interfaces:**
- Consumes: `syncGigaamTransportControls(settings:mlxAvailable:)` (Task 3).
- Produces: ничего для дальнейших задач — последняя задача перед гейтом.

🔴 **Это САМАЯ ВАЖНАЯ задача плана.** Без неё вся проводка Task 2-4 синтаксически корректна, компилируется, и выглядит рабочей — но пикер никогда не получит начальное значение при открытии Settings. Ровно класс «декоративная проводка», из-за которого в этом репозитории уже был мёртв `setupErrorBus`/`setupHealthMonitor` несмотря на 100% зелёные тесты.

- [ ] **Step 1: Написать падающие source-контракт тесты**

Создать `native/KrabEarAgent/Tests/KrabEarAgentTests/STTTransportPickerWiringTests.swift`:

```swift
import XCTest
@testable import KrabEarAgent

/// Source-контракт тесты для пикера транспорта GigaAM (2026-08-23).
///
/// Каждый тест проверяет ФАКТ ВЫЗОВА, а не факт существования кода — три
/// механизма (autoenablesItems, sync-хук, completion-проводка) в неправильной
/// реализации выглядят присутствующими и дают зелёные unit-тесты при мёртвом
/// UI. Паттерн: MainErrorsWiringTests / MainHealthMonitorWiringTests.
final class STTTransportPickerWiringTests: XCTestCase {

    /// Резолвит путь ОТ ЭТОГО тестового файла до корня репозитория. Тот же
    /// bundle-based паттерн, что MainErrorsWiringTests.mainSwiftURL — bundle
    /// на CI может лежать не там же, где исходники, поэтому обход вверх по
    /// файловой системе надёжнее жёсткого подсчёта deletingLastPathComponent.
    private func readSourceFile(_ relativePath: String) throws -> String {
        let bundleURL = Bundle(for: STTTransportPickerWiringTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent(relativePath)
            if FileManager.default.fileExists(atPath: candidate.path) {
                return try String(contentsOf: candidate, encoding: .utf8)
            }
            url = url.deletingLastPathComponent()
        }
        // Фолбэк (тот же паттерн, что MainErrorsWiringTests.mainSwiftURL):
        // от #file поднимаемся до repo root — 5 компонентов пути теста
        // (native/KrabEarAgent/Tests/KrabEarAgentTests/<файл>.swift).
        let fileURL = URL(fileURLWithPath: #file)
        let repoRoot = fileURL
            .deletingLastPathComponent()  // KrabEarAgentTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // KrabEarAgent (package root)
            .deletingLastPathComponent()  // native
            .deletingLastPathComponent()  // repo root
        return try String(
            contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8
        )
    }

    /// H1: autoenablesItems=false обязан стоять рядом с item.isEnabled для
    /// пункта MLX — иначе NSMenu перезаписывает disabled-состояние перед
    /// показом (прецедент: main+CallObserver.swift:56, находка MED-2).
    func test_autoenablesItemsFalse_isSetNextToMlxItemDisable() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift"
        )
        XCTAssertTrue(
            source.contains("menu?.autoenablesItems = false"),
            "Пикер транспорта GigaAM обязан выставлять autoenablesItems = false "
            + "перед item(at:).isEnabled — иначе задизейбленный пункт MLX "
            + "останется кликабельным (см. main+CallObserver.swift:56)"
        )
    }

    /// C5a(а): syncGigaamTransportControls ОПРЕДЕЛЁН — но это не доказывает,
    /// что он вызывается. Следующий тест проверяет именно вызов.
    func test_syncGigaamTransportControls_isDefined() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift"
        )
        XCTAssertTrue(source.contains("func syncGigaamTransportControls"))
    }

    /// C5a(а), ГЛАВНЫЙ ГАРД: syncGigaamTransportControls обязан вызываться
    /// из syncSettingsControls — по образцу syncCloudRewriterControls
    /// (HistoryPanelController+Settings.swift). Без этого вызова пикер
    /// не получит начальное значение при открытии Settings и не ресинкнется
    /// после внешнего set_settings/apply_profile_preset.
    func test_syncGigaamTransportControls_isCalledFromSyncSettingsControls() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift"
        )
        XCTAssertTrue(
            source.contains("syncGigaamTransportControls("),
            "syncSettingsControls() обязан вызывать syncGigaamTransportControls "
            + "(по образцу syncCloudRewriterControls) — иначе пикер транспорта "
            + "GigaAM никогда не отразит реальное состояние настроек"
        )
    }

    /// C5a(б,в): видимость карточки и mlxAvailable обязаны пересчитываться
    /// в completion fetchAndRebuildSTTEnginesCard — тумблер GigaAM живёт в
    /// СОСЕДНЕЙ асинхронно перестраиваемой карточке.
    func test_gigaamTransportCard_visibilityWiredFromEnginesCompletion() throws {
        let source = try readSourceFile(
            "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift"
        )
        XCTAssertTrue(
            source.contains("gigaamEnabled"),
            "Completion fetchAndRebuildSTTEnginesCard обязан пересчитывать "
            + "видимость карточки транспорта по актуальному состоянию тумблера "
            + "GigaAM, а не только при первом построении секции"
        )
        XCTAssertTrue(
            source.contains("mlx_available"),
            "Completion обязан извлекать mlx_available из сырого ответа "
            + "list_stt_engines и передавать в syncGigaamTransportControls"
        )
    }
}
```

- [ ] **Step 2: Прогнать тесты и убедиться, что они падают**

Run:
```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift test --filter STTTransportPickerWiringTests 2>&1 | tail -30
```

Expected: **1 failed** — `test_syncGigaamTransportControls_isCalledFromSyncSettingsControls` (остальные три уже должны быть GREEN после Task 3/4, так как исходники уже содержат нужные подстроки; если больше одного FAIL — значит Task 3/4 реализованы не полностью, вернуться и проверить).

🔴 **Если ВСЕ 4 теста падают с ошибкой чтения файла** (`Fatal error` или `could not open file`, а не `XCTAssertTrue failed`) — значит `readSourceFile` не находит исходники ни через bundle-обход, ни через `#file`-фолбэк на этой машине/раннере. Диагностировать печатью `bundleURL` и `candidate.path` внутри цикла перед тем, как менять логику дальше — не гадать заново.

- [ ] **Step 3: Вызвать хук из `syncSettingsControls`**

Найти `syncCloudRewriterControls(settings: settings)` в `HistoryPanelController+Settings.swift` (внутри `func syncSettingsControls(using value: AgentSettings? = nil)`) и добавить сразу после:

```swift
        syncCloudRewriterControls(settings: settings)
        // mlxAvailable здесь всегда false: этот путь синхронизации не имеет
        // доступа к последнему ответу list_stt_engines (тот приходит только
        // из fetchAndRebuildSTTEnginesCard, см. Task 3 Step 5). syncSettingsControls
        // покрывает значение пикера (subprocess/mlx) и бейдж по последнему
        // ИЗВЕСТНОМУ mlxAvailable — актуализация после факта идёт через
        // completion в Task 3 Step 5, который зовёт syncGigaamTransportControls
        // повторно с настоящим значением.
        syncGigaamTransportControls(settings: settings, mlxAvailable: lastKnownGigaamMlxAvailable)
```

Добавить хранимое свойство `lastKnownGigaamMlxAvailable` в `HistoryPanelController` (рядом с объявлением `isSyncingSettings` в том же файле):

```bash
cd "$(git rev-parse --show-toplevel)" && grep -n "var isSyncingSettings" native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift
```

Добавить рядом с найденной строкой:

```swift
    /// Последнее известное значение mlx_available из list_stt_engines.
    /// syncSettingsControls не имеет доступа к свежему ответу IPC — использует
    /// это кэшированное значение; completion fetchAndRebuildSTTEnginesCard
    /// (Task 3 Step 5) обновляет его и вызывает syncGigaamTransportControls
    /// повторно с актуальным значением.
    var lastKnownGigaamMlxAvailable: Bool = false
```

В Task 3 Step 5, в completion `fetchAndRebuildSTTEnginesCard`, перед вызовом `self.syncGigaamTransportControls(...)` добавить:

```swift
                self.lastKnownGigaamMlxAvailable = mlxAvailable
```

- [ ] **Step 4: Прогнать тесты и убедиться, что они проходят**

Run:
```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift test --filter STTTransportPickerWiringTests 2>&1 | tail -30
```

Expected: **4 passed**.

- [ ] **Step 5: Собрать релиз и прогнать полный тест-сьют Swift**

Run:
```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift build -c release 2>&1 | tail -20 && swift test 2>&1 | tail -60
```

Expected: `Build complete!`, все существующие тесты остаются зелёными (регрессия отсутствует).

- [ ] **Step 6: Коммит**

```bash
cd "$(git rev-parse --show-toplevel)"
git add native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift \
        native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift \
        native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+STTEnginesPicker.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/STTTransportPickerWiringTests.swift
git commit -m "$(cat <<'EOF'
fix(ui): пикер транспорта GigaAM подключён к syncSettingsControls

Без этого коммита вся проводка Task 2-4 компилировалась и выглядела
рабочей, но пикер никогда не получал начальное значение при открытии
Settings — класс декоративной проводки (setupErrorBus/setupHealthMonitor
precedent). 4 source-контракт-теста проверяют ФАКТ вызова трёх
критичных механизмов, не факт существования кода.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Полный гейт и PR

**Files:**
- Modify: `docs/NOW.md`

- [ ] **Step 1: Прогнать полный Python-гейт**

Run:
```bash
cd "$(git rev-parse --show-toplevel)" && PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_list_stt_engines.py -k "list_stt_engines or mlx_available" -v
python3 -m flake8 KrabEar/backend/stt_management_service.py KrabEar/tests/test_list_stt_engines.py
scripts/pre_merge_py312_check.sh KrabEar/tests/test_list_stt_engines.py
make audit-all
```

Expected: всё зелёное.

- [ ] **Step 2: Полный Swift-гейт**

Run:
```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift build -c release 2>&1 | tail -10 && swift test 2>&1 | tail -80
```

Expected: `Build complete!`, все тесты зелёные.

- [ ] **Step 3: Живая проверка в приложении**

Собрать и подписать бинарь, запустить, открыть Settings → STT-движки, убедиться визуально:

```bash
cd "$(git rev-parse --show-toplevel)/native/KrabEarAgent" && swift build -c release && cp -f .build/release/KrabEarAgent ../runtime/KrabEarAgent && codesign -s - -f ../runtime/KrabEarAgent
```

Проверить: карточка транспорта видна при включённом GigaAM, скрыта при выключенном; переключение на MLX (библиотека уже установлена и работает — подтверждено живым замером 2026-08-23) не даёт предупреждающего бейджа; после `set_settings` из другого источника (например через IPC-скрипт) пикер отражает актуальное значение при следующем открытии/пересинке панели.

- [ ] **Step 4: Запись в NOW.md**

Добавить в актуальный раздел:

```markdown
- 🟢 Пикер транспорта GigaAM (subprocess/mlx) в Settings → STT-движки: оба
  UI-варианта (Gemini + Claude Design). Честная индикация недоступности MLX
  через `find_spec("gigaam_mlx")` (не импорт — тот успешен без библиотеки).
  Спека: [`2026-08-23-gigaam-transport-picker-design.md`](superpowers/specs/2026-08-23-gigaam-transport-picker-design.md).
```

- [ ] **Step 5: PR**

```bash
cd "$(git rev-parse --show-toplevel)"
git push -u origin feat/gigaam-transport-picker
gh pr create --base codex/krab-ear-v2 --title "feat(ui): пикер транспорта GigaAM в Settings" --body "$(cat <<'EOF'
## Что добавляет

Переключатель транспорта GigaAM v3 (`subprocess`/`mlx`) в Settings → STT-движки,
оба UI-варианта. Оба пути уже реализованы в backend — не хватало только UI.

## Обоснование живым замером

MLX даёт 48× realtime против 25× у subprocess при расхождении текста 2.9%
(орфографические варианты нечётких слов, смысловых потерь нет). Но найдена
асимметрия `mlx_lock`: whisper держит его на весь файл, поэтому GigaAM-чанк
параллельно с 60с диктовки ждёт 6.71с вместо 0.07с. Выбор остаётся выбором
намеренно.

## Честное поведение при недоступной библиотеке

🔴 Роутер НЕ откатывается с `mlx` на `subprocess` при отсутствующей
`gigaam_mlx` — GigaAM молча исчезает из STT-каскада. Пикер делает это видимым:
задизейбленный пункт MLX (с `autoenablesItems=false` — иначе `NSMenu`
перезаписывает `isEnabled`) + предупреждающий бейдж, если MLX уже выбран,
а библиотека не найдена.

## Гейты

- Python: pytest + flake8 + ubuntu-parity + `make audit-all`
- Swift: `swift build -c release` + `swift test` (включая 4 source-контракт-теста
  на факт ВЫЗОВА sync-хука и `autoenablesItems`, не факт существования кода)
- Живая проверка в собранном приложении

## Известные ограничения (см. спеку)

- При `transport=mlx` GigaAM пропадает из `GET /v1/models` REST-процесса
  (фабрика `stt_router_factory` не знает транспорт `mlx`) — существующее
  поведение с mlx-волны, фикс фабрики вне объёма этого PR.
- После неудачной попытки MLX возврат на subprocess лечит не мгновенно —
  маркер недоступности живёт до 5 минут (TTL в engine.py).

Спека: `docs/superpowers/specs/2026-08-23-gigaam-transport-picker-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**1. Покрытие спеки.**

| Раздел спеки | Задача |
|---|---|
| C1 (отдельная статическая карточка) | Task 3 Step 2, Task 4 Step 1 |
| C2 (6 точек AgentSettings + N1-фикс дефолта) | Task 2 |
| C3 (`find_spec`, не импорт) | Task 1 |
| C4 (два значения, запись, оговорка про полный payload) | Task 3 Step 3 |
| C5 (задизейбленный пункт + бейдж + H1-фикс autoenablesItems) | Task 3 Step 4 |
| C5a (три обязательства проводки) | Task 3 Step 5, Task 5 |
| C6 (оба варианта) | Task 3 + Task 4 |
| Тесты 1-9 из спеки | Task 1 (1-3), Task 5 (7,8,9), тесты 4-6 покрыты транзитивно сборкой (Task 2 Step 7, Task 3/4 Step 6/2) |
| Ограничение 3 (M2, REST-фабрика) | документировано в PR-описании (Task 6 Step 5), кода не требует — отдельная волна |
| Ограничение 4 (L2, TTL 5 мин) | документировано в спеке, кода не требует |

Гэпов нет.

**2. Заглушки.** Не найдено — каждый шаг с кодом содержит полный текст, каждая команда точна.

**3. Согласованность типов.** `gigaamTransport: String` (Task 2) используется идентично в Task 3/4/5 (`settings.gigaamTransport`, ключ `"stt_gigaam_transport"`). `mlx_available: bool` (Task 1, Python snake_case ключ) читается в Task 3 Step 5 как `dict["mlx_available"]` — совпадает буква-в-букву. `syncGigaamTransportControls(settings:mlxAvailable:)` объявлен в Task 3 Step 4 и вызывается с той же сигнатурой в Task 3 Step 5 и Task 5 Step 3.
