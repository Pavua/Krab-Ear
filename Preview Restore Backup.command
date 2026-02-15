#!/bin/zsh
# One-click preview восстановления последнего backup.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT_DIR/scripts/restore_backup_preview.command" "$@"
