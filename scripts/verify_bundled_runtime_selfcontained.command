#!/bin/bash
# verify_bundled_runtime_selfcontained.command — гейт на самодостаточность
# bundled Python-рантайма (задача #9 упаковки, 2026-08-09; ужесточён по
# итогам адверсариального ревью того же дня — см. правки ниже).
#
# Весь смысл scripts/build_bundled_runtime.command — DMG-получатель БЕЗ
# system Python >= 3.12 и БЕЗ Homebrew (T2/T3 из
# docs/audit/2026-08-05-onboarding-clean-mac-audit.md). Тихая зависимость от
# /opt/homebrew или /usr/local, просочившаяся через какой-нибудь будущий pip-
# пакет, обесценивает весь бандл: на dev-машине (где Homebrew есть) всё
# работало бы, а у реального получателя — падало бы с dyld: Library not
# loaded, причём ТОЛЬКО у него, не в CI и не у автора.
#
# Проверяет ТРЕМЯ независимыми способами:
#   1) otool -L на КАЖДОМ исполняемом Mach-O внутри VENDOR_DIR (по
#      СОДЕРЖИМОМУ через `file`, не по расширению — интерпретатор
#      .venv_krab_ear/bin/python3.12 и torch/bin/protoc не имеют .so/.dylib
#      расширения, но линкуются как обычный Mach-O) — ни один не должен
#      ссылаться на /opt/homebrew или /usr/local.
#   2) Явный запрет на файлы, которым в бандле в принципе не место —
#      .env/.venv*/логи/data/.pytest_cache внутри KrabEar/ (адверсариальный
#      ревью 2026-08-09, HIGH-1: rsync-blacklist в build_bundled_runtime.command
#      реально утащил .env владельца в собранный build/vendor до перехода на
#      git-ls-files allowlist — эта проверка ловит регрессию НА ЭТОТ класс
#      бага, а не полагается только на то, что копирующая сторона больше не
#      ошибётся).
#   3) Критичные импорты реально выполняются с PATH, из которого вырезаны
#      /opt/homebrew/* и /usr/local/* (симуляция чистого Mac), плюс `-s`
#      (без user-site) — сильнее статического otool-скана: ловит и
#      рантайм-поиск бинарей через PATH (subprocess/shutil.which), и
#      случайный успех импорта через ~/.local/lib site-packages, который на
#      машине автора есть, а у получателя дал бы ImportError.
#
# Usage:
#   scripts/verify_bundled_runtime_selfcontained.command [VENDOR_DIR]
#   (по умолчанию VENDOR_DIR = build/vendor)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_DIR="${1:-$ROOT_DIR/build/vendor}"

log()  { printf '\033[0;32m[selfcontained]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[selfcontained] ❌ %s\033[0m\n' "$*" >&2; exit 1; }

[[ -d "$VENDOR_DIR" ]] || fail "нет каталога: $VENDOR_DIR (сначала build_bundled_runtime.command)"
VENV_PY="$VENDOR_DIR/.venv_krab_ear/bin/python"
[[ -x "$VENV_PY" ]] || fail "нет исполняемого $VENV_PY"
[[ -f "$VENDOR_DIR/KrabEar/backend/service.py" ]] || fail "нет $VENDOR_DIR/KrabEar/backend/service.py"

# ── Проверка 1: otool -L на КАЖДОМ Mach-O, найденном ПО СОДЕРЖИМОМУ ────────
# 🔴 Корень сканирования — ВЕСЬ $VENDOR_DIR, не только .venv_krab_ear: живой
# инцидент показал, что чужой .venv (py3.13) может оказаться внутри
# $VENDOR_DIR/KrabEar/ — старое сканирование его целиком пропускало и молча
# отдавало "самодостаточен" при реальной Homebrew-ссылке внутри.
#
# 🔴 БЕЗ `-perm +111` пре-фильтра (первая версия этой правки его имела и
# была неверна): `.dylib` — динамические библиотеки, загружаются через
# dlopen, у НИХ ЧАСТО НЕТ исполняемого бита вовсе (типично `-rw-r--r--`).
# Живая проверка поймала это на собственной ошибке: реальная утечка
# torch/lib/libomp.dylib → /opt/homebrew/... имеет права -rw-r--r--,
# `-perm +111`-версия скана давала "570 файлов, 0 находок" на ТОЙ ЖЕ
# директории, где полное сканирование находит 1124 Mach-O и эту утечку.
# `file -b` дешевле, чем otool на каждом кандидате: сначала фильтруем по
# содержимому (Mach-O) единым батч-вызовом (~100с на 66K файлов —
# приемлемо: гейт гоняется раз на релиз, не на коммит), только потом
# дорогой otool -L на отфильтрованном подмножестве.
log "Сканирую исполняемые Mach-O на зависимости от Homebrew/usr-local (может занять минуту) ..."
FOUND_LEAKS=0
LEAK_REPORT="$(mktemp)"
MACHO_LIST="$(mktemp)"
SCANNED=0

