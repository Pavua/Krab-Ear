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
#   3. Клонирует репозиторий в ~/KrabEar (git; fallback: curl-tarball без git)
#   4. Создаёт .venv_krab_ear и ставит зависимости (несколько ГБ, потерпите)
#   5. Записывает указатель project_root — агент из /Applications найдёт backend
#   6. Ставит launchd-сервис backend (HF-токен спросит, Enter = пропустить)
#
# Переопределения (env): KRAB_EAR_INSTALL_DIR, KRAB_EAR_REPO_URL, KRAB_EAR_BRANCH.
# Флаг --dry-run: показать план без изменений на диске.

set -u

REPO_URL="${KRAB_EAR_REPO_URL:-https://github.com/Pavua/Krab-Ear.git}"
BRANCH="${KRAB_EAR_BRANCH:-codex/krab-ear-v2}"
INSTALL_DIR="${KRAB_EAR_INSTALL_DIR:-$HOME/KrabEar}"
POINTER_DIR="$HOME/Library/Application Support/KrabEar"
POINTER_FILE="$POINTER_DIR/project_root"
MIN_PY_MINOR=12
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

log()  { printf '\033[0;32m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bootstrap] ⚠\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[bootstrap] ❌ %s\033[0m\n' "$*" >&2; exit 1; }

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

if [ "$DRY_RUN" = "1" ]; then
  log "— dry-run — план без изменений:"
  log "  код:      $REPO_URL (ветка $BRANCH) → $INSTALL_DIR"
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
  log "Скачиваю код ($BRANCH) → $INSTALL_DIR ..."
  if git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" 2>&1; then
    log "git clone: ок"
  else
    # git без Xcode CLT падает (или триггерит диалог установки CLT) — качаем tarball.
    warn "git clone не удался — пробую tarball через curl"
    TARBALL_URL="${REPO_URL%.git}/archive/refs/heads/${BRANCH}.tar.gz"
    TMP_EXTRACT="$(mktemp -d /tmp/krab_ear_bootstrap.XXXXXX)"
    curl -fL "$TARBALL_URL" | tar xz -C "$TMP_EXTRACT" || fail "Не удалось скачать $TARBALL_URL"
    SRC_DIR="$(find "$TMP_EXTRACT" -mindepth 1 -maxdepth 1 -type d | head -1)"
    [ -n "$SRC_DIR" ] || fail "Пустой tarball из $TARBALL_URL"
    mv "$SRC_DIR" "$INSTALL_DIR" || fail "Не удалось переместить код в $INSTALL_DIR"
    rm -rf "$TMP_EXTRACT"
    log "tarball: ок"
  fi
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
