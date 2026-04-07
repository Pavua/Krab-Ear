"""Скрипт проверки работоспособности Krab Ear.

Проверяет импорты, конфигурацию и готовность STT к работе.
"""

import sys
import os
from pathlib import Path

# Добавляем пути
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

print("🔍 Проверка состояния Krab Ear...")

try:
    from core.config import settings
    print(f"✅ Конфигурация загружена. База: {settings.DATA_DIR}")
    
    from core.utils import TextUtils
    print("✅ Утилиты текста доступны.")
    
    from core.engine import AudioEngine
    engine = AudioEngine()
    print(f"✅ AudioEngine инициализирован. Модель: {engine.current_model}")
    
    from backend.rest_server import app
    print("✅ REST API сервер готов к запуску.")
    
    print("\n🚀 КРАБ ГОТОВ К РАБОТЕ!")
except Exception as e:
    print(f"\n❌ ОШИБКА ПРОВЕРКИ: {e}")
    sys.exit(1)