# `file` (БЕЗ -b) печатает "путь:<выравнивающие пробелы/табы>описание" на
# файл — путь остаётся в выводе, поэтому НЕ нужно склеивать два отдельных
# find (склейка через paste двух независимых обходов дерева — источник
# рассинхрона сама по себе). 🔴 Разделитель — двоеточие + ПЕРЕМЕННОЕ число
# пробелов/табов (file выравнивает колонки при батч-выводе), НЕ ": " с ровно
# одним пробелом — живой прогон это доказал: `grep ': Mach-O'` дал 0 находок
# на дереве с 1124 подтверждёнными Mach-O файлами. Живой замер: 66132 файлов
# / ~50с батчем через xargs — гейт гоняется раз на релиз, не на коммит.
# Ревью G2-Krab-Ear-02b2, KE02B2-04: раньше весь конвейер кончался `|| true` — сбой
# find/xargs/file посреди обхода давал укороченный список и «чистый» скан. Теперь
# конвейер `find | xargs file` проверяется отдельно (его сбой — fail), а код 1 от grep
# («нет Mach-O») отделён от кода 2 («ошибка grep»); пустой список ловит гард ниже.
FILE_OUT="$(mktemp)"
trap 'rm -f "$FILE_OUT" "$MACHO_LIST" "$LEAK_REPORT" "${OTOOL_ERR_REPORT:-}" "${LINK_LIST:-}"' EXIT
find "$VENDOR_DIR" -type f -print0 | xargs -0 file > "$FILE_OUT" \
  || fail "find/xargs/file завершились с ошибкой — список файлов неполный, скан недостоверен"
grep_rc=0
grep -E ':[[:space:]]+Mach-O' "$FILE_OUT" | sed -E 's/:[[:space:]]+Mach-O.*//' > "$MACHO_LIST" \
  || grep_rc=$?
# pipefail: rc конвейера = 1, когда grep ничего не нашёл (это покрывает гард SCANNED=0);
# любой другой ненулевой код — реальная ошибка.
[[ "$grep_rc" -eq 0 || "$grep_rc" -eq 1 ]] || fail "grep/sed упали (код $grep_rc) при разборе вывода file"
rm -f "$FILE_OUT"

OTOOL_ERR_REPORT="$(mktemp)"
OTOOL_ERRORS=0
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  SCANNED=$((SCANNED + 1))
  # Ошибка самого otool (битый/обрезанный Mach-O, нет прав) — НЕ «зависимостей нет»:
  # раньше `2>/dev/null | grep || true` превращал её в зелёный результат.
  if ! otool_out="$(otool -L "$f" 2>&1)"; then
    OTOOL_ERRORS=$((OTOOL_ERRORS + 1))
    echo "  $f: ${otool_out%%$'\n'*}" >> "$OTOOL_ERR_REPORT"
    continue
  fi
  hits="$(printf '%s\n' "$otool_out" | grep -E '/opt/homebrew|/usr/local' || true)"
  if [[ -n "$hits" ]]; then
    FOUND_LEAKS=1
    {
      echo "  $f"
      echo "$hits" | sed 's/^/    /'
    } >> "$LEAK_REPORT"
  fi
done < "$MACHO_LIST"
rm -f "$MACHO_LIST"

if [[ "$OTOOL_ERRORS" -gt 0 ]]; then
  echo "" >&2
  head -20 "$OTOOL_ERR_REPORT" >&2
  fail "otool не смог прочитать $OTOOL_ERRORS Mach-O файл(ов) — скан неполный (первые строки выше)"
fi

if [[ "$FOUND_LEAKS" -eq 1 ]]; then
  echo "" >&2
  cat "$LEAK_REPORT" >&2
  rm -f "$LEAK_REPORT"
  fail "найдены Homebrew/usr-local зависимости — бандл НЕ самодостаточен (см. список выше)"
fi
rm -f "$LEAK_REPORT"

# 🔴 Fail-closed на пустом скане (CLAUDE.md: «всякий цикл сбор→обработка
# обязан иметь гард на пустой список — иначе пустота читается как успех»).
# Переименование .venv_krab_ear, смена раскладки PBS-дистрибутива, опечатка
# в пути — и без этого гейт был бы зелёным на НЕсканированном бандле.
[[ "$SCANNED" -gt 0 ]] \
  || fail "0 исполняемых Mach-O файлов найдено — сканирование не нашло НИЧЕГО, путь/раскладка сломаны"
log "otool-скан: чисто ($SCANNED Mach-O файлов, 0 Homebrew/usr-local ссылок)"

