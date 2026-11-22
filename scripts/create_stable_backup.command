#!/bin/zsh
# ------------------------------------------------------------------
# Создаёт стабильный резервный снимок Krab Ear.
# Формат: tar.gz + sha256 + metadata.
# ------------------------------------------------------------------

set -euo pipefail
# Архив содержит исходники проекта: права только владельцу (и каталог, и файлы).
umask 077

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/_STABLE_BACKUPS"
TS="$(date +%Y%m%d_%H%M%S)"
NAME="krabear_stable_${TS}"
STAGE_DIR="$BACKUP_DIR/.stage_${NAME}"
ARCHIVE_PATH="$BACKUP_DIR/${NAME}.tar.gz"
SHA_PATH="$BACKUP_DIR/${NAME}.sha256"
META_PATH="$BACKUP_DIR/${NAME}.metadata.txt"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

# Основной рабочий контур проекта.
TARGETS=(
  "KrabEar" "native" "scripts" "docs" "README.md"
  "Start Krab Ear.command" "Open Krab Ear Panel.command"
  "Enable Krab Ear Autostart.command" "Disable Krab Ear Autostart.command"
)

# 🔴 ALLOWLIST через git, а не слепой `rsync -a`: рядом с исходниками годами
# лежат .env (реальные секреты), data/ (история диктовок), чужие venv.
#  • только ОТСЛЕЖИВАЕМЫЕ файлы (--cached): неотслеживаемое не попадает, даже если не в
#    .gitignore (там может лежать api-credentials.json) — нужное добавьте через `git add`;
#  • только обычные файлы и симлинки (gitlink подмодуля — каталог: его содержимое подчиняется
#    правилам ДРУГОГО репозитория, tar вытянул бы его .env); битый симлинк сохраняется как симлинк;
#  • имена, похожие на секреты, отбрасываются даже когда отслеживаются.
is_secret_like() {
  case "${1:l}" in
    .env|*/.env|.env.*|*/.env.*|*.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|*credential*|*secret*|*id_rsa*|*id_ed25519*|*id_ecdsa*|*token*.json) return 0 ;;
  esac
  return 1
}

if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  SKIPPED_LIST="$STAGE_DIR/../.skipped_${NAME}.txt"
  : > "$SKIPPED_LIST"
  git -C "$ROOT_DIR" ls-files -z --cached -- "${TARGETS[@]}" \
    | while IFS= read -r -d '' rel; do
        if is_secret_like "$rel"; then
          printf '%s\n' "$rel" >> "$SKIPPED_LIST"
        elif [ -L "$ROOT_DIR/$rel" ] || [ -f "$ROOT_DIR/$rel" ]; then
          printf '%s\0' "$rel"
        fi
      done \
    | { cd "$ROOT_DIR" && tar -cf - --null -T -; } \
    | tar -xf - -C "$STAGE_DIR"
  if [ -s "$SKIPPED_LIST" ]; then
    echo "⚠️  Пропущены файлы, похожие на секреты ($(wc -l < "$SKIPPED_LIST" | tr -d ' ')): $(head -3 "$SKIPPED_LIST" | tr '\n' ' ')..." >&2
  fi
  rm -f "$SKIPPED_LIST"
  UNTRACKED_N="$(git -C "$ROOT_DIR" ls-files --others --exclude-standard -- "${TARGETS[@]}" | wc -l | tr -d ' ')"
  if [ "$UNTRACKED_N" != "0" ]; then
    echo "⚠️  Неотслеживаемых файлов в бэкап НЕ включено: $UNTRACKED_N (git add — чтобы включить)." >&2
  fi
else
  echo "⚠️  Не git-репозиторий: копирую с исключением секретов и данных (список не исчерпывающий)." >&2
  for target in "${TARGETS[@]}"; do
    if [ -e "$ROOT_DIR/$target" ]; then
      rsync -a \
        --exclude='.env' --exclude='.env.*' --exclude='*.pem' --exclude='*.key' \
        --exclude='data/' --exclude='.venv*' --exclude='__pycache__/' --exclude='_STABLE_BACKUPS/' \
        "$ROOT_DIR/$target" "$STAGE_DIR/"
    fi
  done
fi

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
chmod 600 "$ARCHIVE_PATH" "$META_PATH"
shasum -a 256 "$ARCHIVE_PATH" > "$SHA_PATH"
chmod 600 "$SHA_PATH"

rm -rf "$STAGE_DIR"

echo "✅ Резервная копия создана"
echo "Архив:   $ARCHIVE_PATH"
echo "Checksum: $SHA_PATH"
echo "Metadata: $META_PATH"
