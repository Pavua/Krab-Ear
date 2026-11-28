#!/bin/zsh
# ------------------------------------------------------------------
# verify_binaries.command — проверяет что все три binary в sync:
#   1. .app/Contents/MacOS/KrabEarAgent (LaunchServices-managed)
#   2. native/runtime/KrabEarAgent (legacy dev path)
#   3. running KrabEarAgent process
#
# Все три должны иметь одинаковый SHA-256, иначе:
# - User видит UI старого билда / поведение нового билда (или наоборот)
# - timeout / hotkey / IPC изменения не действуют
# - Confused debugging session
#
# Если drift обнаружен — script:
# 0. (ревью G2-Krab-Ear-02b2, KE02B2-05) отказывает, пока backend занят записью/
#    встречей либо его состояние неизвестно (`--force` — осознанный обход);
# 1. Останавливает running KrabEarAgent
# 2. Копирует .build/release/KrabEarAgent в оба места
# 3. Codesign + restart .app
#
# Запуск:
#   bash scripts/verify_binaries.command            # report-only
#   bash scripts/verify_binaries.command --fix      # auto re-sync
#
# Background: после нескольких session'ов session 2026-04-26 нашли что
# `Update Krab Ear Agent.command` иногда rebuild только runtime/ без .app/,
# и наоборот. Активный binary (running PID) можно достать через `lsof -p`.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_BIN="$ROOT_DIR/Krab Ear.app/Contents/MacOS/KrabEarAgent"
RUNTIME_BIN="$ROOT_DIR/native/runtime/KrabEarAgent"
BUILD_BIN="$ROOT_DIR/native/KrabEarAgent/.build/release/KrabEarAgent"
# Переопределяется окружением только для тестов (контрактные тесты подсовывают заглушку).
PGREP_BIN="${PGREP_BIN:-/usr/bin/pgrep}"

# CLI flags
FIX_DRIFT=false
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --fix) FIX_DRIFT=true ;;
    --force) FORCE=true ;;
    --help|-h)
      echo "Usage: $0 [--fix [--force]]"
      echo "  --fix    Auto-resync binaries if drift detected (kills running, re-copies, restarts)"
      echo "  --force  С --fix: не проверять активную запись/встречу (аудио активной записи будет потеряно)"
      exit 0 ;;
    *)
      # KE02B2-16: раньше опечатка (`--fx`) молча давала report-only режим.
      echo "unknown arg: $arg" >&2
      echo "Usage: $0 [--fix [--force]]" >&2
      exit 2 ;;
  esac
done
if [ "$FORCE" = true ] && [ "$FIX_DRIFT" != true ]; then
  echo "--force имеет смысл только вместе с --fix" >&2
  exit 2
fi

# Report-only не требует Keychain. --fix обязан проверить identity до lifecycle.
if [ "$FIX_DRIFT" = true ]; then
  source "$ROOT_DIR/scripts/agent_signing_identity.sh"
  SIGN_ID="$(resolve_agent_signing_identity)" || exit 2
fi

# Hash helper. Каждый ad-hoc `codesign -s -` invocation генерирует new
# signature blob с unique salt — поэтому SHA-256 raw файла всегда
# differs даже для identical Mach-O contents. CDHash тоже зависит от signature
# в этом edge case. Используем file size + mtime + первые 4096 bytes Mach-O
# header как proxy для "same compiled output" — fragile но достаточно для
# detecting **stale builds** (different sizes / dramatically different headers).
hash_or_missing() {
  local file_path="$1"
  if [ ! -f "$file_path" ]; then
    echo "MISSING"
    return
  fi
  local size
  size="$(/usr/bin/stat -f '%z' "$file_path" 2>/dev/null || echo "0")"
  # Header proxy — first 4 KiB после первых байт magic header.
  local hdr
  hdr="$(/bin/dd if="$file_path" bs=4096 count=1 2>/dev/null | /usr/bin/shasum -a 256 | awk '{print $1}')"
  printf "size=%s hdr=%s" "$size" "${hdr:0:16}"
}

