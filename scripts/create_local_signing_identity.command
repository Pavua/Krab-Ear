#!/bin/zsh
# ------------------------------------------------------------------
# ONE-TIME SETUP: Create a self-signed code signing identity
# "Krab Ear Dev Local" in the login Keychain.
#
# Зачем нужно:
#   Ad-hoc подпись (-s -) меняет cdhash при каждом rebuild → TCC
#   сбрасывает Accessibility/Microphone permissions после каждой
#   пересборки Swift-агента.
#   Self-signed identity даёт stable identifier → TCC matches
#   по CFBundleIdentifier, а не cdhash → permissions persist.
#
# Usage:
#   ./scripts/create_local_signing_identity.command
#   ./scripts/create_local_signing_identity.command --dry-run
#
# После запуска:
#   1. Keychain спросит пароль при первом подписании — разрешите "Always Allow".
#   2. Пересоберите агент: ./scripts/update_agent.command
#   3. Убедитесь, что identity видна: security find-identity -v -p codesigning
# ------------------------------------------------------------------

set -euo pipefail

IDENTITY_NAME="Krab Ear Dev Local"
KEYCHAIN="login.keychain-db"
DRY_RUN=0

# Parse args
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --help|-h)
      echo "Usage: $0 [--dry-run]"
      echo "  --dry-run   Print steps without executing"
      exit 0
      ;;
  esac
done

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] $*"
  else
    eval "$@"
  fi
}

echo "=================================================="
echo " Krab Ear Dev — Local Code Signing Identity Setup"
echo "=================================================="
echo ""

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN mode: no changes will be made."
  echo ""
fi

# ── Check if identity already exists ──────────────────────────────
echo "Проверяю наличие identity \"$IDENTITY_NAME\" ..."
if security find-identity -v -p codesigning 2>/dev/null \
     | grep -q "$IDENTITY_NAME"; then
  echo ""
  echo "✓ Identity \"$IDENTITY_NAME\" уже существует в Keychain."
  echo "  Для пересоздания: security delete-identity -c \"$IDENTITY_NAME\""
  echo "  Затем запустите скрипт снова."
  echo ""
  security find-identity -v -p codesigning | grep "$IDENTITY_NAME" || true
  exit 0
fi

echo "  Identity не найдена. Создаю..."
echo ""

# ── Temp directory for OpenSSL artifacts ──────────────────────────
TMPDIR_WORK="$(mktemp -d /tmp/krabear_codesign.XXXXXX)"
KEY_FILE="$TMPDIR_WORK/krabear_dev.key"
CERT_CSR="$TMPDIR_WORK/krabear_dev.csr"
CERT_FILE="$TMPDIR_WORK/krabear_dev.crt"
P12_FILE="$TMPDIR_WORK/krabear_dev.p12"
EXT_FILE="$TMPDIR_WORK/codesign_ext.cnf"
P12_PASSWORD="krabear_local_dev"   # temp password for p12 bundle; not security-sensitive

cleanup() {
  rm -rf "$TMPDIR_WORK"
}
trap cleanup EXIT

# ── Step 1: Generate private key (RSA-2048) ────────────────────────
echo "Шаг 1/5: Генерирую private key (RSA-2048) ..."
run "openssl genrsa -out '$KEY_FILE' 2048 2>/dev/null"

# ── Step 2: Generate CSR ───────────────────────────────────────────
echo "Шаг 2/5: Создаю Certificate Signing Request ..."
run "openssl req -new \
  -key '$KEY_FILE' \
  -out '$CERT_CSR' \
  -subj '/CN=${IDENTITY_NAME}/O=Antigravity/OU=KrabEar Dev/C=US' \
  2>/dev/null"

# ── Step 3: Create v3 extensions for code signing ─────────────────
echo "Шаг 3/5: Настраиваю code signing extensions ..."
if [ "$DRY_RUN" -eq 0 ]; then
  cat > "$EXT_FILE" <<'EXTCNF'
[codesign_ext]
basicConstraints       = critical,CA:FALSE
keyUsage               = critical,digitalSignature
extendedKeyUsage       = critical,codeSigning
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
EXTCNF
else
  echo "[dry-run] Would write OpenSSL v3 extensions to $EXT_FILE"
fi

# ── Step 4: Self-sign the certificate (10 years) ──────────────────
echo "Шаг 4/5: Подписываю сертификат (self-signed, 3650 дней) ..."
run "openssl x509 -req \
  -days 3650 \
  -in '$CERT_CSR' \
  -signkey '$KEY_FILE' \
  -out '$CERT_FILE' \
  -extfile '$EXT_FILE' \
  -extensions codesign_ext \
  2>/dev/null"

# ── Step 5: Bundle into PKCS12 + import into login keychain ───────
echo "Шаг 5/5: Импортирую в login Keychain ..."
run "openssl pkcs12 -export \
  -out '$P12_FILE' \
  -inkey '$KEY_FILE' \
  -in '$CERT_FILE' \
  -name '$IDENTITY_NAME' \
  -passout pass:${P12_PASSWORD} \
  2>/dev/null"

run "security import '$P12_FILE' \
  -k '$KEYCHAIN' \
  -P '$P12_PASSWORD' \
  -T /usr/bin/codesign \
  -f pkcs12"

# Trust the certificate for code signing
run "security add-trusted-cert \
  -d \
  -r trustRoot \
  -k '$KEYCHAIN' \
  '$CERT_FILE'"

echo ""
if [ "$DRY_RUN" -eq 0 ]; then
  echo "=================================================="
  echo " Готово! Identity создана:"
  security find-identity -v -p codesigning | grep "$IDENTITY_NAME" || \
    echo "  (refresh Keychain Access или перезапустите shell)"
  echo ""
  echo "Следующие шаги:"
  echo "  1. Пересоберите агент:"
  echo "     ./scripts/update_agent.command"
  echo "     (скрипт автоматически использует \"$IDENTITY_NAME\")"
  echo ""
  echo "  2. При первом codesign macOS покажет Keychain-диалог."
  echo "     Выберите «Всегда разрешать» (Always Allow)."
  echo ""
  echo "  3. После rebuild — Accessibility/Microphone permissions"
  echo "     НЕ должны сбрасываться при последующих пересборках."
  echo ""
  echo "  Если что-то пошло не так:"
  echo "     security delete-identity -c \"$IDENTITY_NAME\""
  echo "     # затем запустите скрипт снова"
  echo "=================================================="
else
  echo "[dry-run] Все шаги выполнены без изменений."
  echo "Уберите флаг --dry-run для реального выполнения."
fi
