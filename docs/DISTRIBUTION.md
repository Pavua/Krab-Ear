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

## Требования на целевой машине (текущая сборка)

> ⚠️ **Текущий DMG содержит ТОЛЬКО нативный menu-bar агент (Swift).** Python-backend (распознавание речи, история, перевод, все IPC-функции) в бандл НЕ входит — он ставится на целевой машине **bootstrap-инсталлятором** (см. следующий подраздел) или вручную по списку ниже. На «чистом» Mac первый запуск покажет «Krab Ear: backend не установлен» и подсветит инсталлятор в Finder.

### Автоустановка backend (bootstrap-инсталлятор)

DMG-сборка несёт `bootstrap_backend.command` внутри `Krab Ear.app/Contents/Resources`. Когда агент на чистом Mac не находит backend, он подсвечивает этот файл в Finder — получателю достаточно:

1. Дважды щёлкнуть `bootstrap_backend.command` (откроется Terminal). Если Gatekeeper блокирует скрипт из скачанного DMG — ПКМ (Ctrl+щелчок) → **Открыть**, как и с самим приложением.
2. Дождаться окончания (скрипт скачает код, создаст venv, поставит зависимости — несколько ГБ, нужна сеть; спросит HF-токен для диаризации — Enter, чтобы пропустить).
3. Открыть **Krab Ear** заново.

Что делает скрипт (идемпотентно, без sudo): проверяет Apple Silicon → ищет Python ≥ 3.12 (Homebrew/python.org; если нет — печатает, как поставить) → клонирует репозиторий в `~/KrabEar` (git; fallback: curl-tarball) → создаёт `.venv_krab_ear` + `pip install -r requirements.txt` → пишет указатель `~/Library/Application Support/KrabEar/project_root` (по нему агент из `/Applications` находит backend) → ставит launchd-сервис backend.

Переопределения через env: `KRAB_EAR_INSTALL_DIR`, `KRAB_EAR_REPO_URL`, `KRAB_EAR_BRANCH`; флаг `--dry-run` печатает план без изменений. Скрипт можно запускать и напрямую из репозитория (`scripts/bootstrap_backend.command`).

### Ручная установка (эквивалент того, что делает инсталлятор)

Что должно быть на Mac получателя до первого запуска:

1. **Каталог проекта Krab Ear** (клон репозитория) — агент ищет `KrabEar/backend/service.py` в таком порядке: аргумент `--project-root`, env `KRAB_EAR_PROJECT_ROOT`, текущая директория, до 8 уровней вверх от исполняемого файла. Самый простой надёжный вариант — задать `KRAB_EAR_PROJECT_ROOT` или запускать агент из каталога проекта.
2. **Python-окружение** `.venv_krab_ear` внутри каталога проекта — создаётся `Start Krab Ear.command` (или вручную: `python3.12+ -m venv .venv_krab_ear && pip install -r KrabEar/requirements.txt`). Без venv агент падает на системный `/usr/bin/python3`, в котором нет зависимостей (mlx-whisper и т.д.).
3. **launchd-сервис backend** (рекомендуется): `scripts/install_backend_launchagent.command` — иначе агент поднимает backend сам в active-режиме.
4. **STT-модели**: загружаются онбордингом при первом запуске (нужна сеть). Прод-plist включает `HF_HUB_OFFLINE=1` — загрузчик моделей временно снимает офлайн-режим на время пользовательской загрузки, дополнительных действий не требуется.
5. **ffmpeg** (`brew install ffmpeg`) — опционально: нужен только для импорта аудиофайлов; диктовка работает без него.
6. **Диаризация говорящих** — опционально: требует HF-токен и принятие лицензии pyannote (см. `docs/USER_ACTION_CHECKLIST.md`); без токена молча отключается, транскрибация работает.

Если backend не найден и не запущен, агент показывает целевое сообщение «Krab Ear: backend не установлен» сразу при старте (вместо прежнего 6–20-секундного таймаута с «backend недоступен»).

### Дорожная карта к self-contained DMG

Статус вариантов: **(b) bootstrap-инсталлятор первого запуска — РЕАЛИЗОВАН** (подраздел «Автоустановка backend» выше: лёгкий DMG, сеть обязательна при установке); **(c)** ручная установка «две части» — задокументирована ниже как эквивалент; **(a)** встроить Python-runtime + backend в `Contents/Resources` (python-build-standalone/Briefcase; +сотни МБ к DMG, нотаризация всех нативных модулей) — не реализовано, отложено.

---

## How recipients install Krab Ear

1. Download `Krab-Ear-vX.Y.Z.dmg`.
2. Double-click the DMG → drag **Krab Ear** into the **Applications** folder.
3. Eject the DMG.
4. First launch: double-click **Krab Ear** in Applications (or Spotlight).
   - **На чистом Mac** появится «backend не установлен», а в Finder подсветится `bootstrap_backend.command` — запустите его двойным щелчком, дождитесь окончания и откройте Krab Ear снова (см. «Автоустановка backend» выше).
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
