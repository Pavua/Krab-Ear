#!/bin/bash
# Общий fail-closed selector для локальной доставки агента (Bash 3.2 / zsh).
# Вызывающий скрипт фиксирует SHA-1 один раз ДО копирования/остановки агента.
# CDHash меняется при сборке; TCC сохраняет grant по сертификату + bundle ID.

resolve_agent_signing_identity() {
  local identities fingerprint
  if ! identities="$(security find-identity -v -p codesigning)"; then
    echo 'Ошибка: Keychain signing identities недоступны; обновление отменено.' >&2
    return 1
  fi
  if ! fingerprint="$(printf '%s\n' "$identities" | awk '
    BEGIN { count = 0; invalid = 0 }
    {
      quote = index($0, "\"")
      if (!quote) next
      tail = substr($0, quote + 1)
      endquote = index(tail, "\"")
      if (!endquote || substr(tail, 1, endquote - 1) != "Krab Ear Dev Local") next
      count++
      suffix = substr(tail, endquote + 1)
      if ($1 !~ /^[0-9]+\)$/ || length($2) != 40 || $2 !~ /^[0-9A-Fa-f]+$/ || suffix !~ /^[[:space:]]*$/) invalid = 1
      fingerprint = toupper($2)
    }
    END {
      if (count != 1 || invalid) exit 1
      print fingerprint
    }
  ')"; then
    echo 'Ошибка: требуется одна действующая identity "Krab Ear Dev Local"; обновление отменено.' >&2
    return 1
  fi
  printf '%s\n' "$fingerprint"
}
