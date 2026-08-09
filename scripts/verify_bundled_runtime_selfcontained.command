#!/bin/bash
# verify_bundled_runtime_selfcontained.command — гейт на самодостаточность
# bundled Python-рантайма (задача #9 упаковки, 2026-08-09).
#
# Весь смысл scripts/build_bundled_runtime.command — DMG-получатель БЕЗ
# system Python >= 3.12 и БЕЗ Homebrew (T2/T3 из
# docs/audit/2026-08-05-onboarding-clean-mac-audit.md). Тихая зависимость от
# /opt/homebrew или /usr/local, просочившаяся через какой-нибудь будущий pip-
# пакет, обесценивает весь бандл: на dev-машине (где Homebrew есть) всё
# работало бы, а у реального получателя — падало бы с dyld: Library not
# loaded, причём ТОЛЬКО у него, не в CI и не у автора.
#
# Проверяет ДВУМЯ независимыми способами:
#   1) otool -L на КАЖДОМ .so/.dylib — ни один не должен ссылаться на
#      /opt/homebrew или /usr/local (только @rpath/@loader_path/абсолютные
#      пути ВНУТРИ бандла и системные /usr/lib, /System/Library/Frameworks —
#      они гарантированно есть на любом macOS).
#   2) Критичные импорты реально выполняются с PATH, из которого вырезаны
#      /opt/homebrew/* и /usr/local/* — сильнее статического otool-скана:
#      ловит и рантайм-поиск бинарей через PATH (subprocess/shutil.which),
#      не только link-time зависимости.
#
# Usage:
#   scripts/verify_bundled_runtime_selfcontained.command [VENDOR_DIR]
#   (по умолчанию VENDOR_DIR = build/vendor)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="${1:-$ROOT_DIR/build/vendor}"

log()  { printf '\033[0;32m[selfcontained]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[selfcontained] ⚠ %s\033[0m\n' "$*"; }
fail() { printf '\033[0;31m[selfcontained] ❌ %s\033[0m\n' "$*" >&2; exit 1; }

[[ -d "$VENDOR_DIR" ]] || fail "нет каталога: $VENDOR_DIR (сначала build_bundled_runtime.command)"
VENV_PY="$VENDOR_DIR/.venv_krab_ear/bin/python"
[[ -x "$VENV_PY" ]] || fail "нет исполняемого $VENV_PY"
[[ -f "$VENDOR_DIR/KrabEar/backend/service.py" ]] || fail "нет $VENDOR_DIR/KrabEar/backend/service.py"

# ── Проверка 1: otool -L на каждом нативном бинаре ─────────────────────────
log "Сканирую .so/.dylib на зависимости от Homebrew/usr-local ..."
FOUND_LEAKS=0
LEAK_REPORT="$(mktemp)"
trap 'rm -f "$LEAK_REPORT"' EXIT

while IFS= read -r -d '' f; do
  hits="$(otool -L "$f" 2>/dev/null | grep -E '/opt/homebrew|/usr/local' || true)"
  if [[ -n "$hits" ]]; then
    FOUND_LEAKS=1
    {
      echo "  $f"
      echo "$hits" | sed 's/^/    /'
    } >> "$LEAK_REPORT"
  fi
done < <(find "$VENDOR_DIR/.venv_krab_ear" \( -name "*.so" -o -name "*.dylib" \) -print0)

if [[ "$FOUND_LEAKS" -eq 1 ]]; then
  echo "" >&2
  cat "$LEAK_REPORT" >&2
  fail "найдены Homebrew/usr-local зависимости — бандл НЕ самодостаточен (см. список выше)"
fi
SCANNED="$(find "$VENDOR_DIR/.venv_krab_ear" \( -name "*.so" -o -name "*.dylib" \) | wc -l | tr -d ' ')"
log "otool-скан: чисто ($SCANNED файлов, 0 Homebrew/usr-local ссылок)"

# ── Проверка 2: критичные импорты с урезанным PATH ─────────────────────────
# /usr/bin:/bin — минимум, который есть на ЛЮБОМ Mac без единой сторонней
# установки. Если что-то в рантайме ищет бинарь через PATH (subprocess.run,
# shutil.which, ffmpeg-обёртки) и рассчитывает на Homebrew — здесь это
# проявится, даже если otool выше ничего не нашёл (link-time чист, а
# рантайм-поиск — отдельный класс зависимости).
log "Проверяю критичные импорты с PATH=/usr/bin:/bin (симуляция чистого Mac) ..."
CLEAN_PATH_OUTPUT="$(env -i PATH=/usr/bin:/bin HOME="$HOME" "$VENV_PY" -c "
import mlx_whisper, numpy, sounddevice, flask, requests, pydantic_settings
print('imports ok (urezan PATH)')
" 2>&1)" || {
  echo "$CLEAN_PATH_OUTPUT" >&2
  fail "критичный импорт упал с урезанным PATH — рантайм-зависимость от Homebrew/usr-local вне link-time"
}
log "Импорты с урезанным PATH: ок"

log ""
log "Готово: $VENDOR_DIR самодостаточен ($SCANNED нативных файлов проверено)."
