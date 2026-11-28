# shellcheck shell=bash
# ------------------------------------------------------------------
# Общий lifecycle launchd-юнита агента для update_agent.command и
# verify_binaries.command --fix (ревью #2324). Подключается через `source`,
# совместим с zsh и Bash 3.2 (без mapfile/declare -A/глобов).
#
# Инвариант: launchd никогда не запускает бинарь без проверенной подписи
# (`codesign --verify --strict`). Не прошла проверка — откат из резервной
# копии, подпись, повторная проверка; не вышло и это — юнит остаётся
# выгруженным, печатается инструкция для ручного восстановления.
#
# Ожидает переменные: AGENT_LABEL, AGENT_PLIST, APP_BUNDLE, RUNTIME_BIN,
# BUNDLE_ID, SIGN_ID. Флаг UNIT_RECOVERY=1 означает «юнит мог быть выгружен
# нами»: выход скрипта в этом состоянии сверяет РЕАЛЬНОЕ состояние launchd.
# ------------------------------------------------------------------

UNIT_RECOVERY=0
MAINT_LOCK_OWNED=0
MAINT_LOCK_DIR=""
AGENT_BACKUP_DIR=""

# Состояние юнита: печатает loaded|absent. `launchctl print` даёт 113, когда
# сервиса нет; любой другой ненулевой код (ошибка домена, сбой launchctl) —
# неизвестное состояние, вызывающий обязан отказаться от изменений.
agent_unit_state() {
  local rc=0
  "${LAUNCHCTL_BIN:-launchctl}" print "gui/$(id -u)/$AGENT_LABEL" >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) echo loaded ;;
    113) echo absent ;;
    *) echo "launchctl print gui/$(id -u)/$AGENT_LABEL вернул код $rc" >&2; return 1 ;;
  esac
}

# Эксклюзивная блокировка обслуживания агента (mkdir атомарен). Одна на
# пользователя: launchd-юнит общий для всех чекаутов/worktree.
maint_lock_acquire() {
  local holder stale
  MAINT_LOCK_DIR="${KRAB_EAR_MAINT_LOCK:-$HOME/Library/Caches/KrabEar/agent_maintenance.lock}"
  mkdir -p "$(dirname "$MAINT_LOCK_DIR")" 2>/dev/null || true
  if mkdir "$MAINT_LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$MAINT_LOCK_DIR/pid"
    MAINT_LOCK_OWNED=1
    return 0
  fi
  holder="$(cat "$MAINT_LOCK_DIR/pid" 2>/dev/null || true)"
  case "$holder" in ''|*[!0-9]*) holder="" ;; esac
  if [ -z "$holder" ] || kill -0 "$holder" 2>/dev/null; then
    echo "Обслуживание агента уже идёт (блокировка $MAINT_LOCK_DIR, PID ${holder:-неизвестен})." >&2
    echo "Дождитесь его конца. Если процесса точно нет — удалите каталог блокировки вручную." >&2
    return 1
  fi
  # Владелец мёртв: переименовываем атомарно и проверяем, что увели именно его
  # блокировку, а не свежую чужую (два одновременных «стейл»-захвата).
  stale="$MAINT_LOCK_DIR.stale.$$"
  mv "$MAINT_LOCK_DIR" "$stale" 2>/dev/null || return 1
  if [ "$(cat "$stale/pid" 2>/dev/null || true)" != "$holder" ]; then
    if [ ! -e "$MAINT_LOCK_DIR" ]; then mv "$stale" "$MAINT_LOCK_DIR" 2>/dev/null || true; fi
    echo "Блокировка $MAINT_LOCK_DIR перехвачена другим процессом — отказ." >&2
    return 1
  fi
  rm -rf "$stale"
  if mkdir "$MAINT_LOCK_DIR" 2>/dev/null; then
    echo "$$" > "$MAINT_LOCK_DIR/pid"
    MAINT_LOCK_OWNED=1
    echo "Снята брошенная блокировка обслуживания (PID $holder завершён)."
    return 0
  fi
  echo "Обслуживание агента уже идёт (блокировка $MAINT_LOCK_DIR)." >&2
  return 1
}

