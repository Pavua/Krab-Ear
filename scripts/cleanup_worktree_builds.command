#!/bin/zsh
# ------------------------------------------------------------------
# cleanup_worktree_builds.command — Remove stale .build caches from
# old worktrees under .claude/worktrees/*/native/KrabEarAgent/.build/
#
# Usage:
#   ./scripts/cleanup_worktree_builds.command             # dry-run (default)
#   ./scripts/cleanup_worktree_builds.command --dry-run   # dry-run explicit
#   ./scripts/cleanup_worktree_builds.command --apply     # actually delete
#   ./scripts/cleanup_worktree_builds.command --apply --force  # skip confirm
#   ./scripts/cleanup_worktree_builds.command --days 14   # only older than 14 days
#   ./scripts/cleanup_worktree_builds.command --apply --days 60
#
# Safety:
#   - Never deletes outside .claude/worktrees/*/native/KrabEarAgent/.build/
#   - Skips the currently active worktree
#   - Dry-run by default; --apply required to actually remove anything
# ------------------------------------------------------------------

set -uo pipefail

# ── Colors ──────────────────────────────────────────────────────────
RESET=$'\e[0m'
BOLD=$'\e[1m'
RED=$'\e[31m'
GREEN=$'\e[32m'
YELLOW=$'\e[33m'
CYAN=$'\e[36m'
DIM=$'\e[2m'

# ── Parse args ──────────────────────────────────────────────────────
DRY_RUN=true
FORCE=false
DAYS=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)    DRY_RUN=false ;;
    --dry-run)  DRY_RUN=true ;;
    --force)    FORCE=true ;;
    --days)
      shift
      DAYS="${1:?--days requires a number}"
      ;;
    --days=*)   DAYS="${1#--days=}" ;;
    --help|-h)
      grep '^#' "$0" | head -20 | sed 's/^# //' | sed 's/^#//'
      exit 0
      ;;
    *)
      echo "${RED}Unknown flag: $1${RESET}" >&2
      exit 1
      ;;
  esac
  shift
done

# ── Locate repo root and worktrees dir ───────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Find the main worktree root via `git worktree list` (first entry = main repo).
# This works whether the script is run from the main repo or from a worktree.
MAIN_REPO="$(git -C "$ROOT_DIR" worktree list --porcelain 2>/dev/null | awk '/^worktree / {print $2; exit}')"
if [[ -z "$MAIN_REPO" ]]; then
  MAIN_REPO="$ROOT_DIR"
fi
WORKTREES_DIR="$MAIN_REPO/.claude/worktrees"

# Fallback: if script lives *inside* a worktree (path contains .claude/worktrees/*)
# the worktrees dir is two levels above SCRIPT_DIR (worktree/scripts → worktree → worktrees)
if [[ ! -d "$WORKTREES_DIR" ]]; then
  WORKTREES_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi

if [[ ! -d "$WORKTREES_DIR" ]]; then
  echo "${RED}ERROR: worktrees dir not found. Tried:${RESET}" >&2
  echo "  $MAIN_REPO/.claude/worktrees" >&2
  echo "  $(cd "$SCRIPT_DIR/../.." && pwd)" >&2
  exit 1
fi

# ── Detect current active worktree root ──────────────────────────────
CURRENT_WORKTREE="$(git -C "$ROOT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$ROOT_DIR")"
# Normalise (resolve symlinks)
CURRENT_WORKTREE="$(cd "$CURRENT_WORKTREE" && pwd -P 2>/dev/null || echo "$CURRENT_WORKTREE")"

# ── Header ───────────────────────────────────────────────────────────
echo ""
echo "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${RESET}"
if $DRY_RUN; then
  echo "${BOLD}${CYAN}║   Krab Ear — Worktree .build cleanup  [DRY-RUN]          ║${RESET}"
else
  echo "${BOLD}${CYAN}║   Krab Ear — Worktree .build cleanup  [APPLY]            ║${RESET}"
