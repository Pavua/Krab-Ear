#!/usr/bin/env bash
# audit_tcc_permissions.command — Wave 686
# Reads TCC.db and reports Krab Ear permission grants.
# Output: colored stdout + /tmp/krab-tcc-audit.json
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
OUT_JSON="/tmp/krab-tcc-audit.json"

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
SUMMARY_JSON=""
for SVC in "${SERVICES_OF_INTEREST[@]}"; do
  short=$(svc_short "$SVC")
  best=$(echo "$ROWS" | { grep "^${SVC}|" || true; } | awk -F'|' '{print $4}' | sort -rn | head -1)
  if [[ -z "$best" ]]; then
    echo -e "  ${YELLOW}$(printf '%-20s' "$short")  NOT IN TCC.db  ← needs user grant${RESET}"
    SUMMARY_JSON+="\"$short\": \"MISSING\","
  else
    label=$(auth_label "$best")
    color=$(auth_color "$label")
    echo -e "  $(printf '%-20s' "$short")  ${color}${label}${RESET}"
    SUMMARY_JSON+="\"$short\": \"$label\","
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
python3 - <<PYEOF
import json, sys

raw = """${JSON_LINES}"""
entries = []
for line in raw.strip().splitlines():
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

summary_raw = """${SUMMARY_JSON}"""
summary = {}
for item in summary_raw.strip().rstrip(',').split('",'):
    item = item.strip()
    if '": "' in item:
        k, v = item.split('": "', 1)
        summary[k.strip('"')] = v.rstrip('"')

out = {
    "generated": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "wave": 686,
    "app_bundle": "$APP_BUNDLE",
    "bundle_id": "$CS_IDENTIFIER",
    "cd_hash": "$CS_HASH",
    "authority": "${CS_AUTHORITY:-adhoc}",
    "key_services": summary,
    "tcc_entries": entries,
}
with open("$OUT_JSON", "w") as f:
    json.dump(out, f, indent=2)
print(f"  JSON written: $OUT_JSON  ({len(entries)} entries)")
PYEOF

echo -e "\n${BOLD}Done.${RESET}\n"
