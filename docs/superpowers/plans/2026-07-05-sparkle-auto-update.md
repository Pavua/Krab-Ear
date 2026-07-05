# Sparkle Auto-Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Автообновления Swift-агента Krab Ear через Sparkle 2 + CI-релиз (GitHub Actions по тегу `vX.Y.Z` / workflow_dispatch) с публикацией в GitHub Releases и appcast.xml в репо.

**Architecture:** Клиент — `SPUStandardUpdaterController` (стандартный Sparkle UI), инициализируется ТОЛЬКО когда `.app` установлен вне каталога проекта (dev-guard: прод-бандл владельца живёт внутри git-репо, in-place обновление переписало бы рабочее дерево). Релиз — новый workflow `release.yml`: CI-green guard → сборка → ассемблирование через общий `scripts/assemble_signed_app.sh` (вынесен из DMG-скрипта) → codesign той же identity «Krab Ear Dev Local» (TCC переживает обновление) → zip → EdDSA-подпись → `gh release create` → appcast-коммит с `[skip ci]`.

**Tech Stack:** Sparkle 2.6.x (SPM), GitHub Actions (macos-latest arm64), python-stdlib генератор appcast, zsh-скрипты.

**Спека:** `docs/superpowers/specs/2026-07-05-sparkle-auto-update-design.md`

**Критичные факты, найденные разведкой (НЕ пропускать):**
- Sparkle — ДИНАМИЧЕСКИЙ framework: без `Sparkle.framework` по rpath бинарь падает на старте dyld-ошибкой «Library not loaded». Нужны: rpath `@executable_path/../Frameworks` в Package.swift, копирование framework в `Contents/Frameworks/` бандла, и зеркало в `native/Frameworks/` для голого dev-бинаря `native/runtime/KrabEarAgent`.
- Прод-приложение владельца (`Krab Ear.app`) лежит ВНУТРИ git-репо (launchd `ai.krab.ear.agent` указывает туда). Sparkle-обновление in-place = переписанное рабочее дерево git. Dev-guard обязателен (Task 5).
- Существующий пункт меню «Update Channel» (stable/beta, `settings.updateChannel`) — вестигиальный, ничего не делает. В ЭТОЙ волне НЕ трогаем и НЕ подключаем к Sparkle (YAGNI, один appcast). Не удалять — вне скоупа.
- В `Package.swift` тест-таргеты наследуют зависимость — `swift test` работает из `.build`, где framework есть; отдельных правок не нужно.

---

### Task 1: Генератор appcast-item (python, TDD)

**Files:**
- Create: `scripts/generate_appcast_item.py`
- Create: `appcast.xml` (скелет, корень репо)
- Test: `KrabEar/tests/test_appcast_generator.py`

- [ ] **Step 1: Скелет appcast.xml в корне репо**

```xml
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Krab Ear</title>
    <link>https://raw.githubusercontent.com/Pavua/Krab-Ear/codex/krab-ear-v2/appcast.xml</link>
    <description>Krab Ear updates</description>
    <language>ru</language>
  </channel>
</rss>
```

- [ ] **Step 2: Failing-тест**

`KrabEar/tests/test_appcast_generator.py`:

```python
"""Тесты генератора appcast-item (Sparkle auto-update, spec 2026-07-05).

Скрипт scripts/generate_appcast_item.py вставляет <item> в appcast.xml.
Требования: монотонность версии (новая строго > максимальной существующей),
валидный XML на выходе, все обязательные Sparkle-атрибуты enclosure.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SCRIPT = _PROJECT_ROOT / "scripts" / "generate_appcast_item.py"
_spec = importlib.util.spec_from_file_location("generate_appcast_item", _SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

SKELETON = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
  <channel>
    <title>Krab Ear</title>
    <link>https://example.invalid/appcast.xml</link>
    <description>Krab Ear updates</description>
    <language>ru</language>
  </channel>
</rss>
"""


def _add(xml_text, version, url="https://example.invalid/a.zip",
         sig="EDSIG==", length=1234):
    return gen.add_item(xml_text, version=version, url=url,
                        ed_signature=sig, length=length)


class TestAddItem(unittest.TestCase):
    def test_insert_into_skeleton_is_valid_xml_with_required_attrs(self):
        out = _add(SKELETON, "2.4.0")
        root = ET.fromstring(out)  # парсится => валидный XML
        ns = {"sparkle": "http://www.andymatuschak.org/xml-namespaces/sparkle"}
        enclosures = root.findall(".//item/enclosure")
        self.assertEqual(len(enclosures), 1)
        e = enclosures[0]
        self.assertEqual(e.get("url"), "https://example.invalid/a.zip")
        self.assertEqual(
            e.get("{http://www.andymatuschak.org/xml-namespaces/sparkle}version"),
            "2.4.0")
        self.assertEqual(e.get("length"), "1234")
        self.assertEqual(
            e.get("{http://www.andymatuschak.org/xml-namespaces/sparkle}edSignature"),
            "EDSIG==")
        min_os = root.find(".//item/sparkle:minimumSystemVersion", ns)
        self.assertIsNotNone(min_os)
        self.assertEqual(min_os.text, "13.0")

    def test_second_item_appends_and_keeps_first(self):
        out = _add(_add(SKELETON, "2.4.0"), "2.4.1")
        root = ET.fromstring(out)
        versions = [
            e.get("{http://www.andymatuschak.org/xml-namespaces/sparkle}version")
            for e in root.findall(".//item/enclosure")
        ]
        self.assertEqual(sorted(versions), ["2.4.0", "2.4.1"])

    def test_non_monotonic_version_rejected(self):
        once = _add(SKELETON, "2.4.0")
        with self.assertRaises(ValueError):
            _add(once, "2.4.0")   # равная
        with self.assertRaises(ValueError):
            _add(once, "2.3.9")   # меньшая

    def test_bad_semver_rejected(self):
        with self.assertRaises(ValueError):
            _add(SKELETON, "v2.4.0")
        with self.assertRaises(ValueError):
            _add(SKELETON, "2.4")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Прогнать — убедиться что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_appcast_generator.py -v`
