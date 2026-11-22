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

# Сверяем хэш САМОГО архива с первым полем sidecar. `shasum -c` проверял бы файл,
# названный внутри sidecar (может быть другим/перемещённым) и сообщал успех за архив.
EXPECTED_SHA="$(awk 'NR==1 {print $1}' "$LATEST_SHA")"
ACTUAL_SHA="$(shasum -a 256 "$LATEST_ARCHIVE" | awk '{print $1}')"
if [ -z "$EXPECTED_SHA" ] || [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
  echo "❌ Backup checksum mismatch"
  exit 1
fi

echo "✅ Backup checksum OK: $LATEST_ARCHIVE"
echo "Содержимое (первые 40 строк):"
tar -tzf "$LATEST_ARCHIVE" | sed -n '1,40p' || true