# Ревью Opus #2304, P1: размер ПОДПИСАННОГО файла не годится для сверки копий. Подпись bare-бинарника и
# подпись бандла различаются (у бандла есть special slot Info.plist: +32 байта), поэтому корректно
# синхронизированная пара давала ложный DRIFT после любого update_agent/--fix. Сверяем содержимое БЕЗ
# подписи: sha256 временной копии после `codesign --remove-signature` (не Mach-O/не подписан — хэш как есть).
unsigned_digest() {
  local file_path="$1" tmpd digest
  [ -f "$file_path" ] || { echo "MISSING"; return; }
  tmpd="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/verify_binaries.XXXXXX")" || { echo "ERROR"; return; }
  if /bin/cp "$file_path" "$tmpd/bin" 2>/dev/null; then
    /usr/bin/codesign --remove-signature "$tmpd/bin" >/dev/null 2>&1 || true
    digest="$(/usr/bin/shasum -a 256 "$tmpd/bin" | awk '{print $1}')"
  else
    digest="ERROR"
  fi
  /bin/rm -rf "$tmpd"
  echo "$digest"
}

# Процесс жив и не зомби (зомби — уже завершённый, ждёт wait() родителя).
pid_alive() {
  local st
  st="$(/bin/ps -p "$1" -o stat= 2>/dev/null || true)"
  [ -n "$st" ] && [ "${st#Z}" = "$st" ]
}

# launchd-юнит агента (opt-in KeepAlive, install_agent_launchagent.command). Если он загружен, то после
# pkill launchd сам поднимает СТАРЫЙ бинарник ещё до установки нового, а `open` лишь активирует уже
# запущенный процесс — «PID не из OLD_PIDS» засчитал бы его как успех при старом коде (ревью Opus #2304, P2).
AGENT_LABEL="ai.krab.ear.agent"
agent_unit_loaded() {
  "${LAUNCHCTL_BIN:-launchctl}" print "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1
}

# Сколько секунд процессу (BSD ps: etime = [[dd-]hh:]mm:ss); пусто — неизвестно.
pid_elapsed_sec() {
  local el
  el="$("${PS_BIN:-/bin/ps}" -p "$1" -o etime= 2>/dev/null | tr -d ' ')"
  [ -n "$el" ] || return 1
  printf '%s' "$el" | awk -F'[-:]' '{ if (NF==4) print $1*86400+$2*3600+$3*60+$4; else if (NF==3) print $1*3600+$2*60+$3; else print $1*60+$2 }'
}

# Процесс стартовал не раньше метки времени $2 (epoch); неизвестный возраст = нет.
pid_started_since() {
  local el now
  el="$(pid_elapsed_sec "$1")" || return 1
  [ -n "$el" ] || return 1
  now="$(date +%s)"
  [ "$el" -le $(( now - $2 )) ]
}

APP_HASH="$(hash_or_missing "$APP_BIN")"
RUNTIME_HASH="$(hash_or_missing "$RUNTIME_BIN")"
BUILD_HASH="$(hash_or_missing "$BUILD_BIN")"

# Find running PID + its binary path. Use `ps -o comm` (full executable
# path on macOS) — простой и reliable. lsof может показать stale text-segment
# entry если binary был replaced after spawn.
# KE02B2-07: точное имя процесса (`pgrep -x`), а не `-f`: `-f` цеплял любой процесс
# со словом KrabEarAgent в аргументах (редактор, tail, этот же скрипт), и «первый PID»
# мог оказаться не агентом. Если экземпляров несколько — не угадываем, а считаем это
# отдельной проблемой (SingleInstanceGuard должен не допускать дублей).
RUNNING_PIDS="$("$PGREP_BIN" -x "KrabEarAgent" 2>/dev/null || true)"
RUNNING_COUNT=0
RUNNING_PID=""
if [ -n "$RUNNING_PIDS" ]; then
  RUNNING_COUNT="$(printf '%s\n' "$RUNNING_PIDS" | grep -c . || true)"
  RUNNING_PID="$(printf '%s\n' "$RUNNING_PIDS" | head -1)"
fi
RUNNING_BIN=""
RUNNING_HASH="NONE"
if [ -n "$RUNNING_PID" ]; then
  RUNNING_BIN="$(/bin/ps -p "$RUNNING_PID" -o comm= 2>/dev/null | head -1 || true)"
  if [ -n "$RUNNING_BIN" ] && [ -f "$RUNNING_BIN" ]; then
    RUNNING_HASH="$(/usr/bin/shasum -a 256 "$RUNNING_BIN" | awk '{print $1}')"
  fi
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Krab Ear Binary Verification"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
printf "  %-40s %s\n" ".app/Contents/MacOS/KrabEarAgent:" "${APP_HASH:0:16}…"
printf "  %-40s %s\n" "native/runtime/KrabEarAgent:" "${RUNTIME_HASH:0:16}…"
printf "  %-40s %s\n" ".build/release/KrabEarAgent:" "${BUILD_HASH:0:16}…"
if [ -n "$RUNNING_PID" ]; then
  printf "  %-40s %s\n" "running PID $RUNNING_PID ($(basename "${RUNNING_BIN:-?}")):" "${RUNNING_HASH:0:16}…"
