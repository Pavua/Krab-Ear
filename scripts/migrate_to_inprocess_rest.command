#!/usr/bin/env bash
# Миграция отдельного REST временно отложена: снимок активности backend не
# запрещает новые REST-запросы и не доказывает завершение текущих обработчиков.
# Восстановить lifecycle только после квалифицированного admission/drain.
set -uo pipefail

while [ "$#" -gt 0 ]; do
  case "$1" in
    --rollback) shift ;;
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

echo "REFUSED: миграция и откат REST отложены до проверки общего admission/drain." >&2
exit 1
