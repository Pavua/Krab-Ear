# Krab Ear — Distribution Guide

This guide explains how to build and distribute Krab Ear as a notarized `.dmg` for other Macs (or your own clean machine).

---

## Quick start (local / ad-hoc, no Apple Developer account)

```bash
cd /path/to/Krab-Ear
bash scripts/build_distribution_dmg.command --no-notarize
# → dist/Krab-Ear-v1.0.0.dmg
```

The resulting DMG is **ad-hoc signed** — it runs only on your Mac. Gatekeeper will block it on other machines unless the recipient right-clicks → Open the first time.

---

## Full notarized distribution (requires Apple Developer account, $99/yr)

### 1. Get an Apple Developer account

Sign up at <https://developer.apple.com/enroll/>. Individual account is sufficient ($99/yr). After enrollment you get a **Team ID** (10-character string like `AB12CD34EF`) visible at <https://developer.apple.com/account/#/membership>.

### 2. Create a Developer ID Application certificate

1. Open **Keychain Access** → Certificate Assistant → Request a Certificate from a Certificate Authority → save to disk.
2. In the Apple Developer portal go to **Certificates, Identifiers & Profiles** → Certificates → `+` → **Developer ID Application**.
3. Upload your CSR, download the `.cer`, double-click to install into Keychain.

Verify it is installed:
```bash
security find-identity -v -p codesigning | grep "Developer ID Application"
```

### 3. Generate an app-specific password

1. Go to <https://appleid.apple.com> → Sign In → App-Specific Passwords → Generate.
2. Label it something like `krab-ear-notarytool`.
3. Copy the generated password (`xxxx-xxxx-xxxx-xxxx`).

### 4. Export environment variables

Add to your shell profile (`~/.zshrc` or `~/.zprofile`) or set them before running the script:

```bash
export APPLE_ID_EMAIL="you@example.com"
export APPLE_ID_PASSWORD="xxxx-xxxx-xxxx-xxxx"   # app-specific password, NOT your Apple ID password
export APPLE_TEAM_ID="AB12CD34EF"
```

### 5. Build the notarized DMG

```bash
bash scripts/build_distribution_dmg.command
# → dist/Krab-Ear-v1.0.0.dmg  (notarized + stapled)
# → dist/Krab-Ear-v1.0.0.dmg.sha256
```

Notarization typically takes **1–5 minutes**. The script waits and staples the ticket automatically.

---

## Script flags

| Flag | Effect |
|------|--------|
| `--no-notarize` | Skip notarization even if env vars are set; produces ad-hoc DMG |
| `--rebuild` | Force clean Swift rebuild (removes `.build/` cache first) |
| `--version X.Y.Z` | Override version string written into `Info.plist` |

Examples:

```bash
# Bump version + notarize
bash scripts/build_distribution_dmg.command --version 1.2.0

# Force clean rebuild, skip notarization
bash scripts/build_distribution_dmg.command --rebuild --no-notarize

# All three
bash scripts/build_distribution_dmg.command --rebuild --version 2.0.0
```

---

## Output files

| File | Description |
|------|-------------|
| `dist/Krab-Ear-vX.Y.Z.dmg` | The distributable disk image |
| `dist/Krab-Ear-vX.Y.Z.dmg.sha256` | SHA-256 checksum for integrity verification |
| `dist/Krab Ear.app` | Assembled `.app` (intermediate, can be discarded) |

---

## How recipients install Krab Ear

1. Download `Krab-Ear-vX.Y.Z.dmg`.
2. Double-click the DMG → drag **Krab Ear** into the **Applications** folder.
3. Eject the DMG.
4. First launch: double-click **Krab Ear** in Applications (or Spotlight).
5. Grant permissions when prompted:
   - **Microphone** — required for voice recording.
   - **Accessibility** — required for auto-paste into the active app.
6. Press **Right Option** to start recording.

> **Gatekeeper note (ad-hoc builds only):** if the DMG is not notarized, the recipient must right-click → Open → Open on first launch.

---

## Открытие на другом Mac (ad-hoc сборка без нотаризации)

Если вы распространяете ad-hoc подписанную версию Krab Ear (т.е. собрали её без Apple Developer ID), то при попытке запустить приложение на другом Mac пользователь увидит сообщение «неопознанный разработчик» или «повреждён и не может быть открыт». Это нормальное поведение macOS Gatekeeper для unsigned/self-signed приложений.

### Правильный способ открыть приложение

1. Откройте **Finder** и перейдите в папку **Applications**.
2. Найдите **Krab Ear** в списке.
3. **Нажмите Ctrl+щёлкнуть** (или **ПКМ**) по **Krab Ear** → выберите **Открыть**.
4. В появившемся диалоге нажмите **Открыть**.
5. После первого запуска приложение становится доверенным — в следующие разы можно запускать двойным щелчком обычным образом.

### Если сообщение говорит «повреждён»

Если вы видите сообщение типа «"Krab Ear" повреждён и не может быть открыт», выполните в терминале одну команду для удаления карантина:

```bash
xattr -dr com.apple.quarantine /Applications/Krab\ Ear.app
```

Затем попробуйте запустить приложение снова (Ctrl+щёлкнуть → **Открыть**).

### Полная фиксация вместо ad-hoc (требуется Apple Developer ID)

Чтобы избежать этих предупреждений полностью, app нужно подписать Developer ID и отправить на нотаризацию Apple. Это требует:
- Apple Developer account ($99/год) — см. раздел выше.
- Запуск скрипта сборки С нотаризацией (env vars + `--version X.Y.Z`).

Для локального распространения (ad-hoc) достаточно инструкции выше.

---

## Verifying the download

```bash
# On the recipient's Mac:
shasum -a 256 ~/Downloads/Krab-Ear-v1.0.0.dmg
# Compare with the published .sha256 file content
```

---

## Troubleshooting

### "Developer ID Application" certificate not found
Run `security find-identity -v -p codesigning` and verify the cert is present and not expired. Re-download from the Apple Developer portal if missing.

### Notarytool submission rejected
Check the detailed log returned by `notarytool`:
```bash
xcrun notarytool log <submission-id> \
  --apple-id $APPLE_ID_EMAIL \
  --password $APPLE_ID_PASSWORD \
  --team-id $APPLE_TEAM_ID
```
Common causes: hardened runtime not enabled (already handled by the script), unsigned frameworks bundled inside `.app`.

### `xcrun notarytool: command not found`
Ensure Xcode Command Line Tools are installed: `xcode-select --install`. `notarytool` requires Xcode 13+ or Xcode CLT 13+.
