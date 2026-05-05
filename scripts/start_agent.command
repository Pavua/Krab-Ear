#!/bin/zsh
# ------------------------------------------------------------------
# DEPRECATED ENTRY POINT — now redirects to Krab Ear.app bundle.
#
# Background:
#   Это исторический launcher для native/runtime/KrabEarAgent. Когда вызывается
#   из ecosystem-уровневых скриптов (Краб's `Start Full Ecosystem.command`,
#   `new start_krab.command`), он запускал runtime бинарник напрямую → возникал
#   two-binary drift (см. `memory/blocker_two_binary_drift_2026-05-03.md`).
#
# Root cause fix (2026-05-05):
#   Этот скрипт ТЕПЕРЬ просто открывает Krab Ear.app bundle через
#   `/usr/bin/open`. Все callers — old launchd plists, Краб ecosystem,
#   ручные запуски — автоматически прозрачно используют бандл, без
#   изменения их кода. runtime бинарник остаётся для dev (`./native/runtime/...`),
#   но никто его больше не запускает через скрипт.
#
# Defense-in-depth:
#   `main.swift` дополнительно содержит self-redirect: если runtime обнаружит
#   что он запущен с native/runtime/KrabEarAgent а bundle лежит рядом, он
#   exec'нет bundle и завершит сам процесс.
# ------------------------------------------------------------------

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_PATH="$ROOT_DIR/Krab Ear.app"

if [[ ! -d "$BUNDLE_PATH" ]]; then
  echo "❌ Krab Ear.app не найден по пути: $BUNDLE_PATH" >&2
  echo "   Соберите его через make build && make sign перед запуском." >&2
  exit 1
fi

# Передаём все аргументы в bundle через --args. `open -W` ждёт завершения,
# что нужно когда нас вызывают через `nohup ... &` из ecosystem-скрипта.
exec /usr/bin/open -W "$BUNDLE_PATH" --args "$@"
