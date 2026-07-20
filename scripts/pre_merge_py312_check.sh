#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# pre_merge_py312_check.sh — локально воспроизводит окружение ubuntu krab-ear-ci,
# чтобы проверить PR до медленного удалённого CI и не создавать red-tip цикл.
#
# ЗАЧЕМ: dev-venv (.venv_krab_ear) работает на Python 3.14 и содержит
# mlx-whisper / mlx.core (для macOS есть wheels). Ubuntu runner использует
# Python 3.12 без mlx wheels. Тест, который полагается на успешный импорт MLX,
# локально даёт ложный GREEN, но падает в ubuntu (см. mlx-masking в CLAUDE.md).
#
# Harness создаёт или переиспользует Python 3.12 venv в $HARNESS_VENV с полными
# backend-зависимостями, кроме mlx / mlx-whisper, и запускает файлы по одному.
# Между файлами он пассивно сравнивает worker-процессы: чужие процессы не трогает,
# а новые утечки показывает как PID+command и учитывает как провал файла.
#
# ИСПОЛЬЗОВАНИЕ:
#   scripts/pre_merge_py312_check.sh [TEST_FILE ...]
#     - с аргументами: запускает именно эти файлы (относительные или абсолютные);
#     - без аргументов: находит изменённые тесты относительно
#       origin/codex/krab-ear-v2 через git diff --name-only.
#
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ:
#   HARNESS_VENV   (по умолчанию /tmp/py312)     путь к venv;
#   PY312          (по умолчанию python3.12)     интерпретатор для venv;
#   REBUILD=1      принудительно пересоздаёт venv.
#
# ВЫХОД: 0, если все выбранные файлы прошли; иначе ненулевой код.
# ---------------------------------------------------------------------------
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HARNESS_VENV="${HARNESS_VENV:-/tmp/py312}"
PY312="${PY312:-python3.12}"
BASE_REF="${BASE_REF:-origin/codex/krab-ear-v2}"

log() { printf '\033[1;36m[harness]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[harness]\033[0m %s\n' "$*" >&2; }

snapshot_matching_workers() {
  local output_path="$1"
  local all_processes_path="${output_path}.all"
  local snapshot_status

  # Сначала сохраняем ps целиком: тогда команда фильтра не попадёт в собственный
  # снимок только потому, что её argv содержит искомые worker-маркеры.
  if ! LC_ALL=C ps -axo pid=,command= > "$all_processes_path"; then
    rm -f "$all_processes_path"
    return 1
  fi
  LC_ALL=C awk '
    index($0, "gigaam_worker.py") ||
    index($0, "mlx_subprocess") ||
    index($0, "import sys;ex") { print }
  ' "$all_processes_path" | LC_ALL=C sort > "$output_path"
  snapshot_status=$?
  rm -f "$all_processes_path"
  return "$snapshot_status"
}

# --- 1. ensure interpreter ------------------------------------------------
if ! command -v "$PY312" >/dev/null 2>&1; then
  for cand in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    [ -x "$cand" ] && PY312="$cand" && break
  done
fi
command -v "$PY312" >/dev/null 2>&1 || { err "python3.12 not found (set PY312=...)"; exit 2; }

# --- 2. build / reuse venv ------------------------------------------------
needs_build=0
if [ "${REBUILD:-0}" = "1" ] || [ ! -x "$HARNESS_VENV/bin/python" ]; then
  needs_build=1
else
  # reuse only if backend imports AND mlx is genuinely absent (ubuntu parity)
  if ! PYTHONPATH="$REPO_ROOT/KrabEar" "$HARNESS_VENV/bin/python" -c "import backend.service" >/dev/null 2>&1; then
    log "existing venv cannot import backend → rebuild"
    needs_build=1
  elif "$HARNESS_VENV/bin/python" -c "import mlx_whisper" >/dev/null 2>&1 || \
       "$HARNESS_VENV/bin/python" -c "import mlx.core"     >/dev/null 2>&1; then
    log "existing venv still has mlx → purging to match ubuntu"
    "$HARNESS_VENV/bin/pip" uninstall -y mlx mlx-whisper mlx-lm >/dev/null 2>&1 || true
  fi
fi

if [ "$needs_build" = "1" ]; then
  log "building ubuntu-parity venv at $HARNESS_VENV ($($PY312 --version 2>&1)) ..."
  rm -rf "$HARNESS_VENV"
  "$PY312" -m venv "$HARNESS_VENV" || { err "venv create failed"; exit 2; }
  "$HARNESS_VENV/bin/pip" install -q --upgrade pip wheel >/dev/null 2>&1 || true
  log "installing KrabEar/requirements.txt (this is slow on first build) ..."
  "$HARNESS_VENV/bin/pip" install -q -r KrabEar/requirements.txt
  "$HARNESS_VENV/bin/pip" install -q pytest >/dev/null 2>&1 || true
  # CRITICAL: strip mlx so the env matches ubuntu (no Linux wheels exist there).
  log "purging mlx / mlx-whisper / mlx-lm to reproduce ubuntu (no Linux wheels)"
  "$HARNESS_VENV/bin/pip" uninstall -y mlx mlx-whisper mlx-lm >/dev/null 2>&1 || true
