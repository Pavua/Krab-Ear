#!/bin/zsh
# ------------------------------------------------------------------
# Проверка целостности стабильного backup (S20):
# 1) ищет последний архив в _STABLE_BACKUPS;
# 2) сверяет sha256;
# 3) делает dry-list содержимого.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/_STABLE_BACKUPS"

LATEST_ARCHIVE="$(ls -1t "$BACKUP_DIR"/krabear_stable_*.tar.gz 2>/dev/null | head -n 1 || true)"
if [ -z "$LATEST_ARCHIVE" ]; then
  echo "Ошибка: не найдено backup-архивов в $BACKUP_DIR"
  exit 1
fi

LATEST_SHA="${LATEST_ARCHIVE%.tar.gz}.sha256"
if [ ! -f "$LATEST_SHA" ]; then
  echo "Ошибка: checksum-файл не найден: $LATEST_SHA"
  exit 1
fi

pushd "$BACKUP_DIR" >/dev/null
if ! shasum -a 256 -c "$(basename "$LATEST_SHA")" >/dev/null; then
  popd >/dev/null
  echo "❌ Backup checksum mismatch"
  exit 1
fi
popd >/dev/null

echo "✅ Backup checksum OK: $LATEST_ARCHIVE"
echo "Содержимое (первые 40 строк):"
tar -tzf "$LATEST_ARCHIVE" | head -n 40
