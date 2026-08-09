#!/bin/bash
# build_bundled_runtime.command — сборка самодостаточного Python-рантайма для
# вкладывания в Krab Ear.app (устраняет T2/T3 из docs/audit/2026-08-05-
# onboarding-clean-mac-audit.md: на чистом Mac нет Python >= 3.12, и
# установка сейчас требует Terminal + `pip install` вручную).
#
# Использует python-build-standalone (astral-sh) — relocatable CPython build,
# не требует внешнего Python/Homebrew на машине сборки для ЗАПУСКА (только
# для скачивания). Версия/asset/checksum ЗАПИНОВАНЫ ниже — воспроизводимость
# важнее автообновления (тот же принцип, что GigaAM-пин в install_gigaam_venv.command).
#
# Вывод (--output DIR, по умолчанию build/vendor):
#   DIR/
#     KrabEar/                       — копия backend-исходников
#     .venv_krab_ear/bin/python      — та же структура, что
#                                       BackendSupervisor.swift уже ищет
#                                       (projectRoot/.venv_krab_ear/bin/python)
#
# GigaAM НЕ включён (отдельный venv, пин torch<=2.5.1, конфликт с основным
# torch через pyannote.audio) — остаётся opt-in через install_gigaam_venv.command,
# как сегодня. mlx-whisper покрывает базовый STT без GigaAM.
#
# Usage:
#   scripts/build_bundled_runtime.command [--output DIR] [--skip-download]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
KRABEAR_SRC="$ROOT_DIR/KrabEar"

# ── Пин python-build-standalone (2026-08-05) ────────────────────────────────
PBS_TAG="20260804"
PBS_ASSET="cpython-3.12.13+20260804-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_ASSET}"
PBS_SHA256="b00971ee829e39965e2bda5585666dfdcc74bd1bd97f4b75071b3b05cecf52fd"

OUTPUT_DIR="$ROOT_DIR/build/vendor"
SKIP_DOWNLOAD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) OUTPUT_DIR="$2"; shift 2 ;;
    --skip-download) SKIP_DOWNLOAD=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

