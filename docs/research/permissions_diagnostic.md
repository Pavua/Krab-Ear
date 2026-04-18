# Диагностика прав доступа Krab Ear на macOS

## 1. Текущий коdesign идентификатор

```
Identifier: com.antigravity.krab-ear
Format: app bundle with Mach-O thin (arm64)
Signature: ad-hoc (самоподписанный)
```

✅ Идентификатор корректный — соответствует ожидаемому.

---

## 2. Состояние TCC (Transparency, Consent, Control)

### Найдено для `com.antigravity.krab-ear`:
- ✅ `kTCCServiceMicrophone` — **РАЗРЕШЕНО** (auth_value=2)
- ✅ `kTCCServiceAppleEvents` — **РАЗРЕШЕНО** (auth_value=2)
- ❌ `kTCCServiceAccessibility` — **НЕ НАЙДЕНО**

### Проблема
В TCC базе **отсутствует запись** `com.antigravity.krab-ear | kTCCServiceAccessibility`. Это объясняет, почему:
1. Autopaste не работает (требует Accessibility для pasteboard access)
2. Приложение повторно запрашивает разрешение

---

## 3. Обнаруженные дублирующиеся TCC записи

Найдены **старые записи** под путями к бинарям (а не bundle ID):

```
/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent/.build/arm64-apple-macosx/release/KrabEarAgent
  → kTCCServiceMicrophone (2 — РАЗРЕШЕНО)

/Users/pablito/Antigravity_AGENTS/Krab Ear/native/runtime/KrabEarAgent
  → kTCCServiceMicrophone (2 — РАЗРЕШЕНО)

/Users/pablito/Applications/Start Krab.app/Contents/MacOS/applet
  → kTCCServiceMicrophone (2 — РАЗРЕШЕНО)
```

**Диагноз:** Приложение было переименовано/пересигнировано, старые TCC записи по пути теперь не совпадают с новым app bundle ID.

---

## 4. Корневая причина

**Мультифакторный сценарий:**

1. **Старые TCC записи по пути** остались от предыдущих версий
2. **Новый app bundle** имеет коррект ID `com.antigravity.krab-ear`, но macOS видит это как *новый* идентификатор
3. **Пользователь выдаёт разрешение** в Privacy & Security, но:
   - Accessibility вообще не запрашивается (или запрос игнорируется)
   - Даже если выдать разрешение, TCC не записывает `Accessibility` для этого ID

---

## 5. Инструкция по исправлению

### Шаг 1: Удалить старые TCC записи (по пути)

**Не выполнять сами команды** — дать пользователю:

```bash
# Удалить старые записи по пути (они использовались старой версией)
tccutil reset Microphone /Users/pablito/Antigravity_AGENTS/Krab\ Ear/native/KrabEarAgent/.build/arm64-apple-macosx/release/KrabEarAgent
tccutil reset Microphone /Users/pablito/Antigravity_AGENTS/Krab\ Ear/native/runtime/KrabEarAgent
```

### Шаг 2: Очистить новый app bundle ID (заново запросить)

```bash
# Полностью сбросить всё для нового bundle ID
tccutil reset Microphone com.antigravity.krab-ear
tccutil reset AppleEvents com.antigravity.krab-ear
```

### Шаг 3: Дать разрешения заново

1. Открыть **System Settings → Privacy & Security → Microphone**
   - Найти «Krab Ear.app»
   - Убедиться, что переключатель **ON** (зелёный)

2. **System Settings → Privacy & Security → Accessibility**
   - Добавить «Krab Ear.app» вручную (если не появилось автоматически)
   - Включить переключатель

3. **System Settings → Privacy & Security → Apple Events**
   - Найти «Krab Ear.app»
   - Убедиться **ON**

### Шаг 4: Перезапустить приложение

```bash
killall KrabEarAgent  # Завершить текущий процесс
# Запустить "Start Krab Ear.app" или Cmd+Space → Krab Ear
```

---

## 6. Почему это случилось?

- App bundle был пересигнирован/переименован → ID изменился на `com.antigravity.krab-ear`
- Старые TCC записи по абсолютному пути `/Users/.../KrabEarAgent` больше не совпадают
- macOS TCC требует либо точного bundle ID, либо точного пути → Mismatch

---

## 📋 Краткая сводка для пользователя

| Статус | Проблема |
|--------|----------|
| ✅ Codesign ID | Корректный (`com.antigravity.krab-ear`) |
| ✅ Microphone | Выдано для bundle ID |
| ✅ AppleEvents | Выдано для bundle ID |
| ❌ **Accessibility** | **ОТСУТСТВУЕТ** — это блокирует autopaste |
| ⚠️ Старые записи | Дублирующиеся по пути (уже не используются) |

**Решение:** Удалить старые записи по пути + выдать Accessibility для нового bundle ID в System Settings.