# ── Проверка 2: запрещённые файлы внутри KrabEar/ ──────────────────────────
# Belt-and-suspenders к allowlist-копированию в build_bundled_runtime.command
# (git ls-files) — эта проверка не должна зависеть от того, что копирующая
# сторона больше не ошибётся; она независимо ловит РЕЗУЛЬТАТ, а не механизм.
log "Проверяю на .env/.venv/логи/данные внутри KrabEar/ ..."
# Ревью G2-Krab-Ear-02b2, KE02B2-03: раньше `-maxdepth 1` и `|| true` — вложенные
# `KrabEar/x/.env`, `KrabEar/x/y.log` пропускались, а сбой find давал «не найдено».
# Секреты/логи/чужой venv/кэш ищем по ВСЕМУ дереву KrabEar/; `data` — только в корне
# (подпакеты вправе называться data). Любая ошибка find — fail.
FORBIDDEN="$(find "$VENDOR_DIR/KrabEar" \
  \( -name ".env" -o -name ".env.*" -o -name ".venv*" \
     -o -name "*.log" -o -name ".pytest_cache" \) -print)" \
  || fail "find по $VENDOR_DIR/KrabEar завершился с ошибкой — проверка запрещённых файлов недостоверна"
FORBIDDEN_DATA="$(find "$VENDOR_DIR/KrabEar" -maxdepth 1 -name "data" -print)" \
  || fail "find (data) по $VENDOR_DIR/KrabEar завершился с ошибкой"
FORBIDDEN="$(printf '%s\n%s\n' "$FORBIDDEN" "$FORBIDDEN_DATA" | sed '/^$/d')"
if [[ -n "$FORBIDDEN" ]]; then
  echo "$FORBIDDEN" | sed 's/^/  /' >&2
  fail "найдены файлы, которым не место в дистрибутиве (секреты/логи/чужой venv) — см. список выше"
fi
log "Запрещённых файлов внутри KrabEar/: не найдено"

# ── Проверка 2b: symlink не должен вести за пределы бандла (KE02B2-08) ────
# `find -type f` символические ссылки пропускает: ссылка на /opt/homebrew/... внутри
# бандла выглядела бы чистой, а у получателя была бы битой (или тянула чужую
# библиотеку). Резолвим КАЖДУЮ ссылку; наружу, в никуда или с ошибкой резолва — fail.
log "Проверяю, что symlink внутри бандла не ведут за его пределы ..."
LINK_LIST="$(mktemp)"
find "$VENDOR_DIR" -type l -print0 > "$LINK_LIST" \
  || fail "find -type l завершился с ошибкой — проверка symlink недостоверна"
LINK_BAD="$(/usr/bin/python3 -I - "$VENDOR_DIR" "$LINK_LIST" <<'PY'
import os, sys
root = os.path.realpath(sys.argv[1])
bad = []
with open(sys.argv[2], "rb") as fh:
    links = [x for x in fh.read().split(b"\0") if x]
for raw in links:
    link = os.fsdecode(raw)
    target = os.path.realpath(link)
    inside = target == root or target.startswith(root + os.sep)
    if not inside or not os.path.exists(target):
        bad.append(f"{link} -> {target}")
print("\n".join(bad[:20]))
PY
)" || fail "разбор symlink завершился с ошибкой"
rm -f "$LINK_LIST"
if [[ -n "$LINK_BAD" ]]; then
  echo "$LINK_BAD" | sed 's/^/  /' >&2
  fail "symlink ведут за пределы бандла или в никуда (первые строки выше) — бандл НЕ самодостаточен"
fi
log "Symlink внутри бандла: все резолвятся внутрь"

# ── Проверка 3: критичные импорты с урезанным PATH, без user-site ─────────
# /usr/bin:/bin — минимум, который есть на ЛЮБОМ Mac без единой сторонней
# установки. `-s`: без user-site (~/.local/lib/...) — иначе пакет, выпавший
# из requirements.txt, мог бы молча импортироваться за счёт user-site
# автора, а у получателя дать ImportError.
log "Проверяю критичные импорты с PATH=/usr/bin:/bin, без user-site (симуляция чистого Mac) ..."
CLEAN_PATH_OUTPUT="$(env -i PATH=/usr/bin:/bin HOME="$HOME" "$VENV_PY" -s -c "
import mlx_whisper, numpy, sounddevice, flask, requests, pydantic_settings
print('imports ok (urezan PATH, no user-site)')
" 2>&1)" || {
  echo "$CLEAN_PATH_OUTPUT" >&2
  fail "критичный импорт упал с урезанным PATH — рантайм-зависимость от Homebrew/usr-local вне link-time"
}
log "Импорты с урезанным PATH: ок"

log ""
log "Готово: $VENDOR_DIR самодостаточен ($SCANNED Mach-O файлов проверено)."