log()  { printf '\033[0;32m[bundled-runtime]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[bundled-runtime] ❌ %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(uname -m)" == "arm64" ]] || fail "Сборка только на Apple Silicon (arm64) — mlx-whisper требует arm64"

CACHE_DIR="$ROOT_DIR/build/.runtime_cache"
mkdir -p "$CACHE_DIR"
TARBALL="$CACHE_DIR/$PBS_ASSET"

if [[ "$SKIP_DOWNLOAD" == "1" && -f "$TARBALL" ]]; then
  log "Использую закэшированный $TARBALL"
else
  log "Скачиваю python-build-standalone $PBS_TAG ..."
  curl -fL --retry 3 -o "$TARBALL" "$PBS_URL" || fail "Не удалось скачать $PBS_URL"
fi

ACTUAL_SHA="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
[[ "$ACTUAL_SHA" == "$PBS_SHA256" ]] || fail "checksum mismatch: ожидался $PBS_SHA256, получен $ACTUAL_SHA (испорченная/подменённая загрузка — НЕ используем)"
log "checksum: ок"

BUILD_TMP="$(mktemp -d /tmp/krab_ear_runtime_build.XXXXXX)"
trap 'rm -rf "$BUILD_TMP"' EXIT

log "Распаковываю интерпретатор ..."
tar xzf "$TARBALL" -C "$BUILD_TMP"
BASE_PYTHON="$BUILD_TMP/python/bin/python3"
[[ -x "$BASE_PYTHON" ]] || fail "Не найден интерпретатор после распаковки: $BASE_PYTHON"
log "Интерпретатор: $("$BASE_PYTHON" --version 2>&1)"

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# ── переносим ИЗВЛЕЧЁННОЕ дерево целиком в финальное место, БЕЗ отдельного
# venv-слоя. `python -m venv` создаёт site-packages локально, но stdlib
# (os.py, encodings/...) резолвит через pyvenv.cfg → base install — а base
# install это temp-каталог сборки, которого после trap-очистки уже нет.
# python-build-standalone САМ ПО СЕБЕ уже relocatable (доказано отдельным
# тестом: sys.prefix корректно отражает новое место после переноса) —
# venv-обёртка здесь не нужна и только ломает self-relative резолвинг.
VENV_DIR="$OUTPUT_DIR/.venv_krab_ear"
log "Переношу самодостаточное дерево интерпретатора ..."
mv "$BUILD_TMP/python" "$VENV_DIR"
# BackendSupervisor.swift ищет .venv_krab_ear/bin/python (без суффикса) —
# дистрибутив даёт только python3/python3.12.
ln -sf python3 "$VENV_DIR/bin/python"
VENV_PY="$VENV_DIR/bin/python"

log "Ставлю зависимости (mlx-whisper, pyannote.audio/torch, flask-стек — минуты) ..."
"$VENV_PY" -m pip install --upgrade pip wheel -q
"$VENV_PY" -m pip install -q -r "$KRABEAR_SRC/requirements.txt" \
  || fail "pip install -r requirements.txt не удался"

if [[ -f "$KRABEAR_SRC/requirements-wakeword.txt" ]]; then
  log "Ставлю openWakeWord (живой прод-движок wake word, не опция для бандла) ..."
  "$VENV_PY" -m pip install -q -r "$KRABEAR_SRC/requirements-wakeword.txt" \
    && "$VENV_PY" -c "import openwakeword.utils as u; u.download_models()" \
    || log "⚠ openWakeWord не поставился — детектор будет недоступен (не фатально)"
fi

log "Проверяю импорт критичных пакетов ..."
"$VENV_PY" -c "import mlx_whisper, numpy, sounddevice, flask, requests, pydantic_settings; print('imports ok')" \
  || fail "Критичный импорт не прошёл — бандл нерабочий, останавливаюсь"

log "Копирую KrabEar/ (исходники backend) ..."
mkdir -p "$OUTPUT_DIR/KrabEar"
# 🔴 ALLOWLIST через `git ls-files`, НЕ blacklist-rsync (адверсариальный
# ревью 2026-08-09, HIGH-1). Прежний `rsync --exclude=...` копировал ВСЁ,
# что лежит рядом с исходниками на машине сборки, кроме трёх явных
# исключений — а рядом на dev-машине годами накапливаются .env (реальные
# секреты!), чужой .venv_krab_ear (py3.13, конфликтующий с этим бандлом),
# .pytest_cache, *.log, data/. Живой замер: KrabEar/.env реально оказался
# внутри build/vendor/KrabEar/.env на машине владельца. rsync-blacklist
# ПРИНЦИПИАЛЬНО не может быть безопасным здесь: список того, что может
# появиться в рабочем дереве, не ограничен и не проверяем на review-time.
# git ls-files — единственный источник, который отвечает на вопрос «что
# реально принадлежит проекту», а не «что сейчас лежит на диске».
if ! command -v git &>/dev/null; then
  fail "git не найден — allowlist-копирование требует git ls-files"
fi
# 🔴 БЕЗ trap ... EXIT: он бы ЗАМЕНИЛ (не дополнил) уже установленный на
# строке выше `trap 'rm -rf "$BUILD_TMP"' EXIT` — bash/zsh не складывают
# EXIT-обработчики. Файл живёт линейно и коротко — явный rm сразу после
# использования, без нового trap.
TRACKED_LIST="$(mktemp)"
git -C "$ROOT_DIR" ls-files KrabEar \
  | grep -v '^KrabEar/tests/' \
  > "$TRACKED_LIST"
if [[ ! -s "$TRACKED_LIST" ]]; then
  rm -f "$TRACKED_LIST"
  fail "git ls-files KrabEar пуст — allowlist сломан, копировать нечего"
fi
rsync -a --files-from="$TRACKED_LIST" --exclude='__pycache__' --exclude='*.pyc' \
  "$ROOT_DIR/" "$OUTPUT_DIR/"
rm -f "$TRACKED_LIST"
[[ -f "$OUTPUT_DIR/KrabEar/backend/service.py" ]] \
  || fail "После копирования нет KrabEar/backend/service.py — bundle сломан"

# 🔴 Байткомпилируем ДО подписи (ревью 2026-08-09, MEDIUM-3): без этого
# Python допишет __pycache__/*.pyc внутрь Contents/Resources при первом
# запуске у получателя — запись в подписанный бандл ломает codesign-печать
# (Gatekeeper/Sparkle-проверки могут начать отклонять апдейты). ditto ниже
# по конвейеру (assemble_signed_app.sh) сохраняет mtime .pyc, поэтому
# CPython их не перезапишет повторно при следующем запуске.
log "Байткомпилирую KrabEar/ (иначе Python допишет .pyc в подписанный бандл при первом запуске) ..."
"$VENV_PY" -m compileall -q "$OUTPUT_DIR/KrabEar" || log "⚠ compileall частично не прошёл (не фатально)"

SIZE_MB="$(du -sm "$OUTPUT_DIR" | awk '{print $1}')"
log ""
log "Готово: $OUTPUT_DIR (${SIZE_MB} MB)"
log "Проверка: $VENV_PY $OUTPUT_DIR/KrabEar/main.py --data-dir /tmp/smoke --socket-path /tmp/smoke/backend.sock"