Expected: FAIL (script not found / no attribute add_item)

- [ ] **Step 4: Реализация `scripts/generate_appcast_item.py`**

```python
#!/usr/bin/env python3
"""Вставляет <item> релиза в appcast.xml (Sparkle auto-update).

Только stdlib. Вставка строковая (перед </channel>) — ElementTree ломает
namespace-префиксы при round-trip; валидность выхода проверяется парсом.

Usage:
    python3 scripts/generate_appcast_item.py \
        --appcast appcast.xml --version 2.4.0 \
        --url https://github.com/Pavua/Krab-Ear/releases/download/v2.4.0/Krab-Ear-v2.4.0.zip \
        --ed-signature "BASE64SIG==" --length 7080544
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_VERSION_ATTR_RE = re.compile(r'sparkle:version="(\d+\.\d+\.\d+)"')


def _parse_semver(version: str) -> tuple[int, int, int]:
    if not _SEMVER_RE.match(version):
        raise ValueError(f"версия не semver X.Y.Z: {version!r}")
    a, b, c = version.split(".")
    return (int(a), int(b), int(c))


def add_item(xml_text: str, *, version: str, url: str,
             ed_signature: str, length: int, pub_date: str | None = None) -> str:
    """Возвращает appcast с добавленным <item>. ValueError при немонотонной версии."""
    new_v = _parse_semver(version)
    existing = [_parse_semver(v) for v in _VERSION_ATTR_RE.findall(xml_text)]
    if existing and new_v <= max(existing):
        raise ValueError(
            f"версия {version} не больше максимальной в appcast "
            f"({'.'.join(map(str, max(existing)))}) — Sparkle требует монотонность")
    if "</channel>" not in xml_text:
        raise ValueError("appcast без </channel> — не скелет Sparkle-фида")

    if pub_date is None:
        pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    item = f"""    <item>
      <title>Krab Ear {version}</title>
      <pubDate>{pub_date}</pubDate>
      <sparkle:minimumSystemVersion>13.0</sparkle:minimumSystemVersion>
      <enclosure url="{url}"
                 sparkle:version="{version}"
                 sparkle:shortVersionString="{version}"
                 length="{length}"
                 sparkle:edSignature="{ed_signature}"
                 type="application/octet-stream"/>
    </item>
"""
    out = xml_text.replace("</channel>", item + "  </channel>", 1)
    ET.fromstring(out)  # self-check: выход обязан быть валидным XML
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--appcast", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--ed-signature", required=True)
    p.add_argument("--length", required=True, type=int)
    args = p.parse_args()
    with open(args.appcast, encoding="utf-8") as f:
        xml_text = f.read()
    out = add_item(xml_text, version=args.version, url=args.url,
                   ed_signature=args.ed_signature, length=args.length)
    with open(args.appcast, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"appcast: добавлен item v{args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Тесты зелёные + flake8 + ubuntu-parity**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_appcast_generator.py -v`
Expected: 4 passed.
Run: `.venv_krab_ear/bin/flake8 scripts/generate_appcast_item.py KrabEar/tests/test_appcast_generator.py --max-line-length=120 --ignore=E501,W503,E402`
Expected: пусто.
Run: `bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_appcast_generator.py`
Expected: ALL GREEN.

- [ ] **Step 6: Commit**

```bash
git add appcast.xml scripts/generate_appcast_item.py KrabEar/tests/test_appcast_generator.py
git commit -m "feat(sparkle): appcast-скелет + генератор item с монотонной валидацией"
```

---

### Task 2: Sparkle EdDSA-ключи + GitHub Secrets (частично owner-assisted)

**Files:** нет изменений в репо (секреты + Keychain). Публичный ключ записать в блокнот задачи — нужен в Task 4.

- [ ] **Step 1: Скачать пинованный Sparkle-дистрибутив (тулзы)**

```bash
mkdir -p /tmp/sparkle-tools && cd /tmp/sparkle-tools
curl -L -o sparkle.tar.xz \
  https://github.com/sparkle-project/Sparkle/releases/download/2.6.4/Sparkle-2.6.4.tar.xz
tar -xJf sparkle.tar.xz
ls bin/   # ожидаем: generate_keys sign_update generate_appcast ...
```

- [ ] **Step 2: Сгенерировать ключи (приватный — в login Keychain)**

```bash
/tmp/sparkle-tools/bin/generate_keys
# Печатает публичный ключ вида: <SUPublicEDKey> ... </...> или base64-строку.
# ЗАПИСАТЬ публичный ключ — он пойдёт в Info.plist (Task 4).
```

⚠️ macOS может показать GUI-запрос доступа к Keychain — нужен клик владельца.

- [ ] **Step 3: Экспорт приватного ключа + секрет в GitHub**

```bash
/tmp/sparkle-tools/bin/generate_keys -x /tmp/sparkle_ed_private_key
chmod 600 /tmp/sparkle_ed_private_key
gh secret set SPARKLE_PRIVATE_KEY < /tmp/sparkle_ed_private_key
rm -f /tmp/sparkle_ed_private_key
```

НИКОГДА не печатать содержимое ключа в чат/лог.

