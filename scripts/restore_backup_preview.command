#!/bin/zsh
# ------------------------------------------------------------------
# Preview восстановления backup (S20):
# - не перезаписывает проект;
# - распаковывает последний backup во временный каталог;
# - показывает дерево и потенциальные точки восстановления.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/_STABLE_BACKUPS"
PREVIEW_ROOT="$BACKUP_DIR/_restore_preview"
TS="$(date +%Y%m%d_%H%M%S)"
TARGET_DIR="$PREVIEW_ROOT/$TS"

LATEST_ARCHIVE="$(ls -1t "$BACKUP_DIR"/krabear_stable_*.tar.gz 2>/dev/null | head -n 1 || true)"
if [ -z "$LATEST_ARCHIVE" ]; then
  echo "Ошибка: не найдено backup-архивов в $BACKUP_DIR"
  exit 1
fi

mkdir -p "$TARGET_DIR"
tar -xzf "$LATEST_ARCHIVE" -C "$TARGET_DIR"

echo "✅ Restore preview готов"
echo "Backup: $LATEST_ARCHIVE"
echo "Preview dir: $TARGET_DIR"
echo "Содержимое (верхний уровень):"
find "$TARGET_DIR" -maxdepth 2 -mindepth 1 | sed -n '1,80p'