fi

# sanity: mlx MUST be absent, backend MUST import
if "$HARNESS_VENV/bin/python" -c "import mlx_whisper" >/dev/null 2>&1; then
  err "FATAL: mlx_whisper still importable in harness venv — not ubuntu-parity"; exit 2
fi
PYTHONPATH="$REPO_ROOT/KrabEar" "$HARNESS_VENV/bin/python" -c "import backend.service" >/dev/null 2>&1 \
  || { err "FATAL: backend.service import failed in harness venv"; exit 2; }
log "venv ready: $($HARNESS_VENV/bin/python --version 2>&1), mlx ABSENT (ubuntu parity), backend imports OK"

# --- 3. select test files -------------------------------------------------
declare -a TESTS=()
if [ "$#" -gt 0 ]; then
  TESTS=("$@")
else
  log "no args → auto-detecting changed test files vs $BASE_REF"
  while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] && TESTS+=("$f")
  done < <(git diff --name-only "$BASE_REF"...HEAD 2>/dev/null | grep -E 'KrabEar/tests/test_.*\.py$' ; \
           git diff --name-only 2>/dev/null | grep -E 'KrabEar/tests/test_.*\.py$')
  # Убираем дубликаты без mapfile/readarray, которых нет в системном Bash 3.2.
  if [ "${#TESTS[@]}" -gt 0 ]; then
    unique_tests=()
    while IFS= read -r f; do
      [ -n "$f" ] && unique_tests+=("$f")
    done < <(printf '%s\n' "${TESTS[@]}" | sort -u)
    TESTS=("${unique_tests[@]}")
  fi
fi

if [ "${#TESTS[@]}" -eq 0 ]; then
  err "no test files selected (pass paths as args, or have changed test files vs $BASE_REF)"
  exit 3
fi

log "running ${#TESTS[@]} test file(s) memory-safe (one at a time, no xdist):"
printf '  - %s\n' "${TESTS[@]}"

# --- 4. запускаем каждый файл и пассивно проверяем утечки ----------------
WORKER_SNAPSHOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/krab-ear-py312-workers.XXXXXX")" \
  || { err "не удалось создать каталог снимков worker-процессов"; exit 2; }

cleanup_worker_snapshots() {
  [ -d "${WORKER_SNAPSHOT_DIR:-}" ] || return 0
  rm -f "$WORKER_SNAPSHOT_DIR"/* 2>/dev/null || true
  rmdir "$WORKER_SNAPSHOT_DIR" 2>/dev/null || true
}
trap cleanup_worker_snapshots EXIT

fails=()
test_index=0
for t in "${TESTS[@]}"; do
  test_index=$((test_index + 1))
  before_workers="$WORKER_SNAPSHOT_DIR/${test_index}.before"
  after_workers="$WORKER_SNAPSHOT_DIR/${test_index}.after"
  new_workers="$WORKER_SNAPSHOT_DIR/${test_index}.new"
  file_failed=0

  if ! snapshot_matching_workers "$before_workers"; then
    err "не удалось снять worker-процессы перед $t"
    exit 2
  fi

  log "→ $t"
  if PYTHONPATH="$REPO_ROOT/KrabEar" "$HARNESS_VENV/bin/python" -m pytest "$t" -p no:xdist -q; then
    :
  else
    file_failed=1
  fi

  if ! snapshot_matching_workers "$after_workers"; then
    err "не удалось снять worker-процессы после $t"
    file_failed=1
  elif ! LC_ALL=C comm -13 "$before_workers" "$after_workers" > "$new_workers"; then
    err "не удалось сравнить снимки worker-процессов после $t"
    file_failed=1
  elif [ -s "$new_workers" ]; then
    err "после $t появились новые worker-процессы; сигналы им НЕ отправлялись:"
    while IFS= read -r worker; do
      printf '   НОВЫЙ WORKER: %s\n' "$worker" >&2
    done < "$new_workers"
    file_failed=1
  fi

  if [ "$file_failed" -ne 0 ]; then
    fails+=("$t")
  fi
done

echo
if [ "${#fails[@]}" -eq 0 ]; then
  log "=== ALL GREEN (ubuntu-parity py3.12, mlx absent) | FAIL: none ==="
  exit 0
else
  err "=== RED on ${#fails[@]} file(s) (would fail ubuntu CI) ==="
  printf '   FAIL: %s\n' "${fails[@]}" >&2
  exit 1
fi
