#!/bin/zsh
# ------------------------------------------------------------------
# build_distribution_dmg.command — сборка distributable DMG для Krab Ear
#
# Usage:
#   ./scripts/build_distribution_dmg.command
#   ./scripts/build_distribution_dmg.command --no-notarize
#   ./scripts/build_distribution_dmg.command --rebuild
#   ./scripts/build_distribution_dmg.command --version 1.2.3
#   ./scripts/build_distribution_dmg.command --no-notarize --rebuild --version 2.0.0
#
# Notarization (автоматически если заданы env vars):
#   export APPLE_ID_EMAIL="you@example.com"
#   export APPLE_ID_PASSWORD="xxxx-xxxx-xxxx-xxxx"   # app-specific password
#   export APPLE_TEAM_ID="XXXXXXXXXX"
#
# Output:
#   dist/Krab-Ear-vX.Y.Z.dmg
#   dist/Krab-Ear-vX.Y.Z.dmg.sha256
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NATIVE_DIR="$ROOT_DIR/native/KrabEarAgent"
APP_BUNDLE_SRC="$ROOT_DIR/Krab Ear.app"
DIST_DIR="$ROOT_DIR/dist"

# ── Defaults ──────────────────────────────────────────────────────
DO_NOTARIZE=true
FORCE_REBUILD=false
VERSION=""
NO_NOTARIZE_FLAG=false

# ── Parse args ────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-notarize)
      NO_NOTARIZE_FLAG=true
      shift ;;
    --rebuild)
      FORCE_REBUILD=true
      shift ;;
    --version)
      VERSION="$2"
      shift 2 ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 [--no-notarize] [--rebuild] [--version X.Y.Z]" >&2
      exit 1 ;;
  esac
done

# ── Colors ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[DMG]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*" >&2; exit 1; }

# ── Detect version ────────────────────────────────────────────────
if [[ -z "$VERSION" ]]; then
  # Read from existing Info.plist
  VERSION=$(defaults read "$APP_BUNDLE_SRC/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo "1.0.0")
fi
log "Version: $VERSION"

DMG_NAME="Krab-Ear-v${VERSION}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"
APP_DIST="$DIST_DIR/Krab Ear.app"

# ── Decide notarization ───────────────────────────────────────────
if $NO_NOTARIZE_FLAG; then
  DO_NOTARIZE=false
  log "Notarization: SKIPPED (--no-notarize)"
elif [[ -n "${APPLE_ID_EMAIL:-}" && -n "${APPLE_ID_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
  DO_NOTARIZE=true
  log "Notarization: ENABLED (env vars present)"
else
  DO_NOTARIZE=false
  warn "Notarization: SKIPPED (APPLE_ID_EMAIL / APPLE_ID_PASSWORD / APPLE_TEAM_ID not set)"
  warn "DMG will be ad-hoc signed — works on THIS Mac only."
fi

# ── Verify required tools ─────────────────────────────────────────
log "Checking required tools..."
for tool in swift codesign hdiutil xcrun; do
  if ! command -v "$tool" &>/dev/null; then
    err "Required tool not found: $tool — install Xcode Command Line Tools via: xcode-select --install"
  fi
done
ok "All tools present"

# ── Step 1: Swift build ───────────────────────────────────────────
log "Building Swift agent (release, arm64)..."
if $FORCE_REBUILD; then
  log "Force rebuild: removing .build cache..."
  rm -rf "$NATIVE_DIR/.build"
fi
(cd "$NATIVE_DIR" && swift build -c release 2>&1) || err "Swift build failed"
ok "Swift build complete"

BUILT_BINARY="$NATIVE_DIR/.build/release/KrabEarAgent"
[[ -f "$BUILT_BINARY" ]] || err "Built binary not found: $BUILT_BINARY"

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

# ── Step 4: Build DMG ─────────────────────────────────────────────
log "Building DMG: $DMG_NAME..."
TMP_DMG_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DMG_DIR"' EXIT

# Copy app into staging area
cp -R "$APP_DIST" "$TMP_DMG_DIR/"

# Create Applications symlink
ln -s /Applications "$TMP_DMG_DIR/Applications"

# Create a simple background folder (no image required)
mkdir -p "$TMP_DMG_DIR/.background"

# Remove existing DMG if present
rm -f "$DMG_PATH"

hdiutil create \
  -volname "Krab Ear" \
  -srcfolder "$TMP_DMG_DIR" \
  -ov \
  -format UDZO \
  -fs HFS+ \
  "$DMG_PATH" || err "hdiutil create failed"

ok "DMG created: $DMG_PATH"

# ── Step 5: Notarize ──────────────────────────────────────────────
if $DO_NOTARIZE; then
  log "Submitting DMG to Apple notary service..."
  log "(This may take 1-10 minutes)"

  NOTARY_OUTPUT="$(xcrun notarytool submit "$DMG_PATH" \
    --apple-id "$APPLE_ID_EMAIL" \
    --password "$APPLE_ID_PASSWORD" \
    --team-id "$APPLE_TEAM_ID" \
    --wait 2>&1)"

  echo "$NOTARY_OUTPUT"

  if echo "$NOTARY_OUTPUT" | grep -q "status: Accepted"; then
    ok "Notarization accepted"
  else
    warn "Notarization may have failed — check output above"
    warn "You can check status with: xcrun notarytool log <submission-id> ..."
  fi

  log "Stapling notarization ticket to DMG..."
  xcrun stapler staple "$DMG_PATH" || warn "Stapler failed — DMG may still be notarized but without inline ticket"
  ok "Notarization stapled"
fi

# ── Step 6: SHA256 hash ───────────────────────────────────────────
log "Generating SHA256 hash..."
HASH_FILE="${DMG_PATH}.sha256"
shasum -a 256 "$DMG_PATH" > "$HASH_FILE"
HASH_VALUE="$(awk '{print $1}' "$HASH_FILE")"
ok "SHA256: $HASH_VALUE"
ok "Hash file: $HASH_FILE"

# ── Step 7: Sentry release tracking ──────────────────────────────
SENTRY_RELEASE_SCRIPT="$ROOT_DIR/scripts/sentry_create_release.py"
if [[ -f "$SENTRY_RELEASE_SCRIPT" ]]; then
  log "Creating Sentry release for version $VERSION..."
  if python3 "$SENTRY_RELEASE_SCRIPT" --version "$VERSION" --env production; then
    ok "Sentry release created"
  else
    warn "Sentry release creation failed (non-fatal — build is still valid)"
  fi
else
  warn "sentry_create_release.py not found — skipping Sentry release tracking"
fi

# ── Final report ──────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Krab Ear Distribution DMG ready${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "  File:    ${BLUE}$DMG_PATH${NC}"
echo -e "  Hash:    $HASH_VALUE"
echo -e "  Version: $VERSION"
if $DO_NOTARIZE; then
  echo -e "  Status:  ${GREEN}Notarized + Stapled${NC} (ready for distribution)"
else
  echo -e "  Status:  ${YELLOW}Ad-hoc signed${NC} (local Mac only — no Developer ID)"
fi
echo ""
