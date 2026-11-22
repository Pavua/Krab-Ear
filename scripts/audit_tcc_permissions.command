#!/usr/bin/env bash
# audit_tcc_permissions.command — Wave 686
# Reads TCC.db and reports Krab Ear permission grants.
# Output: colored stdout + JSON (по умолчанию ~/Library/Logs/KrabEar/tcc-audit.json, 0600;
#         путь можно задать KRAB_TCC_AUDIT_OUT)
#
# Usage: double-click or ./scripts/audit_tcc_permissions.command
# Requires Terminal with Full Disk Access (System Settings > Privacy > Full Disk Access).

set -euo pipefail
cd "$(dirname "$0")/.."

# ── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

TCC_DB="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
APP_BUNDLE="$(pwd)/Krab Ear.app"
OUT_JSON="${KRAB_TCC_AUDIT_OUT:-$HOME/Library/Logs/KrabEar/tcc-audit.json}"

# auth_value codes: 0=deny 2=allow 3=limited 4=always_deny 5=prompt/unknown
auth_label() {
  case "$1" in
    0) echo "DENIED" ;;
    2) echo "GRANTED" ;;
    3) echo "LIMITED" ;;
    4) echo "ALWAYS_DENIED" ;;
    5) echo "PROMPT" ;;
    *) echo "UNKNOWN($1)" ;;
  esac
}

auth_color() {
  case "$1" in
    GRANTED)      printf "%b" "$GREEN" ;;
    DENIED|ALWAYS_DENIED) printf "%b" "$RED" ;;
    PROMPT)       printf "%b" "$YELLOW" ;;
    *) printf "%b" "$CYAN" ;;
  esac
}

svc_short() {
  case "$1" in
    kTCCServiceAccessibility)  echo "Accessibility" ;;
    kTCCServiceMicrophone)     echo "Microphone" ;;
    kTCCServiceScreenCapture)  echo "ScreenRecording" ;;
    *) echo "$1" ;;
  esac
}

# ── header ─────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}═══ Krab Ear TCC Permissions Audit (Wave 686) ═══${RESET}"
echo -e "${CYAN}App bundle:${RESET} $APP_BUNDLE"
echo -e "${CYAN}TCC DB:${RESET}     $TCC_DB"

# ── codesign info ──────────────────────────────────────────────────────────
CS_HASH="N/A"; CS_AUTHORITY="N/A"; CS_IDENTIFIER="com.antigravity.krab-ear"
if [[ -d "$APP_BUNDLE" ]]; then
  CS_RAW=$(codesign --display --verbose=4 "$APP_BUNDLE/Contents/MacOS/KrabEarAgent" 2>&1 || true)
  CS_HASH=$(echo "$CS_RAW"     | grep '^CDHash='     | head -1 | cut -d= -f2)
  CS_AUTHORITY=$(echo "$CS_RAW" | grep '^Authority='  | head -1 | cut -d= -f2)
  CS_IDENTIFIER=$(echo "$CS_RAW"| grep '^Identifier=' | head -1 | cut -d= -f2)
  echo -e "${CYAN}Bundle ID:${RESET}  $CS_IDENTIFIER"
  echo -e "${CYAN}CDHash:${RESET}     $CS_HASH"
  echo -e "${CYAN}Authority:${RESET}  ${CS_AUTHORITY:-adhoc/self-signed}"
else
  echo -e "${YELLOW}WARNING: App bundle not found — CDHash unavailable.${RESET}"
fi

# ── TCC query ──────────────────────────────────────────────────────────────
echo -e "\n${BOLD}── TCC entries (bundle-ID + path-based) ──${RESET}"

if [[ ! -r "$TCC_DB" ]]; then
  echo -e "${RED}Cannot read TCC.db."
  echo -e "Grant Terminal Full Disk Access in System Settings > Privacy & Security > Full Disk Access, then re-run.${RESET}"
  exit 1
fi

