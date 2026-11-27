#!/bin/bash
# bootstrap_backend.command — автоустановка Python-backend Krab Ear на «чистом» Mac.
#
# Зачем: DMG-сборка содержит ТОЛЬКО нативный menu-bar агент (Swift). Backend
# (распознавание речи, история, перевод) живёт в каталоге проекта и ставится
# этим скриптом. Запускается двойным щелчком (Terminal откроется сам) — агент
# подсвечивает этот файл в Finder, когда backend не найден.
#
# Что делает (идемпотентно, без sudo):
#   1. Проверяет Apple Silicon (mlx-whisper требует arm64)
#   2. Ищет Python >= 3.12 (Homebrew/python.org); если нет — говорит, как поставить
#   3. Скачивает ЗАКРЕПЛЁННЫЙ коммит в ~/KrabEar (git; fallback: curl-tarball без git)
#      и проверяет, что получен именно он (KEG2-20: не плавающая ветка)
#   4. Создаёт .venv_krab_ear и ставит зависимости (несколько ГБ, потерпите)
#   5. Записывает указатель project_root — агент из /Applications найдёт backend
#   6. Ставит launchd-сервис backend (HF-токен спросит, Enter = пропустить)
#
# Переопределения (env): KRAB_EAR_INSTALL_DIR, KRAB_EAR_REPO_URL, KRAB_EAR_COMMIT
# (полный 40-символьный SHA). Коммит по умолчанию впечатывает assemble_signed_app.sh
# при сборке .app; при запуске из git-чекаута берётся его HEAD.
# Флаг --dry-run: показать план без изменений на диске.

set -u

REPO_URL="${KRAB_EAR_REPO_URL:-https://github.com/Pavua/Krab-Ear.git}"
# Строку ниже переписывает assemble_signed_app.sh (коммит сборки). Не менять формат.
PINNED_COMMIT=""  # KRAB_EAR_PINNED_COMMIT_STAMP
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${KRAB_EAR_INSTALL_DIR:-$HOME/KrabEar}"
POINTER_DIR="$HOME/Library/Application Support/KrabEar"
POINTER_FILE="$POINTER_DIR/project_root"
MIN_PY_MINOR=12
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log()  { printf '\033[0;32m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap] ⚠\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[bootstrap] ❌ %s\033[0m\n' "$*" >&2; exit 1; }

# KEG2-20: какой коммит ставить. Порядок: KRAB_EAR_COMMIT → штамп сборки → HEAD
# git-чекаута, из которого запущен скрипт. Ничего не нашли — отказ, а не «последнее с ветки».
resolve_expected_commit() {
  local c="${KRAB_EAR_COMMIT:-}"
  [ -n "$c" ] || c="$PINNED_COMMIT"
  if [ -z "$c" ] && [ -e "$SCRIPT_DIR/../.git" ] && command -v git >/dev/null 2>&1; then
    c="$(git -C "$SCRIPT_DIR/.." rev-parse HEAD 2>/dev/null || true)"
  fi
  case "$c" in
    *[!0-9a-f]*|"") return 1 ;;
  esac
  [ "${#c}" -eq 40 ] || return 1
  printf '%s\n' "$c"
}

# git-путь: забираем ровно этот коммит и сверяем HEAD. $1 — пустой каталог-стейджинг.
fetch_pinned_git() {
  local dest="$1" got
  command -v git >/dev/null 2>&1 || return 1
  git init -q "$dest" || return 1
  git -C "$dest" remote add origin "$REPO_URL" || return 1
  git -C "$dest" fetch -q --depth 1 origin "$EXPECTED_COMMIT" || return 1
  git -C "$dest" -c advice.detachedHead=false checkout -q --detach FETCH_HEAD || return 1
  got="$(git -C "$dest" rev-parse HEAD 2>/dev/null)" || return 1
  if [ "$got" != "$EXPECTED_COMMIT" ]; then
    warn "git отдал коммит $got вместо $EXPECTED_COMMIT"
    return 1
  fi
}

