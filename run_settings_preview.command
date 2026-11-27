#!/bin/bash
# KEG2-22: каталог проекта — там, где лежит сам скрипт (без жёсткого абсолютного пути).
cd "$(dirname "$0")" || exit 1
swiftc -parse-as-library settings_preview.swift -o SettingsPreviewApp || exit 1
./SettingsPreviewApp