- [ ] **Step 4: Экспорт сертификата «Krab Ear Dev Local» в .p12 (owner-assisted)**

```bash
# Пароль для .p12 — сгенерировать случайный, НЕ печатать:
P12PASS=$(openssl rand -base64 24)
security export -k login.keychain -t identities -f pkcs12 \
  -o /tmp/krab_dev_local.p12 -P "$P12PASS"
# ⚠️ GUI-диалог Keychain «разрешить экспорт ключа» — владелец кликает Allow
#    (возможно с вводом пароля логина).
# Если экспортируется несколько identity — открыть Keychain Access,
# экспортировать ТОЛЬКО «Krab Ear Dev Local» руками в /tmp/krab_dev_local.p12.
base64 -i /tmp/krab_dev_local.p12 | gh secret set MACOS_CERT_P12
printf '%s' "$P12PASS" | gh secret set MACOS_CERT_PASSWORD
rm -f /tmp/krab_dev_local.p12
unset P12PASS
```

- [ ] **Step 5: Проверить секреты на месте**

Run: `gh secret list`
Expected: `MACOS_CERT_P12`, `MACOS_CERT_PASSWORD`, `SPARKLE_PRIVATE_KEY` в списке.

---

### Task 3: Package.swift — Sparkle SPM + rpath

**Files:**
- Modify: `native/KrabEarAgent/Package.swift`

- [ ] **Step 1: Добавить зависимость и rpath**

В блок `dependencies:` (после swift-opus):

```swift
        // Sparkle 2 — автообновления .app (spec 2026-07-05-sparkle-auto-update).
        // ДИНАМИЧЕСКИЙ framework: бинарь требует Sparkle.framework по rpath
        // @executable_path/../Frameworks (см. linkerSettings ниже) — в бандле
        // это Contents/Frameworks/, для dev-бинаря native/runtime — native/Frameworks/.
        .package(
            url: "https://github.com/sparkle-project/Sparkle",
            from: "2.6.0"
        ),
```

В `dependencies:` executableTarget:

```swift
                .product(name: "Sparkle", package: "Sparkle"),
```

В executableTarget после `swiftSettings:` добавить:

```swift
            linkerSettings: [
                // Sparkle.framework ищется рядом с бандлом: Contents/Frameworks.
                // swift build дополнительно зашивает абсолютный rpath на .build —
                // dev-бинарь на машине сборки находит framework и без копии.
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"]),
            ]
```

- [ ] **Step 2: Сборка**

Run: `cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3`
Expected: `Build complete!`

- [ ] **Step 3: Найти собранный framework (понадобится всем скриптам)**

Run: `find native/KrabEarAgent/.build -type d -name "Sparkle.framework" | head -3`
Expected: минимум один путь (артефакт xcframework и/или копия в release). Зафиксировать реальный путь.

- [ ] **Step 4: Тесты не сломаны**

Run: `cd native/KrabEarAgent && swift test 2>&1 | tail -3`
Expected: 0 failures.

- [ ] **Step 5: Commit**

```bash
git add native/KrabEarAgent/Package.swift native/KrabEarAgent/Package.resolved
git commit -m "feat(sparkle): SPM-зависимость Sparkle 2 + rpath Frameworks"
```

---

### Task 4: Info.plist — Sparkle-ключи + бамп версии до 2.4.0

**Files:**
- Modify: `Krab Ear.app/Contents/Info.plist`

- [ ] **Step 1: Добавить в dict (перед `</dict>`)**

```xml
	<key>SUFeedURL</key>
	<string>https://raw.githubusercontent.com/Pavua/Krab-Ear/codex/krab-ear-v2/appcast.xml</string>
	<key>SUPublicEDKey</key>
	<string>ПУБЛИЧНЫЙ_КЛЮЧ_ИЗ_TASK_2</string>
	<key>SUEnableAutomaticChecks</key>
	<true/>
```

И поднять обе версии `2.3.0` → `2.4.0` (первый Sparkle-enabled релиз будет v2.4.0):

```xml
	<key>CFBundleShortVersionString</key>
	<string>2.4.0</string>
	<key>CFBundleVersion</key>
	<string>2.4.0</string>
```

- [ ] **Step 2: Валидация plist**

Run: `plutil -lint "Krab Ear.app/Contents/Info.plist"`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add "Krab Ear.app/Contents/Info.plist"
git commit -m "feat(sparkle): SUFeedURL/SUPublicEDKey в Info.plist, версия 2.4.0"
```

---

### Task 5: Клиент — main+SparkleUpdater.swift + пункт меню + source-контракт тесты

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/main+SparkleUpdater.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (вызов в `completeStartupAfterBackendReady()`)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift` (пункт «Проверить обновления…»)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/SparkleWiringSourceContractTests.swift`

- [ ] **Step 1: Failing source-контракт тест (урок setupErrorBus/setupHealthMonitor: setup-функция без call site = декоративная проводка)**

`SparkleWiringSourceContractTests.swift`:

```swift
/*
 SparkleWiringSourceContractTests — source-контракт: Sparkle реально ПОДКЛЮЧЁН
 к lifecycle, а не только определён (класс бага setupErrorBus/setupHealthMonitor,
 2026-07-05: обе функции годами были определены, но не вызваны — фичи мертвы
 в проде при 100% зелёных изолированных тестах).
*/

import XCTest
import Foundation

final class SparkleWiringSourceContractTests: XCTestCase {

    func test_setupSparkleUpdater_is_actually_called_from_startup() throws {
        let src = try String(contentsOf: Self.sourceURL("main.swift"), encoding: .utf8)
        XCTAssertTrue(
            src.contains("setupSparkleUpdater()"),
            "completeStartupAfterBackendReady() должен вызывать setupSparkleUpdater()"
        )
    }