else
  printf "  %-40s %s\n" "running:" "(none)"
fi
echo ""

# Drift detection. Каждое `codesign -s - -f` produces unique signature blob
# (timestamp + nonce) → raw hash differs even for identical Mach-O contents.
# Therefore single critical check: **running PID's binary path** matches one
# of deploy paths. Если running запущен из stale path не updated после
# rebuild — будет confusing UX.
DRIFT=false
if [ "$RUNNING_COUNT" -gt 1 ]; then
  echo "  ⚠️  DRIFT: запущено $RUNNING_COUNT экземпляров KrabEarAgent (PID: $(printf '%s' "$RUNNING_PIDS" | tr '\n' ' '))"
  DRIFT=true
fi
# KE02B2-06: deploy-копии сверяем между собой по содержимому БЕЗ подписи (см. unsigned_digest).
if [ "$APP_HASH" = "MISSING" ]; then
  echo "  ⚠️  DRIFT: нет $APP_BIN"
  DRIFT=true
elif [ "$RUNTIME_HASH" != "MISSING" ]; then
  APP_UNSIGNED="$(unsigned_digest "$APP_BIN")"
  RUNTIME_UNSIGNED="$(unsigned_digest "$RUNTIME_BIN")"
  if [ "$APP_UNSIGNED" = "ERROR" ] || [ "$RUNTIME_UNSIGNED" = "ERROR" ] || [ "$APP_UNSIGNED" != "$RUNTIME_UNSIGNED" ]; then
    echo "  ⚠️  DRIFT: .app и native/runtime различаются (содержимое без подписи: ${APP_UNSIGNED:0:12} vs ${RUNTIME_UNSIGNED:0:12})"
    DRIFT=true
  fi
fi
RUNNING_BIN_REAL="$(/usr/bin/python3 -c "import os, sys; p=os.path.realpath(sys.argv[1]) if sys.argv[1] else ''; print(p)" "${RUNNING_BIN:-}" 2>/dev/null)"
APP_BIN_REAL="$(/usr/bin/python3 -c "import os; print(os.path.realpath('$APP_BIN'))" 2>/dev/null)"
RUNTIME_BIN_REAL="$(/usr/bin/python3 -c "import os; print(os.path.realpath('$RUNTIME_BIN'))" 2>/dev/null)"

if [ -n "$RUNNING_PID" ] && [ -n "$RUNNING_BIN_REAL" ]; then
  if [ "$RUNNING_BIN_REAL" != "$APP_BIN_REAL" ] && [ "$RUNNING_BIN_REAL" != "$RUNTIME_BIN_REAL" ]; then
    echo "  ⚠️  DRIFT: running PID запущен из foreign path:"
    echo "       $RUNNING_BIN_REAL"
    DRIFT=true
  fi
  # Compare modification time: running binary should be ≥ as recent as deploy paths.
  # mtime older = process started before recent redeploy.
  RUNNING_MTIME="$(/usr/bin/stat -f '%m' "$RUNNING_BIN_REAL" 2>/dev/null || echo 0)"
  APP_MTIME="$(/usr/bin/stat -f '%m' "$APP_BIN" 2>/dev/null || echo 0)"
  if [ "$APP_MTIME" -gt "$RUNNING_MTIME" ]; then
    echo "  ⚠️  DRIFT: .app/ обновлено после старта running PID — restart для apply changes"
    DRIFT=true
  fi
fi
# Build vs deploy paths — info, не error.
if [ "$BUILD_HASH" != "MISSING" ]; then
  BUILD_MTIME="$(/usr/bin/stat -f '%m' "$BUILD_BIN" 2>/dev/null || echo 0)"
  APP_MTIME="${APP_MTIME:-$(/usr/bin/stat -f '%m' "$APP_BIN" 2>/dev/null || echo 0)}"
  if [ "$BUILD_MTIME" -gt "$APP_MTIME" ]; then
    echo '  ℹ️  .build/release/ свежее .app/ — make sign чтобы deploy'
  fi
fi

if [ "$DRIFT" = false ]; then
  if [ -z "$RUNNING_PID" ]; then
    echo "  ✅ Файлы на диске согласованы; процесс KrabEarAgent НЕ запущен — сверка процесса не выполнялась"
  else
    echo "  ✅ All binaries in sync"
  fi
  exit 0
fi

echo ""
if [ "$FIX_DRIFT" = false ]; then
  echo "  Run with --fix to auto-resync."
  exit 1
fi

