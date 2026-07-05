#!/bin/zsh
# ------------------------------------------------------------------
# build_and_deploy.command — Swift build → sign both binaries → dSYM upload
#
# Автоматизирует workflow после редактирования Swift-агента:
#   1. swift build -c release
#   2. cp → native/runtime/KrabEarAgent
#   3. cp → Krab Ear.app/Contents/MacOS/KrabEarAgent
#   4. codesign (stable identity или ad-hoc fallback)
#   5. UUID match-check между binary и .dSYM
#   6. sentry-cli debug-files upload
#   7. macOS notification (osascript)
#
# Usage:
#   ./scripts/build_and_deploy.command
#   ./scripts/build_and_deploy.command --no-sentry     # skip dSYM upload
#   ./scripts/build_and_deploy.command --dry-run       # print steps, no build
#   ./scripts/build_and_deploy.command --help
#
# Exit codes:
#   0  success
#   1  build failed
#   2  codesign failed
#   3  UUID mismatch (binary vs .dSYM)
#   4  sentry-cli upload failed
#
# Токен Sentry читается из:
#   ~/Antigravity_AGENTS/Краб/.env  →  SENTRY_AUTH_TOKEN=...
#   либо из переменной окружения SENTRY_AUTH_TOKEN (если уже задана)
# ------------------------------------------------------------------

set -uo pipefail
# Note: не используем set -e чтобы иметь явный контроль над exit codes

# ── Locate repo root ──────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PACKAGE_DIR="$ROOT_DIR/native/KrabEarAgent"
BUILD_BIN="$PACKAGE_DIR/.build/release/KrabEarAgent"
DSYM_PATH="$PACKAGE_DIR/.build/release/KrabEarAgent.dSYM"
RUNTIME_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"
APP_BUNDLE="$ROOT_DIR/Krab Ear.app"
APP_BIN="$APP_BUNDLE/Contents/MacOS/KrabEarAgent"
BUNDLE_ID="com.antigravity.krab-ear"

SENTRY_ORG="po-zm"
SENTRY_PROJECT="krab-ear-agent"
SENTRY_URL="https://de.sentry.io"
KRAB_ENV_FILE="$HOME/Antigravity_AGENTS/Краб/.env"

# ── Flags ─────────────────────────────────────────────────────────
DRY_RUN=0
SKIP_SENTRY=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY_RUN=1 ;;
    --no-sentry)  SKIP_SENTRY=1 ;;
    --help|-h)
      echo "Usage: $0 [--dry-run] [--no-sentry]"
      echo ""
      echo "  --dry-run     Print steps without executing (safe inspection)"
      echo "  --no-sentry   Skip dSYM upload to Sentry"
      echo ""
      echo "Exit codes:"
      echo "  0  success"
      echo "  1  build failed"
      echo "  2  codesign failed"
      echo "  3  UUID mismatch between binary and .dSYM"
      echo "  4  sentry-cli upload failed"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Run with --help for usage." >&2
      exit 1
      ;;
  esac
done

# ── Colors ────────────────────────────────────────────────────────
BOLD='\033[1m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

section() { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${NC}"; }
log()     { echo -e "${BLUE}  →${NC} $*"; }
ok()      { echo -e "${GREEN}  ✓${NC} $*"; }
warn()    { echo -e "${YELLOW}  ⚠${NC} $*"; }
fail()    { echo -e "${RED}  ✗${NC} $*" >&2; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo -e "  ${YELLOW}[dry-run]${NC} $*"
  else
    eval "$@"
  fi
}

# ── Header ────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Krab Ear — Build & Deploy${NC}"
if [ "$DRY_RUN" -eq 1 ]; then
  echo -e "${YELLOW}  DRY-RUN mode — no changes will be made${NC}"
fi
echo -e "  Root: $ROOT_DIR"
echo ""

# ── Preflight checks ──────────────────────────────────────────────
section "Preflight"

for tool in swift codesign dwarfdump; do
  if command -v "$tool" &>/dev/null; then
    ok "$tool: $(command -v $tool)"
  else
    fail "Required tool not found: $tool"
    fail "Install Xcode Command Line Tools: xcode-select --install"
    exit 1
  fi
done

if [ ! -d "$PACKAGE_DIR" ]; then
  fail "Swift package not found: $PACKAGE_DIR"
  exit 1
fi

if [ ! -d "$APP_BUNDLE" ]; then
  fail "App bundle not found: $APP_BUNDLE"
  exit 1
fi

ok "Repo root: $ROOT_DIR"

# ── Step 1: Swift build ───────────────────────────────────────────
section "Step 1/5 — Swift Build"
log "swift build -c release --package-path $PACKAGE_DIR"

if [ "$DRY_RUN" -eq 0 ]; then
  swift build -c release --package-path "$PACKAGE_DIR"
  BUILD_EXIT=$?
  if [ $BUILD_EXIT -ne 0 ]; then
    fail "Swift build failed (exit $BUILD_EXIT)"
    exit 1
  fi
  if [ ! -x "$BUILD_BIN" ]; then
    fail "Build artifact not found: $BUILD_BIN"
    exit 1
  fi
  ok "Build artifact: $BUILD_BIN"
  BINARY_SIZE=$(du -sh "$BUILD_BIN" | cut -f1)
  ok "Binary size: $BINARY_SIZE"

  # ── Generate .dSYM from embedded DWARF ──────────────────────────
  # SPM release builds keep DWARF inside the binary but do NOT emit a
  # standalone .dSYM bundle. Without this dsymutil step, Step 4 always
  # warns "dSYM not found" → SKIP_SENTRY=1 → every AppHang in Sentry
  # stays unsymbolicated (the perpetual AGENT-5/AGENT-B triage blocker).
  log "dsymutil → $DSYM_PATH"
  rm -rf "$DSYM_PATH"
  if dsymutil "$BUILD_BIN" -o "$DSYM_PATH" 2>/dev/null && [ -d "$DSYM_PATH" ]; then
    ok "dSYM generated: $DSYM_PATH"
  else
    warn "dsymutil failed — Sentry symbolication will be unavailable"
    SKIP_SENTRY=1
  fi
else
  echo -e "  ${YELLOW}[dry-run]${NC} swift build -c release --package-path $PACKAGE_DIR"
  echo -e "  ${YELLOW}[dry-run]${NC} dsymutil $BUILD_BIN -o $DSYM_PATH"
fi

# ── Step 2: Copy to both delivery points ─────────────────────────
section "Step 2/5 — Sync Binaries"

log "→ native/runtime/KrabEarAgent"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$(dirname "$RUNTIME_BIN")"
  if ! cp -f "$BUILD_BIN" "$RUNTIME_BIN"; then
    fail "Failed to copy to runtime binary path"
    exit 1
  fi
  chmod +x "$RUNTIME_BIN"
  ok "native/runtime/KrabEarAgent updated"
else
  echo -e "  ${YELLOW}[dry-run]${NC} cp -f $BUILD_BIN $RUNTIME_BIN"
fi

log "→ Krab Ear.app/Contents/MacOS/KrabEarAgent"
if [ "$DRY_RUN" -eq 0 ]; then
  if ! cp -f "$BUILD_BIN" "$APP_BIN"; then
    fail "Failed to copy to app bundle"
    exit 1
  fi
  chmod +x "$APP_BIN"
  ok "App bundle binary updated"
else
  echo -e "  ${YELLOW}[dry-run]${NC} cp -f $BUILD_BIN $APP_BIN"
fi

log "→ Sparkle.framework (bundle + runtime)"
if [ "$DRY_RUN" -eq 0 ]; then
  # Sparkle — динамический framework: без него бинарь не стартует (dyld).
  SPARKLE_FW="$(find "$PACKAGE_DIR/.build" -type d -name "Sparkle.framework" 2>/dev/null | head -1)"
  if [[ -n "$SPARKLE_FW" ]]; then
    mkdir -p "$ROOT_DIR/Krab Ear.app/Contents/Frameworks" "$ROOT_DIR/native/Frameworks"
    rm -rf "$ROOT_DIR/Krab Ear.app/Contents/Frameworks/Sparkle.framework" \
           "$ROOT_DIR/native/Frameworks/Sparkle.framework"
    ditto "$SPARKLE_FW" "$ROOT_DIR/Krab Ear.app/Contents/Frameworks/Sparkle.framework"
    ditto "$SPARKLE_FW" "$ROOT_DIR/native/Frameworks/Sparkle.framework"
    ok "Sparkle.framework synced to bundle + native/Frameworks"
  else
    warn "Sparkle.framework not found in .build — skipping (Swift build may need re-run)"
  fi
else
  echo -e "  ${YELLOW}[dry-run]${NC} ditto <Sparkle.framework> \"Krab Ear.app/Contents/Frameworks/\" + native/Frameworks/"
fi

# ── Step 3: Code signing ──────────────────────────────────────────
section "Step 3/5 — Code Signing"

LOCAL_IDENTITY="Krab Ear Dev Local"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$LOCAL_IDENTITY"; then
  SIGN_ID="$LOCAL_IDENTITY"
  ok "Using stable identity: \"$SIGN_ID\" (TCC-safe, cdhash stable)"
else
  SIGN_ID="-"
  warn "Stable identity not found — using ad-hoc (-s -)"
  warn "TCC permissions may reset after rebuild."
  warn "Run: scripts/create_local_signing_identity.command"
fi

if [ "$DRY_RUN" -eq 0 ]; then
  # Sign runtime binary
  if ! codesign --force --sign "$SIGN_ID" --timestamp=none \
       --identifier "$BUNDLE_ID" "$RUNTIME_BIN" 2>/dev/null; then
    fail "codesign failed for runtime binary"
    exit 2
  fi
  ok "runtime binary signed"

  # Sign app bundle (signs binary inside too)
  if ! codesign --force --sign "$SIGN_ID" "$APP_BUNDLE" 2>/dev/null; then
    fail "codesign failed for app bundle"
    exit 2
  fi
  ok "App bundle signed"

  # Verify
  if codesign -v "$APP_BUNDLE" 2>/dev/null; then
    ok "Signature verified"
  else
    warn "codesign -v reported warnings (non-fatal for dev builds)"
  fi
else
  echo -e "  ${YELLOW}[dry-run]${NC} codesign --force --sign \"$SIGN_ID\" $RUNTIME_BIN"
  echo -e "  ${YELLOW}[dry-run]${NC} codesign --force --sign \"$SIGN_ID\" $APP_BUNDLE"
fi

# ── Step 4: UUID match check ──────────────────────────────────────
section "Step 4/5 — UUID Verification"

if [ "$DRY_RUN" -eq 0 ]; then
  if [ ! -d "$DSYM_PATH" ]; then
    warn "dSYM not found: $DSYM_PATH"
    warn "Build without dSYM? Check DWARF_DSYM_FOLDER_PATH in Package.swift."
    SKIP_SENTRY=1
  else
    BINARY_UUID=$(dwarfdump --uuid "$APP_BIN" 2>/dev/null | awk '{print $2}' | head -1)
    DSYM_UUID=$(dwarfdump --uuid "$DSYM_PATH" 2>/dev/null | awk '{print $2}' | head -1)

    log "Binary UUID: $BINARY_UUID"
    log "dSYM UUID:   $DSYM_UUID"

    if [ -z "$BINARY_UUID" ] || [ -z "$DSYM_UUID" ]; then
      fail "Could not read UUID(s) — dwarfdump failed"
      exit 3
    fi

    if [ "$BINARY_UUID" = "$DSYM_UUID" ]; then
      ok "UUID match confirmed — dSYM is valid for this build"
    else
      fail "UUID MISMATCH!"
      fail "  Binary: $BINARY_UUID"
      fail "  dSYM:   $DSYM_UUID"
      fail "Sentry upload skipped. Re-run swift build to regenerate dSYM."
      exit 3
    fi
  fi
else
  echo -e "  ${YELLOW}[dry-run]${NC} dwarfdump --uuid $APP_BIN"
  echo -e "  ${YELLOW}[dry-run]${NC} dwarfdump --uuid $DSYM_PATH"
  echo -e "  ${YELLOW}[dry-run]${NC} (UUID match check)"
fi

# ── Step 5: Sentry dSYM upload ────────────────────────────────────
section "Step 5/5 — Sentry dSYM Upload"

if [ "$SKIP_SENTRY" -eq 1 ]; then
  warn "Sentry upload skipped (--no-sentry or dSYM missing)"
else
  # Resolve sentry-cli path
  if command -v sentry-cli &>/dev/null; then
    SENTRY_CLI="$(command -v sentry-cli)"
    ok "sentry-cli: $SENTRY_CLI"
  else
    fail "sentry-cli not found in PATH"
    fail "Install: brew install getsentry/tools/sentry-cli"
    exit 4
  fi

  # Resolve auth token: env var → .env file
  if [ -n "${SENTRY_AUTH_TOKEN:-}" ]; then
    ok "SENTRY_AUTH_TOKEN: from environment"
  elif [ -f "$KRAB_ENV_FILE" ]; then
    SENTRY_AUTH_TOKEN=$(grep -E '^SENTRY_AUTH_TOKEN=' "$KRAB_ENV_FILE" \
                        | head -1 | cut -d= -f2- | tr -d '[:space:]"'"'")
    if [ -n "$SENTRY_AUTH_TOKEN" ]; then
      ok "SENTRY_AUTH_TOKEN: loaded from $KRAB_ENV_FILE"
    else
      fail "SENTRY_AUTH_TOKEN not found in $KRAB_ENV_FILE"
      fail "Add: SENTRY_AUTH_TOKEN=sntryu_... to that file"
      exit 4
    fi
  else
    fail "SENTRY_AUTH_TOKEN not set and .env not found: $KRAB_ENV_FILE"
    exit 4
  fi

  log "Uploading dSYM to Sentry..."
  log "  org=$SENTRY_ORG  project=$SENTRY_PROJECT  url=$SENTRY_URL"
  log "  dSYM: $DSYM_PATH"

  if [ "$DRY_RUN" -eq 0 ]; then
    UPLOAD_EXIT=0
    SENTRY_AUTH_TOKEN="$SENTRY_AUTH_TOKEN" \
    SENTRY_URL="$SENTRY_URL" \
    "$SENTRY_CLI" debug-files upload \
      --org "$SENTRY_ORG" \
      --project "$SENTRY_PROJECT" \
      "$DSYM_PATH" \
      2>&1 || UPLOAD_EXIT=$?

    if [ $UPLOAD_EXIT -ne 0 ]; then
      fail "sentry-cli upload failed (exit $UPLOAD_EXIT)"
      fail "Check token permissions: Project → Debug Files → write"
      exit 4
    fi
    ok "dSYM uploaded to Sentry"
  else
    echo -e "  ${YELLOW}[dry-run]${NC} SENTRY_AUTH_TOKEN=*** SENTRY_URL=$SENTRY_URL \\"
    echo -e "             sentry-cli debug-files upload --org $SENTRY_ORG --project $SENTRY_PROJECT $DSYM_PATH"
  fi
fi

# ── Final report ──────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Krab Ear build & deploy complete${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
if [ "$DRY_RUN" -eq 0 ]; then
  echo -e "  Binary:  ${BLUE}$APP_BIN${NC}"
  echo -e "  Runtime: ${BLUE}$RUNTIME_BIN${NC}"
  echo -e "  Signed:  \"$SIGN_ID\""
  if [ "$SKIP_SENTRY" -eq 0 ]; then
    echo -e "  Sentry:  ${GREEN}dSYM uploaded${NC} (org=$SENTRY_ORG, project=$SENTRY_PROJECT)"
  else
    echo -e "  Sentry:  ${YELLOW}skipped${NC}"
  fi
  echo ""
  # macOS notification
  osascript -e 'display notification "Build & deploy complete" with title "Krab Ear" subtitle "dSYM uploaded to Sentry"' 2>/dev/null || true
else
  echo -e "  ${YELLOW}Dry-run complete — no changes made.${NC}"
fi
echo ""
