#!/bin/bash
# Крутой скрипт запуска Krab Ear Agent
# Сгенерировано Antigravity (Senior Autonomous Architect)

echo "🦀 Запускаю Krab Ear Agent..."
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
./.build/release/KrabEarAgent &
echo "✅ Агент запущен в фоновом режиме."