# Освобождает блокировку только если она наша.
maint_lock_release() {
  if [ "$MAINT_LOCK_OWNED" = 1 ] && [ "$(cat "$MAINT_LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -rf "$MAINT_LOCK_DIR"
  fi
  MAINT_LOCK_OWNED=0
  return 0
}

# Резервная копия прежних бинарей ВНЕ бандла: лишний файл в Contents ломает
# `codesign --verify --strict` всего .app.
agent_backup_binaries() {
  AGENT_BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/krab-ear-agent-backup.XXXXXX")" || return 1
  if [ -f "$APP_BUNDLE/Contents/MacOS/KrabEarAgent" ]; then
    cp "$APP_BUNDLE/Contents/MacOS/KrabEarAgent" "$AGENT_BACKUP_DIR/app" || return 1
  fi
  if [ -f "$RUNTIME_BIN" ]; then
    cp "$RUNTIME_BIN" "$AGENT_BACKUP_DIR/runtime" || return 1
  fi
  return 0
}

agent_backup_cleanup() {
  if [ -n "$AGENT_BACKUP_DIR" ] && [ -d "$AGENT_BACKUP_DIR" ]; then rm -rf "$AGENT_BACKUP_DIR"; fi
  return 0
}

# Возвращает один файл из копии атомарно (tmp рядом + mv); ссылку не трогает.
_agent_restore_one() {
  local src="$1" dest="$2"
  [ -f "$src" ] || return 0
  if [ -L "$dest" ]; then echo "$dest — символическая ссылка, откат отклонён" >&2; return 1; fi
  cp "$src" "$dest.restore.$$" || { rm -f "$dest.restore.$$"; return 1; }
  chmod +x "$dest.restore.$$"
  mv -f "$dest.restore.$$" "$dest" || { rm -f "$dest.restore.$$"; return 1; }
}

agent_restore_binaries() {
  [ -n "$AGENT_BACKUP_DIR" ] && [ -f "$AGENT_BACKUP_DIR/app" ] || return 1
  _agent_restore_one "$AGENT_BACKUP_DIR/app" "$APP_BUNDLE/Contents/MacOS/KrabEarAgent" || return 1
  _agent_restore_one "$AGENT_BACKUP_DIR/runtime" "$RUNTIME_BIN"
}

agent_sign_and_verify() {
  codesign --force --sign "$SIGN_ID" --timestamp=none --identifier "$BUNDLE_ID" "$RUNTIME_BIN" \
    && codesign --force --sign "$SIGN_ID" --timestamp=none --identifier "$BUNDLE_ID" "$APP_BUNDLE" \
    && codesign --verify --strict "$RUNTIME_BIN" \
    && codesign --verify --strict "$APP_BUNDLE"
}

agent_app_verified() {
  codesign --verify --strict "$APP_BUNDLE" >/dev/null 2>&1
}

agent_unit_manual_hint() {
  {
    echo "❌ launchd-юнит $AGENT_LABEL ОСТАВЛЕН ВЫГРУЖЕННЫМ ($1) — агент не запущен."
    echo "   launchd не получает бинарь без проверенной подписи. Проверьте: codesign --verify --strict \"$APP_BUNDLE\""
    echo "   Затем вручную: launchctl bootstrap gui/$(id -u) \"$AGENT_PLIST\""
  } >&2
}

# Загрузка юнита идемпотентна: «уже загружен» = успех. Успех подтверждается
# только `launchctl print`; одна повторная попытка, дальше — отказ.
agent_unit_bootstrap() {
  local attempt st
  for attempt in 1 2; do
    "${LAUNCHCTL_BIN:-launchctl}" bootstrap "gui/$(id -u)" "$AGENT_PLIST" || true
    st="$(agent_unit_state 2>/dev/null)" || st=unknown
    if [ "$st" = loaded ]; then return 0; fi
    if [ "$attempt" = 1 ]; then sleep 1; fi
  done
  return 1
}

# Восстановление на выходе: сверяет фактическое состояние, загружает юнит
# только с проверенной подписью .app (иначе — откат из копии + подпись).
agent_unit_recover() {
  local st
  [ "$UNIT_RECOVERY" = 1 ] || return 0
  UNIT_RECOVERY=0
  st="$(agent_unit_state 2>/dev/null)" || st=unknown
  if [ "$st" = loaded ]; then return 0; fi
  echo "Возвращаю launchd-юнит $AGENT_LABEL после сбоя обновления."
  if ! agent_app_verified; then
    echo "Подпись .app не проходит проверку — откатываю прежний бинарь из резервной копии."
    if ! agent_restore_binaries || ! agent_sign_and_verify; then
      agent_unit_manual_hint "откат не прошёл проверку подписи"
      return 1
    fi
  fi
  if agent_unit_bootstrap; then
    echo "launchd-юнит $AGENT_LABEL возвращён."
    return 0
  fi
  agent_unit_manual_hint "launchctl bootstrap не удался"
  return 1
}