ROWS=$(sqlite3 "$TCC_DB" \
  "SELECT service, client, client_type, auth_value, last_modified \
   FROM access \
   WHERE client LIKE '%krab-ear%'
      OR client LIKE '%antigravity.krab%'
      OR client LIKE '%KrabEarAgent%';" 2>/dev/null || true)

SERVICES_OF_INTEREST=(
  "kTCCServiceAccessibility"
  "kTCCServiceMicrophone"
  "kTCCServiceScreenCapture"
)

# Accumulate JSON entries via Python
JSON_LINES=""
if [[ -z "$ROWS" ]]; then
  echo -e "${YELLOW}  No Krab Ear entries found in TCC.db.${RESET}"
else
  printf "  %-20s %-6s %-15s  %s\n" "Service" "Type" "Status" "Client / Timestamp"
  printf "  %s\n" "$(printf '─%.0s' {1..80})"
  while IFS='|' read -r svc client ctype av ts; do
    label=$(auth_label "$av")
    color=$(auth_color "$label")
    short=$(svc_short "$svc")
    human_ts=$(python3 -c "import datetime; print(datetime.datetime.fromtimestamp($ts).strftime('%Y-%m-%d %H:%M:%S'))" 2>/dev/null || echo "ts:$ts")
    printf "  ${color}%-20s${RESET} [%s]  %-15s  %s  (%s)\n" \
      "$short" "$ctype" "$label" "$client" "$human_ts"
    JSON_LINES+="${svc}|${client}|${ctype}|${av}|${ts}|${label}|${human_ts}"$'\n'
  done <<< "$ROWS"
fi

# ── key-service summary ────────────────────────────────────────────────────
echo -e "\n${BOLD}── Key-service summary ──${RESET}"
SUMMARY_LINES=""
for SVC in "${SERVICES_OF_INTEREST[@]}"; do
  short=$(svc_short "$SVC")
  # Статус считаем по ТЕКУЩЕМУ приложению (bundle-id): максимум auth_value по всем клиентам
  # прятал отказ текущему бандлу за старым path-based разрешением. Если записи для бандла
  # нет — показываем path-based статус с явной пометкой.
  # Точное равенство полей (grep по "^svc|bundle.id|" воспринимал точки идентификатора как
  # regex-шаблон), а при нескольких записях текущего клиента побеждает САМАЯ СВЕЖАЯ по last_modified.
  best=$(printf '%s\n' "$ROWS" | awk -F'|' -v svc="$SVC" -v bid="$CS_IDENTIFIER" \
      '$1 == svc && $2 == bid { print $5 "|" $4 }' | sort -t'|' -k1,1n | tail -1 | cut -d'|' -f2)
  via_path=""
  if [[ -z "$best" ]]; then
    best=$(printf '%s\n' "$ROWS" | awk -F'|' -v svc="$SVC" '$1 == svc { print $4 }' | sort -rn | head -1)
    [[ -z "$best" ]] || via_path=" (path-based only)"
  fi
  if [[ -z "$best" ]]; then
    echo -e "  ${YELLOW}$(printf '%-20s' "$short")  NOT IN TCC.db  ← needs user grant${RESET}"
    SUMMARY_LINES+="${short}|MISSING"$'\n'
  else
    label=$(auth_label "$best")
    color=$(auth_color "$label")
    echo -e "  $(printf '%-20s' "$short")  ${color}${label}${via_path}${RESET}"
    SUMMARY_LINES+="${short}|${label}${via_path}"$'\n'
  fi
done

# ── codesign / TCC cross-check ─────────────────────────────────────────────
echo -e "\n${BOLD}── Codesign / TCC cross-check ──${RESET}"
BUNDLE_ID_ROW=$(echo "$ROWS" | grep "com.antigravity.krab-ear" || true)
PATH_ROW=$(echo "$ROWS"      | grep "KrabEarAgent"             || true)

if [[ -n "$BUNDLE_ID_ROW" ]]; then
  echo -e "  ${GREEN}Bundle-ID grant present${RESET} (com.antigravity.krab-ear) — survives binary rebuild"
else
  echo -e "  ${YELLOW}No bundle-ID grant found${RESET} — path-only grants may break after rebuild"
  echo -e "  ${CYAN}Fix: run scripts/repair_permissions.command${RESET}"
fi
if [[ -n "$PATH_ROW" ]]; then
  echo -e "  ${YELLOW}Path-based grant(s) detected${RESET} — OK if stable-identity signed; fragile otherwise"
fi
echo -e "  Current CDHash: ${CYAN}${CS_HASH:-N/A}${RESET}"

# ── write JSON ────────────────────────────────────────────────────────────
# Значения из TCC.db (client, путь) — ДАННЫЕ: передаются в Python через окружение
# при quoted-heredoc, а не подстановкой в исходник (иначе `"""` в имени клиента
# исполнялся бы как код). Запись — во временный файл 0600 рядом + атомарный
# os.replace: симлинк по целевому пути не разыменовывается.
# Права меняем ТОЛЬКО у каталога, который создали сами; чужой существующий (KRAB_TCC_AUDIT_OUT
# мог указать в общий каталог) не трогаем — файл и так создаётся с 0600.
OUT_DIR="$(dirname "$OUT_JSON")"
if [[ ! -d "$OUT_DIR" ]]; then
  mkdir -p "$OUT_DIR"
  chmod 700 "$OUT_DIR"
fi
TCC_JSON_LINES="$JSON_LINES" TCC_SUMMARY_LINES="$SUMMARY_LINES" TCC_APP_BUNDLE="$APP_BUNDLE" \
TCC_BUNDLE_ID="$CS_IDENTIFIER" TCC_CD_HASH="$CS_HASH" TCC_AUTHORITY="${CS_AUTHORITY:-adhoc}" \
TCC_OUT="$OUT_JSON" python3 - <<'PYEOF'
import datetime
import json
import os
import tempfile

entries = []
for line in os.environ.get("TCC_JSON_LINES", "").splitlines():
    if not line.strip():
        continue
    parts = line.split('|')
    if len(parts) < 7:
        continue
    svc, client, ctype, av, ts, label, human_ts = parts[:7]
    entries.append({
        "service": svc,
        "short": svc.replace("kTCCService", ""),
        "client": client,
        "client_type": int(ctype) if ctype.isdigit() else ctype,
        "auth_value": int(av) if av.isdigit() else av,
        "auth_label": label,
        "last_modified_epoch": int(ts) if ts.isdigit() else ts,
        "last_modified_human": human_ts,
    })

summary = {}
for line in os.environ.get("TCC_SUMMARY_LINES", "").splitlines():
    if "|" in line:
        key, value = line.split("|", 1)
        summary[key] = value

out = {
    "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "wave": 686,
    "app_bundle": os.environ["TCC_APP_BUNDLE"],
    "bundle_id": os.environ["TCC_BUNDLE_ID"],
    "cd_hash": os.environ["TCC_CD_HASH"],
    "authority": os.environ["TCC_AUTHORITY"],
    "key_services": summary,
    "tcc_entries": entries,
}
target = os.environ["TCC_OUT"]
fd, tmp = tempfile.mkstemp(prefix=".tcc-audit.", suffix=".tmp", dir=os.path.dirname(target) or ".")
try:
    with os.fdopen(fd, "w") as f:  # mkstemp: O_EXCL, режим 0600
        json.dump(out, f, indent=2)
    os.replace(tmp, target)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print(f"  JSON written: {target}  ({len(entries)} entries)")
PYEOF

echo -e "\n${BOLD}Done.${RESET}\n"
