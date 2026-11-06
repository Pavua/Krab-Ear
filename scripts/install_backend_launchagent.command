#!/bin/bash
# Установка и переустановка backend временно отложены до квалифицированной
# процедуры admission/drain: idle-снимок не запрещает новые записи/запросы.
set -uo pipefail

while [ "$#" -gt 0 ]; do
  case "$1" in
    --force) shift ;;
    --wait)
      if [ "$#" -lt 2 ]; then
        echo "unknown or missing --wait value" >&2
        exit 2
      fi
      case "$2" in
        ''|*[!0-9]*) echo "invalid --wait value" >&2; exit 2 ;;
      esac
      if [ "${#2}" -gt 6 ]; then
        echo "invalid --wait value" >&2
        exit 2
      fi
      shift 2
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "REFUSED: установка backend отложена до проверки общего admission/drain." >&2
exit 1
