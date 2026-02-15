#!/bin/zsh
# ------------------------------------------------------------------
# Создаёт стабильный резервный снимок Krab Ear.
# Формат: tar.gz + sha256 + metadata.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/_STABLE_BACKUPS"
TS="$(date +%Y%m%d_%H%M%S)"
NAME="krabear_stable_${TS}"
STAGE_DIR="$BACKUP_DIR/.stage_${NAME}"
ARCHIVE_PATH="$BACKUP_DIR/${NAME}.tar.gz"
SHA_PATH="$BACKUP_DIR/${NAME}.sha256"
META_PATH="$BACKUP_DIR/${NAME}.metadata.txt"

mkdir -p "$BACKUP_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

copy_if_exists() {
  local target="$1"
  if [ -e "$ROOT_DIR/$target" ]; then
    rsync -a "$ROOT_DIR/$target" "$STAGE_DIR/"
  fi
}

# Основной рабочий контур проекта.
copy_if_exists "KrabEar"
copy_if_exists "native"
copy_if_exists "scripts"
copy_if_exists "docs"
copy_if_exists "README.md"
copy_if_exists "Start Krab Ear.command"
copy_if_exists "Open Krab Ear Panel.command"
copy_if_exists "Enable Krab Ear Autostart.command"
copy_if_exists "Disable Krab Ear Autostart.command"

# Сохраняем срез окружения для повторяемости.
if [ -x "$ROOT_DIR/.venv_krab_ear/bin/python" ]; then
  (
    source "$ROOT_DIR/.venv_krab_ear/bin/activate"
    python -m pip freeze > "$STAGE_DIR/pip-freeze.txt" || true
  )
fi

{
  GIT_BRANCH="n/a"
  GIT_COMMIT="n/a"
  if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_BRANCH="$(git -C "$ROOT_DIR" branch --show-current 2>/dev/null || echo 'n/a')"
    if git -C "$ROOT_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
      GIT_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || echo 'n/a')"
    fi
  fi

  echo "backup_name=$NAME"
  echo "created_at=$(date -Iseconds)"
  echo "root_dir=$ROOT_DIR"
  echo "host=$(scutil --get ComputerName 2>/dev/null || hostname)"
  echo "user=$(whoami)"
  echo "git_branch=$GIT_BRANCH"
  echo "git_commit=$GIT_COMMIT"
  echo ""
  echo "=== git status --short ==="
  git -C "$ROOT_DIR" status --short 2>/dev/null || true
} > "$META_PATH"

# Упаковываем только stage, чтобы не тащить лишние/временные файлы.
tar -czf "$ARCHIVE_PATH" -C "$STAGE_DIR" .
shasum -a 256 "$ARCHIVE_PATH" > "$SHA_PATH"

rm -rf "$STAGE_DIR"

echo "✅ Резервная копия создана"
echo "Архив:   $ARCHIVE_PATH"
echo "Checksum: $SHA_PATH"
echo "Metadata: $META_PATH"