# KE02B2-05: busy-гейт ДО любой остановки. Аудио активной записи живёт в памяти
# backend-процесса; остановка агента посреди диктовки/встречи её теряет. Состояние
# неизвестно = отказ (как в remove_agent.command).
if [ -n "$RUNNING_PID" ] && [ "$FORCE" != true ]; then
  if ! "$ROOT_DIR/scripts/safe_backend_restart.command" --check-only >/dev/null 2>&1; then
    echo "  ❌ Backend занят записью/встречей либо его состояние неизвестно — ничего не остановлено." >&2
    echo "     Дождитесь конца записи и повторите, либо запустите с --fix --force." >&2
    exit 1
  fi
fi
if [ "$RUNNING_COUNT" -gt 1 ]; then
  echo "  ❌ Запущено несколько экземпляров — не угадываю, какой останавливать. Разберитесь вручную." >&2
  exit 1
fi

echo "  Auto-fixing drift…"
echo ""

# 1. Kill running. Ждём смерти именно СТАРОГО PID (а не «процессов нет»): под launchd (KeepAlive)
# агент поднимается заново сразу после SIGTERM, и это нормально. Не вышел за 10 с — SIGKILL не шлём
# (мог бы оборвать запись) и ничего не подменяем (ревью Opus #2304, P2).
# launchd-юнит (KeepAlive) выгружаем ДО подмены: иначе launchd поднимает старый бинарник прямо в окне
# cp → mv → codesign (сбой TCC/cdhash). bootout → подмена → подпись → bootstrap; сбой посередине — trap
# возвращает юнит (хвост 27.11, тот же класс в update_agent.command).
AGENT_PLIST="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
UNIT_MANAGED=0
UNIT_BOOTED_OUT=0
if agent_unit_loaded; then
  UNIT_MANAGED=1
  if [ ! -f "$AGENT_PLIST" ]; then
    echo "  ❌ юнит $AGENT_LABEL загружен, но нет $AGENT_PLIST — агент не остановлен, бинарники не тронуты." >&2
    exit 3
  fi
fi
restore_unit_on_exit() {
  if [ "$UNIT_BOOTED_OUT" -eq 1 ]; then
    echo "  → возвращаю launchd-юнит $AGENT_LABEL после сбоя"
    "${LAUNCHCTL_BIN:-launchctl}" bootstrap "gui/$(id -u)" "$AGENT_PLIST" \
      || echo "  ❌ не удалось вернуть юнит. Вручную: launchctl bootstrap gui/$(id -u) \"$AGENT_PLIST\"" >&2
  fi
}
trap restore_unit_on_exit EXIT
# zsh не исполняет EXIT-trap при гибели от сигнала — превращаем сигнал в штатный exit.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
if [ "$UNIT_MANAGED" -eq 1 ]; then
  echo "  → launchd-юнит $AGENT_LABEL загружен: bootout до подмены"
  "${LAUNCHCTL_BIN:-launchctl}" bootout "gui/$(id -u)/$AGENT_LABEL" \
    || { echo "  ❌ launchctl bootout не удался — бинарники не тронуты" >&2; exit 3; }
  UNIT_BOOTED_OUT=1
fi

if [ -n "$RUNNING_PID" ]; then
  echo "  → kill running PID $RUNNING_PID"
  /bin/kill "$RUNNING_PID" 2>/dev/null || true
  old_gone=false
  for _ in {1..50}; do
    if ! pid_alive "$RUNNING_PID"; then old_gone=true; break; fi
    /bin/sleep 0.2
  done
  if [ "$old_gone" != true ]; then
    echo "  ❌ Агент (PID $RUNNING_PID) не завершился по SIGTERM за 10 с — SIGKILL не посылаю, бинарники не тронуты." >&2
    exit 3
  fi
fi

# 2. Source binary picked by **newest mtime** (avoid возврат к старому).
# Раньше preference order был .build > .app > runtime — но если .build/release/
# содержал старый build (например после `git checkout` старой ветки без rebuild),
# script revert'нул .app к старому. Теперь mtime-based — newest wins.
get_mtime() {
  /usr/bin/stat -f '%m' "$1" 2>/dev/null || echo 0
}
APP_MT="$(get_mtime "$APP_BIN")"
RUNTIME_MT="$(get_mtime "$RUNTIME_BIN")"
BUILD_MT="$(get_mtime "$BUILD_BIN")"
SOURCE_BIN=""
SOURCE_MT=0
[ "$APP_MT" -gt "$SOURCE_MT" ] && [ "$APP_HASH" != "MISSING" ] && { SOURCE_BIN="$APP_BIN"; SOURCE_MT="$APP_MT"; }
[ "$RUNTIME_MT" -gt "$SOURCE_MT" ] && [ "$RUNTIME_HASH" != "MISSING" ] && { SOURCE_BIN="$RUNTIME_BIN"; SOURCE_MT="$RUNTIME_MT"; }
[ "$BUILD_MT" -gt "$SOURCE_MT" ] && [ "$BUILD_HASH" != "MISSING" ] && { SOURCE_BIN="$BUILD_BIN"; SOURCE_MT="$BUILD_MT"; }