    func test_check_updates_menu_item_exists() throws {
        let src = try String(contentsOf: Self.sourceURL("main+StatusMenu.swift"), encoding: .utf8)
        XCTAssertTrue(
            src.contains("onCheckForUpdates"),
            "rebuildStatusMenu() должен содержать пункт «Проверить обновления…»"
        )
    }

    /// Резолв файла исходников из тест-бандла (паттерн SFSymbolVerificationTests).
    private static func sourceURL(_ name: String) -> URL {
        let bundleURL = Bundle(for: SparkleWiringSourceContractTests.self).bundleURL
        var url = bundleURL
        for _ in 0..<10 {
            let candidate = url.appendingPathComponent("Sources/KrabEarAgent/\(name)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
            url = url.deletingLastPathComponent()
        }
        let fileURL = URL(fileURLWithPath: #file)
        return fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/KrabEarAgent/\(name)")
    }
}
```

- [ ] **Step 2: Прогнать — падает**

Run: `cd native/KrabEarAgent && swift test --filter SparkleWiringSourceContractTests 2>&1 | tail -5`
Expected: 2 failures.

- [ ] **Step 3: `main+SparkleUpdater.swift`**

```swift
/*
 main+SparkleUpdater.swift — автообновления через Sparkle 2 (IPC не участвует).

 Spec: docs/superpowers/specs/2026-07-05-sparkle-auto-update-design.md.

 🔴 Dev-guard (критично): прод-приложение владельца лежит ВНУТРИ git-репо
 (launchd указывает на <repo>/Krab Ear.app) — Sparkle-обновление in-place
 переписало бы рабочее дерево git и сломало parity-конвенцию бинарей.
 Поэтому updater инициализируется ТОЛЬКО когда .app установлен ВНЕ каталога
 проекта (эвристика та же, что resolveProjectRoot: рядом с бандлом нет
 KrabEar/backend/service.py). Для получателей DMG в /Applications — работает.
 На dev-машине путь обновления остаётся build_and_deploy.command.
*/

import AppKit
import Foundation
import ObjectiveC.runtime
import Sparkle

private nonisolated(unsafe) var sparkleControllerKey: UInt8 = 0

@MainActor
extension AgentAppDelegate {

    var sparkleUpdaterController: SPUStandardUpdaterController? {
        get { objc_getAssociatedObject(self, &sparkleControllerKey) as? SPUStandardUpdaterController }
        set {
            objc_setAssociatedObject(
                self, &sparkleControllerKey, newValue, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        }
    }

    /// true когда бандл — установленная копия (не dev внутри репо, не голый бинарь).
    var isSparkleEligibleInstall: Bool {
        let bundlePath = Bundle.main.bundlePath
        guard bundlePath.hasSuffix(".app") else { return false }  // голый dev-бинарь
        let repoMarker = (bundlePath as NSString).deletingLastPathComponent
            + "/KrabEar/backend/service.py"
        return !FileManager.default.fileExists(atPath: repoMarker)
    }

    /// Вызывается из completeStartupAfterBackendReady().
    func setupSparkleUpdater() {
        guard isSparkleEligibleInstall else {
            logger.info("Sparkle: пропущен (dev-запуск: бандл в каталоге проекта или голый бинарь)")
            return
        }
        let controller = SPUStandardUpdaterController(
            startingUpdater: true,
            updaterDelegate: nil,
            userDriverDelegate: nil
        )
        self.sparkleUpdaterController = controller
        logger.info("Sparkle updater запущен (SUFeedURL из Info.plist)")
    }

    @objc func onCheckForUpdates() {
        sparkleUpdaterController?.checkForUpdates(nil)
    }
}
```

- [ ] **Step 4: Вызов в main.swift**

В `completeStartupAfterBackendReady()`, сразу после `setupErrorBus(toastPresenter: ErrorToastPresenter())`:

```swift
        // Sparkle автообновления (только для установленных вне репо копий —
        // dev-guard внутри, см. main+SparkleUpdater.swift).
        setupSparkleUpdater()
```

Teardown не нужен: Sparkle сам управляет своим lifecycle до конца процесса.

- [ ] **Step 5: Пункт меню в main+StatusMenu.swift**

В `rebuildStatusMenu()`, сразу ПОСЛЕ блока `menu.setSubmenu(updateChannelSubmenu, for: updateChannelItem)`:

```swift
        let checkUpdatesItem = NSMenuItem(
            title: "Проверить обновления…",
            action: #selector(onCheckForUpdates),
            keyEquivalent: ""
        )
        checkUpdatesItem.target = self
        // Dev-запуск (бандл в репо): Sparkle не инициализирован — пункт серый.
        checkUpdatesItem.isEnabled = sparkleUpdaterController != nil
        menu.addItem(checkUpdatesItem)
```

Глиф-гейт: `…` (U+2026) уже используется в существующих пунктах меню — безопасен.

- [ ] **Step 6: Сборка + все тесты**

Run: `cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3 && swift test 2>&1 | tail -3`
Expected: Build complete; 0 failures (включая 2 новых source-контракта).

- [ ] **Step 7: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/main+SparkleUpdater.swift \
        native/KrabEarAgent/Sources/KrabEarAgent/main.swift \
        native/KrabEarAgent/Sources/KrabEarAgent/main+StatusMenu.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/SparkleWiringSourceContractTests.swift
git commit -m "feat(sparkle): SPUStandardUpdaterController + dev-guard + пункт меню"
```

---

### Task 6: Общий ассемблер бандла + Sparkle.framework в деплой-скриптах

**Files:**
- Create: `scripts/assemble_signed_app.sh`
- Modify: `scripts/build_distribution_dmg.command` (Step 2+3 → вызов ассемблера)
- Modify: `scripts/build_and_deploy.command` (копирование Sparkle.framework)
- Modify: `.gitignore` (добавить `native/Frameworks/`)

- [ ] **Step 1: `scripts/assemble_signed_app.sh`**

```bash
#!/bin/zsh
# assemble_signed_app.sh — единый ассемблер .app бандла Krab Ear.
# Используется build_distribution_dmg.command И release.yml (CI) — DRY,
# spec 2026-07-05-sparkle-auto-update (шаг 4 workflow).
#
# Usage:
#   scripts/assemble_signed_app.sh --output <dir> --version <X.Y.Z> --identity <name|->
#
# Делает: копия шаблона "Krab Ear.app" → свежий бинарь из .build/release →
# Sparkle.framework в Contents/Frameworks → bootstrap-инсталлятор в Resources →
# штамп версии → codesign --deep. Сборку Swift НЕ делает — caller обязан
# выполнить `swift build -c release` заранее.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NATIVE_DIR="$ROOT_DIR/native/KrabEarAgent"
APP_TEMPLATE="$ROOT_DIR/Krab Ear.app"

OUTPUT_DIR="" VERSION="" IDENTITY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)   OUTPUT_DIR="$2"; shift 2 ;;
    --version)  VERSION="$2";    shift 2 ;;
    --identity) IDENTITY="$2";   shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$OUTPUT_DIR" && -n "$VERSION" && -n "$IDENTITY" ]] || {
  echo "Usage: $0 --output <dir> --version <X.Y.Z> --identity <name|->" >&2; exit 1; }

BUILT_BINARY="$NATIVE_DIR/.build/release/KrabEarAgent"
[[ -f "$BUILT_BINARY" ]] || { echo "Нет $BUILT_BINARY — сначала swift build -c release" >&2; exit 1; }

SPARKLE_FW="$(find "$NATIVE_DIR/.build" -type d -name "Sparkle.framework" 2>/dev/null | head -1)"
[[ -n "$SPARKLE_FW" ]] || { echo "Sparkle.framework не найден в .build" >&2; exit 1; }

APP_OUT="$OUTPUT_DIR/Krab Ear.app"
mkdir -p "$OUTPUT_DIR"
rm -rf "$APP_OUT"
cp -R "$APP_TEMPLATE" "$APP_OUT"
cp -f "$BUILT_BINARY" "$APP_OUT/Contents/MacOS/KrabEarAgent"

mkdir -p "$APP_OUT/Contents/Frameworks"
rm -rf "$APP_OUT/Contents/Frameworks/Sparkle.framework"
# ditto сохраняет симлинки Versions/ внутри framework (cp -R достаточно на APFS,
# ditto — надёжнее при переносе).
ditto "$SPARKLE_FW" "$APP_OUT/Contents/Frameworks/Sparkle.framework"

mkdir -p "$APP_OUT/Contents/Resources"
cp -f "$ROOT_DIR/scripts/bootstrap_backend.command" "$APP_OUT/Contents/Resources/bootstrap_backend.command"
chmod +x "$APP_OUT/Contents/Resources/bootstrap_backend.command"

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_OUT/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP_OUT/Contents/Info.plist"

if [[ "$IDENTITY" == "-" ]]; then
  codesign --deep --force -s - "$APP_OUT"
else
  codesign --deep --force --sign "$IDENTITY" "$APP_OUT"
fi
codesign --verify --deep "$APP_OUT"
echo "OK: $APP_OUT (v$VERSION, identity: $IDENTITY)"
```

`chmod +x scripts/assemble_signed_app.sh`

- [ ] **Step 2: Рефактор build_distribution_dmg.command**

Заменить Step 2 (Assemble) + Step 3 (Code signing) — блок от `# ── Step 2: Assemble dist .app` до конца ad-hoc ветки подписи — на:

```bash
# ── Step 2+3: Assemble + sign (общий ассемблер) ───────────────────
if $DO_NOTARIZE; then
  ASSEMBLE_IDENTITY="Developer ID Application"
else
  ASSEMBLE_IDENTITY="-"
fi
"$ROOT_DIR/scripts/assemble_signed_app.sh" \
  --output "$DIST_DIR" --version "$VERSION" --identity "$ASSEMBLE_IDENTITY" \
  || err "assemble_signed_app.sh failed"
ok "App assembled + signed via shared assembler"
```

Примечание: notarize-ветка раньше добавляла `--options runtime --entitlements` — при получении Developer ID вернуть эти флаги В АССЕМБЛЕР (параметром), сейчас notarize-путь всё равно недоступен (нет Developer ID). Оставить `TODO(dev-id)`-комментарий в ассемблере НЕ надо — вместо этого одна строка в DISTRIBUTION.md (Task 8).

- [ ] **Step 3: Смок DMG-скрипта**

Run: `bash scripts/build_distribution_dmg.command --no-notarize 2>&1 | tail -6`
Expected: `DMG created: dist/Krab-Ear-v2.4.0.dmg` (версия читается из Info.plist = 2.4.0). Проверить framework внутри:
Run: `ls "dist/Krab Ear.app/Contents/Frameworks/"`
Expected: `Sparkle.framework`.

- [ ] **Step 4: build_and_deploy.command — framework в оба места**

После строк копирования бинаря (Step 2/Sync Binaries, `cp -f "$BUILD_BIN" ...` для bundle и runtime) добавить:

```bash
# Sparkle — динамический framework: без него бинарь не стартует (dyld).
SPARKLE_FW="$(find "$PACKAGE_DIR/.build" -type d -name "Sparkle.framework" 2>/dev/null | head -1)"
if [[ -n "$SPARKLE_FW" ]]; then
  mkdir -p "$ROOT_DIR/Krab Ear.app/Contents/Frameworks" "$ROOT_DIR/native/Frameworks"
  rm -rf "$ROOT_DIR/Krab Ear.app/Contents/Frameworks/Sparkle.framework" \
         "$ROOT_DIR/native/Frameworks/Sparkle.framework"
  ditto "$SPARKLE_FW" "$ROOT_DIR/Krab Ear.app/Contents/Frameworks/Sparkle.framework"
  ditto "$SPARKLE_FW" "$ROOT_DIR/native/Frameworks/Sparkle.framework"
fi
```

(вставить ДО шага Code Signing — framework должен попасть под codesign --deep).

- [ ] **Step 5: .gitignore**

Добавить строку `native/Frameworks/` (dev-артефакт; в отличие от него, `Krab Ear.app/Contents/Frameworks/Sparkle.framework` КОММИТИТСЯ — прод-бандл в репо обязан запускаться, конвенция parity-бинарей).

- [ ] **Step 6: Полный локальный деплой-смок**

Run: `./scripts/build_and_deploy.command --no-sentry 2>&1 | tail -5`
Expected: complete. Затем:
Run: `ls "Krab Ear.app/Contents/Frameworks/" native/Frameworks/`
Expected: Sparkle.framework в обоих.
Run: `"Krab Ear.app/Contents/MacOS/KrabEarAgent" --help 2>&1 | head -2 || true`
Expected: НЕ dyld-ошибка «Library not loaded» (любой другой вывод/exit — ок; главное процесс стартует).

- [ ] **Step 7: Commit (включая framework в бандле)**

```bash
git add scripts/assemble_signed_app.sh scripts/build_distribution_dmg.command \
        scripts/build_and_deploy.command .gitignore \
        "Krab Ear.app/Contents/Frameworks" "Krab Ear.app/Contents/MacOS/KrabEarAgent"
git add -f native/runtime/KrabEarAgent
git commit -m "feat(sparkle): общий ассемблер бандла + Sparkle.framework в деплой"
```

---

### Task 7: CI release-workflow

**Files:**
- Create: `.github/workflows/release.yml`

- [ ] **Step 1: Полный workflow**

```yaml
name: release

on:
  push:
    tags: ['v*.*.*']
  workflow_dispatch:
    inputs:
      version:
        description: 'Версия релиза (X.Y.Z, без префикса v)'
        required: true

permissions:
  contents: write

concurrency:
  group: release
  cancel-in-progress: false

env:
  SPARKLE_TOOLS_VERSION: "2.6.4"

jobs:
  release:
    runs-on: macos-latest
    steps:
      - name: Checkout (full — нужны ветка и теги для appcast-пуша)
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Resolve version
        id: ver
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            VERSION="${{ github.event.inputs.version }}"
          else
            VERSION="${GITHUB_REF_NAME#v}"
          fi
          echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
            || { echo "::error::версия не semver X.Y.Z: $VERSION"; exit 1; }
          echo "version=$VERSION" >> "$GITHUB_OUTPUT"

      - name: CI-green guard (krab-ear-ci на собираемом коммите)
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          CONCLUSION=$(gh run list --commit "$GITHUB_SHA" --workflow krabear-ci.yml \
            --json conclusion --jq '.[0].conclusion // "missing"')
          echo "krab-ear-ci @ $GITHUB_SHA: $CONCLUSION"
          [ "$CONCLUSION" = "success" ] \
            || { echo "::error::krab-ear-ci не зелёный ($CONCLUSION) — релиз запрещён (fail-closed)"; exit 1; }

      - name: Import signing certificate
        env:
          MACOS_CERT_P12: ${{ secrets.MACOS_CERT_P12 }}
          MACOS_CERT_PASSWORD: ${{ secrets.MACOS_CERT_PASSWORD }}
        run: |
          echo "$MACOS_CERT_P12" | base64 --decode > /tmp/cert.p12
          security create-keychain -p ci-keychain build.keychain
          security default-keychain -s build.keychain
          security unlock-keychain -p ci-keychain build.keychain
          security import /tmp/cert.p12 -k build.keychain \
            -P "$MACOS_CERT_PASSWORD" -T /usr/bin/codesign
          security set-key-partition-list -S apple-tool:,apple:,codesign: \
            -s -k ci-keychain build.keychain
          # self-signed цепочка не доверена на раннере — добавляем в trust store,
          # иначе codesign может отказаться подписывать этой identity:
          security find-certificate -c "Krab Ear CI Release" -p build.keychain > /tmp/cert.pem
          sudo security add-trusted-cert -d -r trustRoot \
            -k /Library/Keychains/System.keychain /tmp/cert.pem
          rm -f /tmp/cert.p12 /tmp/cert.pem

      - name: Cache SPM
        uses: actions/cache@v4
        with:
          path: native/KrabEarAgent/.build
          key: release-spm-${{ runner.os }}-${{ hashFiles('native/KrabEarAgent/Package.resolved') }}

      - name: Build Swift agent
        run: cd native/KrabEarAgent && swift build -c release

      - name: Assemble + sign .app
        run: |
          scripts/assemble_signed_app.sh \
            --output dist \
            --version "${{ steps.ver.outputs.version }}" \
            --identity "Krab Ear CI Release"

      - name: Zip (ditto — сохраняет симлинки внутри framework)
        run: |
          VERSION="${{ steps.ver.outputs.version }}"
          ditto -c -k --keepParent "dist/Krab Ear.app" "dist/Krab-Ear-v$VERSION.zip"
          shasum -a 256 "dist/Krab-Ear-v$VERSION.zip" > "dist/Krab-Ear-v$VERSION.zip.sha256"

      - name: Sparkle sign_update
        id: sparkle
        env:
          SPARKLE_PRIVATE_KEY: ${{ secrets.SPARKLE_PRIVATE_KEY }}
        run: |
          VERSION="${{ steps.ver.outputs.version }}"
          curl -L -o /tmp/sparkle.tar.xz \
            "https://github.com/sparkle-project/Sparkle/releases/download/${SPARKLE_TOOLS_VERSION}/Sparkle-${SPARKLE_TOOLS_VERSION}.tar.xz"
          mkdir -p /tmp/sparkle-tools && tar -xJf /tmp/sparkle.tar.xz -C /tmp/sparkle-tools
          printf '%s' "$SPARKLE_PRIVATE_KEY" > /tmp/ed_key
          chmod 600 /tmp/ed_key
          SIGN_OUT=$(/tmp/sparkle-tools/bin/sign_update -f /tmp/ed_key "dist/Krab-Ear-v$VERSION.zip")
          rm -f /tmp/ed_key
          echo "sign_update: $SIGN_OUT"
          ED_SIG=$(echo "$SIGN_OUT" | sed -n 's/.*sparkle:edSignature="\([^"]*\)".*/\1/p')
          LENGTH=$(echo "$SIGN_OUT" | sed -n 's/.*length="\([^"]*\)".*/\1/p')
          [ -n "$ED_SIG" ] && [ -n "$LENGTH" ] \
            || { echo "::error::не распарсили вывод sign_update"; exit 1; }
          echo "ed_sig=$ED_SIG" >> "$GITHUB_OUTPUT"
          echo "length=$LENGTH" >> "$GITHUB_OUTPUT"

      - name: Update appcast.xml (монотонность версии проверяет сам скрипт)
        run: |
          VERSION="${{ steps.ver.outputs.version }}"
          git checkout codex/krab-ear-v2
          python3 scripts/generate_appcast_item.py \
            --appcast appcast.xml \
            --version "$VERSION" \
            --url "https://github.com/${{ github.repository }}/releases/download/v$VERSION/Krab-Ear-v$VERSION.zip" \
            --ed-signature "${{ steps.sparkle.outputs.ed_sig }}" \
            --length "${{ steps.sparkle.outputs.length }}"

      - name: Create GitHub Release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          VERSION="${{ steps.ver.outputs.version }}"
          gh release create "v$VERSION" \
            "dist/Krab-Ear-v$VERSION.zip" \
            "dist/Krab-Ear-v$VERSION.zip.sha256" \
            --title "Krab Ear v$VERSION" \
            --generate-notes

      - name: Commit appcast (после успешного релиза; [skip ci])
        run: |
          VERSION="${{ steps.ver.outputs.version }}"
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add appcast.xml
          git commit -m "release: appcast v$VERSION [skip ci]"
          git pull --rebase origin codex/krab-ear-v2
          git push origin codex/krab-ear-v2
```

Порядок: appcast-файл правится ДО `gh release create` (валидация монотонности убивает джоб до публикации), но КОММИТИТСЯ ПОСЛЕ (клиенты не должны увидеть appcast-item раньше, чем ассет станет скачиваемым).

- [ ] **Step 2: Валидация YAML**

Run: `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/release.yml')); print('OK')"`
Expected: OK.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "feat(sparkle): release-workflow (тег/dispatch → сборка → GH Release → appcast)"
```

---

### Task 8: Документация

**Files:**
- Modify: `docs/DISTRIBUTION.md` (новая секция «Автообновления (Sparkle)»)
- Modify: `CLAUDE.md` (короткая запись: Sparkle-клиент + release.yml + dev-guard + framework-нюанс)
- Modify: `RELEASE_CHECKLIST.md` (пункт: релиз теперь = тег `vX.Y.Z` по зелёному CI)

- [ ] **Step 1: DISTRIBUTION.md — секция после «Требования на целевой машине»**

Содержание (полный текст пишет исполнитель по этим пунктам, ~30 строк):
- Как выпустить релиз: `git tag v2.4.1 && git push origin v2.4.1` (или Actions → release → Run workflow); guard не выпустит без зелёного krab-ear-ci.
- Что получает пользователь: приложение раз в сутки проверяет appcast, диалог Sparkle, «Проверить обновления…» в меню.
- Dev-машина: Sparkle отключён guard'ом (бандл в репо), путь обновления — build_and_deploy.command.
- Секреты CI: MACOS_CERT_P12 / MACOS_CERT_PASSWORD / SPARKLE_PRIVATE_KEY; ротация = повторить Task 2.
- Ограничение: репо обязан оставаться публичным (release-ассеты приватного репо не скачиваются анонимно).
- TODO при получении Developer ID: вернуть `--options runtime --entitlements` в assemble_signed_app.sh (notarize-путь) — одна строка.

- [ ] **Step 2: CLAUDE.md — в раздел Swift-агент дописать пункт**

```markdown
- **`main+SparkleUpdater.swift`** (2026-07-05) — автообновления Sparkle 2 (SPM). 🔴 Dev-guard: updater НЕ инициализируется, когда `.app` лежит в каталоге проекта (рядом есть `KrabEar/backend/service.py`) — иначе Sparkle переписал бы git-дерево владельца in-place; работает только для установленных копий (DMG-получатели, /Applications). 🔴 Sparkle — динамический framework: `Sparkle.framework` обязан лежать в `Contents/Frameworks/` бандла (коммитится, как parity-бинарь) и в `native/Frameworks/` для dev-бинаря (gitignored); rpath `@executable_path/../Frameworks` в Package.swift; копирование делают `build_and_deploy.command` и `scripts/assemble_signed_app.sh` (общий ассемблер, используется DMG-скриптом и `.github/workflows/release.yml`). Релиз: тег `vX.Y.Z` или workflow_dispatch → CI-green guard (krab-ear-ci) → GH Release + appcast.xml-коммит `[skip ci]`. Меню «Update Channel» (stable/beta) — по-прежнему вестигиальное, к Sparkle НЕ подключено (один appcast).
```

- [ ] **Step 3: verify_claude_md + commit**

Run: `python3 scripts/verify_claude_md.py`
Expected: OK.

```bash
git add docs/DISTRIBUTION.md CLAUDE.md RELEASE_CHECKLIST.md
git commit -m "docs(sparkle): автообновления в DISTRIBUTION/CLAUDE/RELEASE_CHECKLIST"
```

---

### Task 9: Пуш, CI, parity, первый релиз (смок)

**Files:** нет новых.

- [ ] **Step 1: Пуш всех коммитов волны**

```bash
git push origin codex/krab-ear-v2
```

Дождаться зелёного `krab-ear-ci` И `CI` (включая новый python-тест + Swift build):
Run: `gh run list --branch codex/krab-ear-v2 --limit 2`

- [ ] **Step 2: Смок-релиз v2.4.0 через workflow_dispatch**

```bash
gh workflow run release.yml -f version=2.4.0
gh run watch $(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

Expected: джоб зелёный; далее проверить артефакты:

```bash
gh release view v2.4.0 --json assets --jq '.assets[].name'
# ожидаем: Krab-Ear-v2.4.0.zip + .sha256
curl -sI "https://github.com/Pavua/Krab-Ear/releases/download/v2.4.0/Krab-Ear-v2.4.0.zip" | head -1
# ожидаем: HTTP 302 (анонимная загрузка работает)
git pull   # подтянуть appcast-коммит бота
python3 -c "import xml.etree.ElementTree as ET; ET.parse('appcast.xml'); print('appcast OK')"
```

- [ ] **Step 3: Проверка zip-целостности релиза**

```bash
cd /tmp && curl -sL -o krab-test.zip \
  "https://github.com/Pavua/Krab-Ear/releases/download/v2.4.0/Krab-Ear-v2.4.0.zip"
ditto -x -k krab-test.zip /tmp/krab-test-app
ls "/tmp/krab-test-app/Krab Ear.app/Contents/Frameworks/"   # Sparkle.framework
codesign --verify --deep "/tmp/krab-test-app/Krab Ear.app" && echo SIGNED-OK
defaults read "/tmp/krab-test-app/Krab Ear.app/Contents/Info" SUFeedURL
rm -rf /tmp/krab-test.zip /tmp/krab-test-app
```

- [ ] **Step 4: Живой e2e Sparkle-обновления (owner-assisted, можно отложить)**

Процедура для владельца (документируется, выполняется когда удобно):
1. Распаковать релизный zip v2.4.0 в `/Applications/Krab Ear.app`.
2. Остановить launchd-агент репо: `launchctl bootout gui/$(id -u)/ai.krab.ear.agent` (иначе SingleInstanceGuard убьёт вторую копию).
3. Запустить `/Applications/Krab Ear.app` — Sparkle активен (guard пропускает: рядом нет service.py).
4. Выпустить v2.4.1 (тег) → в меню «Проверить обновления…» → диалог Sparkle предлагает 2.4.1 → Install → приложение перезапускается на 2.4.1.
5. Убрать копию из /Applications, вернуть launchd: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.krab.ear.agent.plist`.

- [ ] **Step 5: Финальный parity-коммит (если build_and_deploy менял бинари) + память**

```bash
git status --short   # если бинари изменились:
git add "Krab Ear.app/Contents/MacOS/KrabEarAgent" && git add -f native/runtime/KrabEarAgent
git commit -m "build(native): parity после Sparkle-волны" && git push
```

Обновить память (project_launch_readiness: секция Sparkle-волны) и `.remember/remember.md`.

---

## Self-review (выполнен при написании)

- **Spec coverage:** one-time setup → Task 2; клиент → Tasks 3-5; workflow (все 10 шагов спеки) → Task 7 (валидация+guard шаги 1-2, keychain шаг 3, ассемблер шаг 4 → Task 6, штамп версии шаг 5 → ассемблер, codesign шаг 6, zip шаг 7, sign_update шаг 8, release шаг 9, appcast+skip-ci шаг 10); скелет appcast + генератор + unit-тест → Task 1; source-контракт → Task 5; смок → Task 9; доки → Task 8. Дополнительно к спеке (найдено разведкой, в спеку не противоречит): dev-guard in-repo бандла, framework-embedding/rpath.
- **Placeholder scan:** текст DISTRIBUTION-секции в Task 8 задан пунктами (не кодом) намеренно — это прозаическая дока; всё исполняемое дано полным кодом.
- **Type consistency:** `setupSparkleUpdater()`/`onCheckForUpdates`/`sparkleUpdaterController` — согласованы между Task 5 кодом, меню и тестами; `add_item(...)` сигнатура совпадает в скрипте и тестах; `assemble_signed_app.sh --output/--version/--identity` совпадает в Task 6 и Task 7.