# tarball-путь (нет git/CLT): архив ровно этого коммита. git archive пишет SHA в
# глобальный pax-заголовок (comment) — сверяем ДО распаковки; распаковка с фильтром
# data (без абсолютных путей, «..», ссылок наружу). $1 — пустой каталог-стейджинг.
fetch_pinned_tarball() {
  local dest="$1" url="${REPO_URL%.git}/archive/${EXPECTED_COMMIT}.tar.gz"
  local tgz="$dest.tar.gz"
  curl -fL -o "$tgz" "$url" || { rm -f "$tgz"; return 1; }
  "$PY" - "$tgz" "$EXPECTED_COMMIT" "$dest" <<'PYEOF'
import sys
import tarfile

path, want, dest = sys.argv[1:4]
with tarfile.open(path, "r:gz") as tf:
    members = tf.getmembers()
    got = tf.pax_headers.get("comment", "")
    if got != want:
        sys.exit(f"tarball: коммит {got or '<нет>'} вместо {want}")
    tops = {m.name.split("/", 1)[0] for m in members}
    if len(tops) != 1:
        sys.exit("tarball: ожидался один корневой каталог")
    tf.extractall(dest, filter="data")
PYEOF
  local rc=$?
  rm -f "$tgz"
  return $rc
}

# Скачать в стейджинг рядом с INSTALL_DIR, проверить и только потом переименовать.
install_pinned_code() {
  local parent stage src
  parent="$(dirname "$INSTALL_DIR")"
  mkdir -p "$parent" || return 1
  stage="$(mktemp -d "$parent/.krab_ear_bootstrap.XXXXXX")" || return 1
  if fetch_pinned_git "$stage/repo"; then
    src="$stage/repo"
    log "git: коммит $EXPECTED_COMMIT проверен"
  else
    warn "git-путь не удался — пробую tarball коммита через curl"
    rm -rf "$stage/repo"
    mkdir "$stage/x" || { rm -rf "$stage"; return 1; }
    if ! fetch_pinned_tarball "$stage/x"; then
      rm -rf "$stage"
      return 1
    fi
    src="$(find "$stage/x" -mindepth 1 -maxdepth 1 -type d | head -1)"
    log "tarball: коммит $EXPECTED_COMMIT проверен"
  fi
  if [ -z "$src" ] || [ ! -f "$src/KrabEar/backend/service.py" ] || ! mv "$src" "$INSTALL_DIR"; then
    rm -rf "$stage"
    return 1
  fi
  rm -rf "$stage"
}

# ── 1. Apple Silicon ─────────────────────────────────────────────────────────
ARCH="$(uname -m)"
if [ "$ARCH" != "arm64" ]; then
  fail "Krab Ear требует Apple Silicon (arm64): mlx-whisper не работает на $ARCH"
fi
log "Apple Silicon: ок ($ARCH)"

# ── 2. Python >= 3.${MIN_PY_MINOR} ───────────────────────────────────────────
# Системный /usr/bin/python3 (3.9) слишком стар — ищем свежий в стандартных
# местах Homebrew/python.org, от новых версий к старым.
PY=""
for cand in python3.14 python3.13 python3.12 python3; do
  for dir in /opt/homebrew/bin /usr/local/bin /Library/Frameworks/Python.framework/Versions/Current/bin; do
    if [ -x "$dir/$cand" ]; then
      minor="$("$dir/$cand" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
      if [ "$minor" -ge "$MIN_PY_MINOR" ] 2>/dev/null; then
        PY="$dir/$cand"
        break 2
      fi
    fi
  done
  if command -v "$cand" >/dev/null 2>&1; then
    minor="$("$cand" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
    if [ "$minor" -ge "$MIN_PY_MINOR" ] 2>/dev/null; then
      PY="$(command -v "$cand")"
      break
    fi
  fi
done
if [ -z "$PY" ]; then
  warn "Не найден Python >= 3.${MIN_PY_MINOR}."
  warn "Поставьте один из вариантов и запустите скрипт снова:"
  warn "  • Homebrew:  brew install python@3.12   (сам Homebrew: https://brew.sh)"
  warn "  • Установщик с https://www.python.org/downloads/macos/"
  fail "Python >= 3.${MIN_PY_MINOR} обязателен"