if [ -z "$SOURCE_BIN" ]; then
  echo "  ❌ No source binary found. Run 'make build' first."
  exit 2
fi
echo "  → using newest source ($(/bin/date -r "$SOURCE_MT" '+%H:%M:%S')): $SOURCE_BIN"

# 3. Copy + sign both.
# Источник может совпадать с destination (самокопирование cp возвращает ошибку).
# KE02B2-13: destination-symlink не перезаписываем (cp -f пошёл бы по ссылке в чужой
# файл), копия идёт во временный файл рядом и встаёт на место атомарным `mv`: обрыв
# посреди копирования не оставляет усечённый бинарник.
install_binary() {
  local src="$1" dest="$2" tmp
  if [ -L "$dest" ]; then
    echo "  ❌ $dest — символическая ссылка; поставка отклонена" >&2
    exit 2
  fi
  tmp="${dest}.new.$$"
  /bin/cp "$src" "$tmp" || { /bin/rm -f "$tmp"; echo "  ❌ cp в $tmp не удался" >&2; exit 2; }
  /bin/chmod +x "$tmp"
  /bin/mv -f "$tmp" "$dest" || { /bin/rm -f "$tmp"; echo "  ❌ mv в $dest не удался" >&2; exit 2; }
}
if [ "$SOURCE_BIN" != "$APP_BIN" ]; then
  install_binary "$SOURCE_BIN" "$APP_BIN"
fi
if [ "$SOURCE_BIN" != "$RUNTIME_BIN" ]; then
  install_binary "$SOURCE_BIN" "$RUNTIME_BIN"
fi
BUNDLE_ID="com.antigravity.krab-ear"
/usr/bin/codesign --force --sign "$SIGN_ID" --timestamp=none --identifier "$BUNDLE_ID" "$RUNTIME_BIN"
/usr/bin/codesign --force --sign "$SIGN_ID" --timestamp=none --identifier "$BUNDLE_ID" "$ROOT_DIR/Krab Ear.app"
/usr/bin/codesign --verify --strict "$RUNTIME_BIN"
/usr/bin/codesign --verify --strict "$ROOT_DIR/Krab Ear.app"
echo "  → synced both paths to source"

# 4. Restart .app.
INSTALL_EPOCH="$(date +%s)"
if [ "$UNIT_MANAGED" -eq 1 ]; then
  # Под launchd `open` дал бы второй экземпляр поверх юнита — возвращаем юнит с новым бинарём.
  UNIT_BOOTED_OUT=0  # дальше trap не повторяет bootstrap
  echo "  → bootstrap launchd-юнита $AGENT_LABEL с новым бинарём"
  "${LAUNCHCTL_BIN:-launchctl}" bootstrap "gui/$(id -u)" "$AGENT_PLIST" || {
    echo "  ❌ launchctl bootstrap не удался — бинарники установлены, агент не запущен." >&2
    echo "     Вручную: launchctl bootstrap gui/$(id -u) \"$AGENT_PLIST\"" >&2
    exit 3
  }
else
  /usr/bin/open "$ROOT_DIR/Krab Ear.app"
fi
# Успех — процесс, которого не было до остановки; под launchd — ещё и стартовавший ПОСЛЕ установки.
NEW_PID=""
for _ in {1..40}; do
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    if [ -n "$RUNNING_PID" ] && printf '%s\n' "$RUNNING_PIDS" | grep -qx "$cand"; then continue; fi
    if [ "$UNIT_MANAGED" -eq 1 ] && ! pid_started_since "$cand" "$INSTALL_EPOCH"; then continue; fi
    NEW_PID="$cand"; break
  done < <("$PGREP_BIN" -x "KrabEarAgent" 2>/dev/null || true)
  [ -n "$NEW_PID" ] && break
  /bin/sleep 0.25
done
if [ -n "$NEW_PID" ]; then
  echo "  ✅ .app restarted (PID $NEW_PID)"
else
  echo "  ⚠️  .app не запустился — проверь permissions"
  exit 3
fi

echo ""
echo "  ✅ Drift fixed"