fi
echo "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  ${DIM}Worktrees dir : $WORKTREES_DIR${RESET}"
echo "  ${DIM}Current wt    : $CURRENT_WORKTREE${RESET}"
echo "  ${DIM}Age threshold : ${DAYS} days${RESET}"
echo ""

# Каноническая проверка перед удалением: возвращает РЕАЛЬНЫЙ путь или код 1.
# Строковое сравнение глоба не видит подмену (между `find` и `rm -rf` родитель мог стать
# симлинком наружу); здесь: сам .build не симлинк, реальный путь лежит под реальным каталогом
# worktrees и ТЕКСТУАЛЬНО совпадает с найденным (т.е. в цепочке нет симлинков).
verify_build_dir() {
  local d="$1" real rel
  [[ ! -L "$d" ]] || return 1
  [[ -d "$d" ]] || return 1
  real="$(cd "$d" && pwd -P)" || return 1
  [[ "$real" == "$WORKTREES_REAL"/*/native/KrabEarAgent/.build ]] || return 1
  rel="${d#$WORKTREES_DIR/}"
  # Ровно <worktree>/native/KrabEarAgent/.build: `*` в шаблоне [[ == ]] совпадает и со слешами,
  # поэтому вложенные wt/backup/native/... иначе проходили.
  [[ "$rel" =~ '^[^/]+/native/KrabEarAgent/\.build$' ]] || return 1
  [[ "$real" == "$WORKTREES_REAL/$rel" ]] || return 1
  print -r -- "$real"
}

WORKTREES_REAL="$(cd "$WORKTREES_DIR" && pwd -P)"

# KEG2-18: файловая идентичность каталога (устройство:inode, lstat — симлинк не разыменовывается).
# Абсолютный /usr/bin/stat: GNU stat из Homebrew в PATH понимает `-f` иначе и молча ломает разбор.
build_dir_identity() {
  /usr/bin/stat -f '%d:%i' "$1" 2>/dev/null
}

# KEG2-18: удаление без повторного прохода по абсолютному пути. Подоболочка делает `cd` в
# проверенный родитель — дальше cwd держит его inode, и подмена любого предка симлинком уже
# не перенаправит `rm`. Перед удалением сверяем: родитель тот же, `.build` не симлинк и это
# ТОТ ЖЕ inode, что был найден при сборе кандидатов. Код 3 — отказ, иначе код rm.
remove_build_dir_pinned() {
  local real="$1" want_id="$2"
  local parent="${real%/.build}"
  (
    [[ -n "$want_id" && "$parent" != "$real" ]] || exit 3
    cd -P -- "$parent" 2>/dev/null || exit 3
    [[ "$(pwd -P)" == "$parent" ]] || exit 3
    [[ ! -L .build && -d .build ]] || exit 3
    [[ "$(build_dir_identity .build)" == "$want_id" ]] || exit 3
    /bin/rm -rf -- ./.build
  )
}

# ── Gather candidates ────────────────────────────────────────────────
# Cutoff epoch seconds
NOW_EPOCH=$(date +%s)
CUTOFF_EPOCH=$(( NOW_EPOCH - DAYS * 86400 ))

# Arrays
typeset -a BUILD_DIRS
typeset -a BUILD_IDS
typeset -a BUILD_SIZES
typeset -a BUILD_MTIMES
typeset -a BUILD_AGES
typeset -a BUILD_SKIP_REASONS

TOTAL_KB=0
SKIPPED_COUNT=0
CANDIDATE_COUNT=0

while IFS= read -r build_dir; do
  # ── Safety check: path must be strictly inside WORKTREES_DIR/*/native/KrabEarAgent/.build ──
  if [[ "$build_dir" != "$WORKTREES_DIR"/*/native/KrabEarAgent/.build ]]; then
    continue
  fi

  # Resolve worktree root (3 levels up from .build)
  wt_root="$(cd "$build_dir/../../.." && pwd -P 2>/dev/null || echo "")"
  wt_name="$(basename "$wt_root")"

  # ── Skip current active worktree ──
  if [[ "$wt_root" == "$CURRENT_WORKTREE" ]]; then
    echo "  ${YELLOW}SKIP${RESET} ${DIM}$wt_name${RESET}  ${DIM}← current active worktree${RESET}"
    SKIPPED_COUNT=$(( SKIPPED_COUNT + 1 ))
    continue
  fi

  # ── Check age via mtime of the .build directory itself ──
  mtime=$(/usr/bin/stat -f "%m" "$build_dir" 2>/dev/null || echo 0)
  age_days=$(( (NOW_EPOCH - mtime) / 86400 ))

  if (( mtime > CUTOFF_EPOCH )); then
    echo "  ${YELLOW}SKIP${RESET} ${DIM}$wt_name${RESET}  ${DIM}← ${age_days}d old (< ${DAYS}d threshold)${RESET}"
    SKIPPED_COUNT=$(( SKIPPED_COUNT + 1 ))
    continue
  fi

  # ── Identity for the final re-check (KEG2-18); unknown identity = never delete ──
  build_id="$(build_dir_identity "$build_dir")"
  if [[ -z "$build_id" || -L "$build_dir" ]]; then
    echo "  ${YELLOW}SKIP${RESET} ${DIM}$wt_name${RESET}  ${DIM}← cannot verify identity${RESET}"
    SKIPPED_COUNT=$(( SKIPPED_COUNT + 1 ))
    continue
  fi

  # ── Compute size ──
  size_kb=$(du -sk "$build_dir" 2>/dev/null | cut -f1)
  size_mb=$(( size_kb / 1024 ))

  BUILD_DIRS+=("$build_dir")
  BUILD_IDS+=("$build_id")
  BUILD_SIZES+=("$size_kb")
  BUILD_MTIMES+=("$mtime")
  BUILD_AGES+=("$age_days")
  TOTAL_KB=$(( TOTAL_KB + size_kb ))
  CANDIDATE_COUNT=$(( CANDIDATE_COUNT + 1 ))

done < <(find "$WORKTREES_DIR" -maxdepth 4 -name ".build" -type d 2>/dev/null | sort)

echo ""

# ── Print candidate table ─────────────────────────────────────────────
if (( CANDIDATE_COUNT == 0 )); then
  echo "${GREEN}Nothing to clean up — no .build dirs older than ${DAYS} days (outside current wt).${RESET}"
  echo ""
  exit 0
fi

echo "${BOLD}Candidates to remove (${CANDIDATE_COUNT} dirs):${RESET}"
echo ""

for (( idx=1; idx<=${#BUILD_DIRS}; idx++ )); do
  build_dir="${BUILD_DIRS[$idx]}"
  size_kb="${BUILD_SIZES[$idx]:-0}"
  age_days="${BUILD_AGES[$idx]:-0}"
  size_mb=$(( ${size_kb:-0} / 1024 ))
  wt_name="$(basename "$(dirname "$(dirname "$(dirname "$build_dir")")")")"

  # Format size
  if (( size_mb >= 1024 )); then
    size_human="$(awk "BEGIN{printf \"%.1f GB\", ${size_mb}/1024}")"
  elif (( size_mb >= 1 )); then
    size_human="${size_mb} MB"
  else
    size_human="${size_kb} KB"
  fi

  echo "  ${RED}✗${RESET} ${BOLD}$wt_name${RESET}"
  echo "      path : ${DIM}$build_dir${RESET}"
  echo "      size : ${YELLOW}$size_human${RESET}  age: ${age_days}d"
  echo ""
done

TOTAL_MB=$(( TOTAL_KB / 1024 ))
if (( TOTAL_MB >= 1024 )); then
  TOTAL_HUMAN="$(awk "BEGIN{printf \"%.1f GB\", $TOTAL_MB/1024}")"
else
  TOTAL_HUMAN="${TOTAL_MB} MB"
fi

echo "${BOLD}${CYAN}─────────────────────────────────────────────────────────${RESET}"
echo "${BOLD}  Total reclaimable : ${YELLOW}$TOTAL_HUMAN${RESET}  (${CANDIDATE_COUNT} dirs, ${SKIPPED_COUNT} skipped)"
echo "${BOLD}${CYAN}─────────────────────────────────────────────────────────${RESET}"
echo ""

# ── Dry-run exit ──────────────────────────────────────────────────────
if $DRY_RUN; then
  echo "${DIM}This was a dry-run. To actually delete, run:${RESET}"
  echo "  ${BOLD}./scripts/cleanup_worktree_builds.command --apply${RESET}"
  echo ""
  exit 0
fi

# ── Confirm before apply ──────────────────────────────────────────────
if ! $FORCE; then
  printf "${BOLD}${RED}Delete %d .build directories (%s)? [y/N] ${RESET}" "$CANDIDATE_COUNT" "$TOTAL_HUMAN"
  read -r confirm
  if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "${YELLOW}Aborted.${RESET}"
    exit 0
  fi
fi

# ── Apply ─────────────────────────────────────────────────────────────
echo ""
echo "${BOLD}Deleting...${RESET}"
echo ""
DELETED_COUNT=0
DELETED_KB=0
FAILED_COUNT=0

for (( idx=1; idx<=${#BUILD_DIRS}; idx++ )); do
  build_dir="${BUILD_DIRS[$idx]}"
  size_kb="${BUILD_SIZES[$idx]:-0}"
  wt_name="$(basename "$(dirname "$(dirname "$(dirname "$build_dir")")")")"

  # Final safety re-check
  if [[ "$build_dir" != "$WORKTREES_DIR"/*/native/KrabEarAgent/.build ]]; then
    echo "  ${RED}REFUSED${RESET} (unsafe path): $build_dir" >&2
    FAILED_COUNT=$(( FAILED_COUNT + 1 ))
    continue
  fi

  real_build_dir="$(verify_build_dir "$build_dir")" || {
    echo "  ${RED}REFUSED${RESET} (путь изменился/не прошёл каноническую проверку): $build_dir" >&2
    FAILED_COUNT=$(( FAILED_COUNT + 1 ))
    continue
  }

  remove_build_dir_pinned "$real_build_dir" "${BUILD_IDS[$idx]}" 2>/dev/null
  rm_rc=$?
  if (( rm_rc == 3 )); then
    echo "  ${RED}REFUSED${RESET} (каталог подменён после проверки): $build_dir" >&2
    FAILED_COUNT=$(( FAILED_COUNT + 1 ))
  elif (( rm_rc == 0 )); then
    DELETED_COUNT=$(( DELETED_COUNT + 1 ))
    DELETED_KB=$(( DELETED_KB + ${size_kb:-0} ))
    DELETED_MB=$(( ${size_kb:-0} / 1024 ))
    echo "  ${GREEN}✓${RESET} Deleted $wt_name/.build  ${DIM}(${DELETED_MB} MB)${RESET}"
  else
    echo "  ${RED}✗ FAILED${RESET} $wt_name/.build"
    FAILED_COUNT=$(( FAILED_COUNT + 1 ))
  fi
done

DELETED_MB=$(( DELETED_KB / 1024 ))
if (( DELETED_MB >= 1024 )); then
  DELETED_HUMAN="$(awk "BEGIN{printf \"%.1f GB\", $DELETED_MB/1024}")"
else
  DELETED_HUMAN="${DELETED_MB} MB"
fi

echo ""
echo "${BOLD}${GREEN}─────────────────────────────────────────────────────────${RESET}"
echo "${BOLD}${GREEN}  Done: deleted ${DELETED_COUNT} dirs, freed ${DELETED_HUMAN}${RESET}"
if (( FAILED_COUNT > 0 )); then
  echo "${RED}  Failures: ${FAILED_COUNT}${RESET}"
fi
echo "${BOLD}${GREEN}─────────────────────────────────────────────────────────${RESET}"
echo ""
# Отказ или сбой удаления — не «успех»: вызывающий видит ненулевой код.
(( FAILED_COUNT == 0 )) || exit 1