fi
log "Python: $PY ($("$PY" --version 2>&1))"

EXPECTED_COMMIT="$(resolve_expected_commit)" \
  || fail "Не задан коммит для установки: укажите KRAB_EAR_COMMIT=<полный 40-символьный SHA>"
log "Коммит: $EXPECTED_COMMIT"

if [ "$DRY_RUN" = "1" ]; then
  log "— dry-run — план без изменений:"
  log "  код:      $REPO_URL (коммит $EXPECTED_COMMIT) → $INSTALL_DIR"
  log "  venv:     $INSTALL_DIR/.venv_krab_ear ($PY)"
  log "  указатель: $POINTER_FILE"
  log "  launchd:  $INSTALL_DIR/scripts/install_backend_launchagent.command"
  exit 0
fi

# ── 3. Код проекта ───────────────────────────────────────────────────────────
if [ -f "$INSTALL_DIR/KrabEar/backend/service.py" ]; then
  log "Код уже установлен: $INSTALL_DIR (clone пропущен)"
else
  [ -e "$INSTALL_DIR" ] && fail "$INSTALL_DIR существует, но это не проект Krab Ear — уберите каталог или задайте KRAB_EAR_INSTALL_DIR"
  log "Скачиваю код (коммит $EXPECTED_COMMIT) → $INSTALL_DIR ..."
  # git без Xcode CLT падает (или триггерит диалог установки CLT) — внутри fallback на tarball.
  install_pinned_code || fail "Не удалось скачать и проверить коммит $EXPECTED_COMMIT из $REPO_URL"
  [ -f "$INSTALL_DIR/KrabEar/backend/service.py" ] || fail "После скачивания нет KrabEar/backend/service.py — что-то пошло не так"
fi

# ── 4. venv + зависимости ────────────────────────────────────────────────────
VENV="$INSTALL_DIR/.venv_krab_ear"
if [ -x "$VENV/bin/python" ]; then
  log "venv уже существует: $VENV"
else
  log "Создаю venv ($PY) ..."
  "$PY" -m venv "$VENV" || fail "python -m venv не удался"
fi
log "Ставлю зависимости (mlx-whisper, torch и т.д. — несколько ГБ, это долго) ..."
"$VENV/bin/pip" install --upgrade pip wheel || fail "pip upgrade не удался"
"$VENV/bin/pip" install -r "$INSTALL_DIR/KrabEar/requirements.txt" || fail "pip install -r requirements.txt не удался"
log "Зависимости: ок"

# Опционально: openWakeWord (детектор слова-пробуждения). Сбой НЕ фатален.
if "$VENV/bin/pip" install -r "$INSTALL_DIR/KrabEar/requirements-wakeword.txt"; then
  # Базовые модели скачиваются один раз с GitHub (dscripka/openWakeWord) —
  # HF_HUB_OFFLINE на них не влияет; без этого первый старт детектора упрётся
  # в отсутствующие файлы моделей.
  "$VENV/bin/python" -c "import openwakeword.utils as u; u.download_models()" \
    || warn "openWakeWord: модели не скачались — детектор попробует при первом запуске"
  log "openWakeWord: ок"
else
  warn "openWakeWord не установился — детектор слова-пробуждения будет недоступен (опционально)"
fi

# ── 5. Указатель project_root для агента ─────────────────────────────────────
# Агент (resolveProjectRoot в main.swift) читает этот файл последним механизмом —
# только он позволяет .app из /Applications найти backend без env-переменных.
mkdir -p "$POINTER_DIR"
printf '%s\n' "$INSTALL_DIR" > "$POINTER_FILE"
log "Указатель записан: $POINTER_FILE → $INSTALL_DIR"

# ── 6. launchd-сервис backend ────────────────────────────────────────────────
log "Ставлю launchd-сервис backend (спросит HF-токен; Enter = пропустить диаризацию) ..."
bash "$INSTALL_DIR/scripts/install_backend_launchagent.command" || fail "Установка launchd-сервиса не удалась"

log ""
log "Готово! Откройте Krab Ear из папки Программы (Applications) заново."
log "Первый запуск предложит скачать STT-модель — нужна сеть."
