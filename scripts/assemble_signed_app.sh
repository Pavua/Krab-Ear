#!/bin/zsh
# assemble_signed_app.sh — единый ассемблер .app бандла Krab Ear.
# Используется build_distribution_dmg.command И release.yml (CI) — DRY,
# spec 2026-07-05-sparkle-auto-update (шаг 4 workflow).
#
# Usage:
#   scripts/assemble_signed_app.sh --output <dir> --version <X.Y.Z> --identity <name|-> [--vendor-dir <dir>]
#
# Делает: копия шаблона "Krab Ear.app" → свежий бинарь из .build/release →
# Sparkle.framework в Contents/Frameworks → bootstrap-инсталлятор в Resources →
# (опционально) bundled Python-рантайм в Contents/Resources/vendor →
# штамп версии → codesign --deep. Сборку Swift НЕ делает — caller обязан
# выполнить `swift build -c release` заранее. Bundled-рантайм собирается
# отдельно через build_bundled_runtime.command и передаётся --vendor-dir —
# ассемблер сам его не строит (пара минут pip install, не для каждого
# dev-прогона).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NATIVE_DIR="$ROOT_DIR/native/KrabEarAgent"
APP_TEMPLATE="$ROOT_DIR/Krab Ear.app"

OUTPUT_DIR="" VERSION="" IDENTITY="" VENDOR_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)     OUTPUT_DIR="$2"; shift 2 ;;
    --version)    VERSION="$2";    shift 2 ;;
    --identity)   IDENTITY="$2";   shift 2 ;;
    # Опционально: bundled Python-рантайм (scripts/build_bundled_runtime.command
    # --output <dir>) — закрывает T2/T3 из onboarding-аудита (нет system
    # Python >= 3.12 на чистом Mac). Опционален осознанно: build_distribution_dmg.command
    # использует ассемблер и для быстрых dev-итераций, где полная сборка
    # рантайма (пара минут pip install) — лишняя стоимость на каждый прогон.
    --vendor-dir) VENDOR_DIR="$2";  shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$OUTPUT_DIR" && -n "$VERSION" && -n "$IDENTITY" ]] || {
  echo "Usage: $0 --output <dir> --version <X.Y.Z> --identity <name|-> [--vendor-dir <dir>]" >&2; exit 1; }
if [[ -n "$VENDOR_DIR" ]]; then
  [[ -f "$VENDOR_DIR/KrabEar/backend/service.py" ]] \
    || { echo "--vendor-dir $VENDOR_DIR не похож на bundled-рантайм (нет KrabEar/backend/service.py)" >&2; exit 1; }
  [[ -x "$VENDOR_DIR/.venv_krab_ear/bin/python" ]] \
    || { echo "--vendor-dir $VENDOR_DIR: нет исполняемого .venv_krab_ear/bin/python" >&2; exit 1; }
fi

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

if [[ -n "$VENDOR_DIR" ]]; then
  # ditto (не cp -R) — сохраняет симлинк .venv_krab_ear/bin/python -> python3,
  # тот же приём, что уже используется для Sparkle.framework выше.
  # native/KrabEarAgent/Sources/KrabEarAgent/main.swift::resolveProjectRoot
  # ищет ИМЕННО Contents/Resources/vendor (см. LaunchOptionsBundledVendorRootTests).
  rm -rf "$APP_OUT/Contents/Resources/vendor"
  ditto "$VENDOR_DIR" "$APP_OUT/Contents/Resources/vendor"
fi

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_OUT/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP_OUT/Contents/Info.plist"

if [[ "$IDENTITY" == "-" ]]; then
  codesign --deep --force -s - "$APP_OUT"
else
  codesign --deep --force --sign "$IDENTITY" "$APP_OUT"
fi
codesign --verify --deep "$APP_OUT"
echo "OK: $APP_OUT (v$VERSION, identity: $IDENTITY)"
