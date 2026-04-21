# Krab Ear — Troubleshooting Permissions (macOS 26+)

Руководство по решению проблем с permissions на macOS 26 (Sequoia) и выше.

## Симптомы и что делать

| Симптом | Root cause | Быстрый fix |
|---|---|---|
| Right Option не реагирует | Нет Input Monitoring или Accessibility | Re-grant через `System Settings` → `Privacy & Security` |
| Текст скопировался в буфер, но не вставился | Нет Accessibility | Добавить Krab Ear в Accessibility list, toggle зелёный |
| macOS запрашивает permission повторно (cooldown 600s) | PasteService fallback loop | Подождать 10 мин или reboot |
| App в Dock, но окна нет | LSUIElement=true (by design, menu bar only) | Кликни на KE icon в правом верхнем углу (menu bar) |
| Icon в menu bar не виден | Memory pressure / icon overflow | Reboot macOS, или найди в Control Center |
| Agent log обрывается после "Настройки загружены" | Silent FileHandle fail после TCC reset | Будет исправлено в PR #153 (AgentLogger resilience) |

## Быстрая проверка TCC

```bash
sqlite3 "$HOME/Library/Application Support/com.apple.TCC/TCC.db" \
  "SELECT service, client, auth_value FROM access WHERE client LIKE '%krab%';"
```

**Ожидаемые строки** (все с `auth_value=2` что = allowed):

- `kTCCServiceMicrophone | com.antigravity.krab-ear | 2` — запись аудио
- `kTCCServiceAccessibility | com.antigravity.krab-ear | 2` — simulate paste Cmd+V
- `kTCCServiceListenEvent | com.antigravity.krab-ear | 2` — global hotkey
- `kTCCServiceAppleEvents | com.antigravity.krab-ear | 2` — osascript fallback

Если **каких-то строк нет** — значит permission не granted. Даже если toggle в System Settings визуально green.

## Manual re-grant procedure

Проблема macOS 26: System Settings показывает toggle включённым, но TCC db не обновляется. Решение — явный add через `+`:

1. **System Settings → Privacy & Security → Accessibility**
2. Если Krab Ear в списке — **удали** через `−`
3. Нажми **`+`** внизу
4. В Finder нажми **`Cmd+Shift+G`** (Go to folder) → вставь точный path:
   ```
   /Users/pablito/Antigravity_AGENTS/Krab Ear
   ```
5. Enter → выбери `Krab Ear.app` → Open
6. Toggle включится **зелёным автоматически**
7. Повтори для **Input Monitoring** (ниже в списке Privacy & Security)

## Reset как last resort

Если re-grant не работает — полный reset через tccutil:

```bash
tccutil reset All com.antigravity.krab-ear
```

Затем **обязательно reboot macOS** — очищает TCC kernel cache.

## Binary rebuild invalidates TCC

Каждый `swift build -c release` создаёт binary с новым хэшем. macOS TCC видит это как "другое приложение" и invalid'ит previous grants даже если bundle ID тот же (`com.antigravity.krab-ear`).

**После каждого rebuild**:
1. Rebuild + install + code sign
2. Kill + relaunch app
3. Re-grant Accessibility + Input Monitoring через System Settings
4. Попробуй Right Option

Если процесс слишком утомителен — используй `launchd` bootstrap из `scripts/start_agent.command` — permissions по bundle ID survive между reboots лучше.

## Memory pressure affects TCC

При `memory_pressure` > критического (less than 5% free):
- TCC queries задерживаются
- System Settings toggle не сохраняется
- App startup прерывается silently

**Проверь**:
```bash
memory_pressure | head -3
```

**Освободи память**:
- Quit Docker Desktop если не нужен (1.5 GB)
- Close Chrome tabs (youtube, canva)
- Quit Xcode swift-frontend (если builds не активны)

## FAQ

**Q: Почему дважды запрашивает Accessibility?**  
A: PasteService имеет fallback chain: AX → osascript → show prompt. Каждый уровень может fail — последний prompt'ит. Normal если впервые. Если повторно — что-то не granted.

**Q: Нужно ли re-grant после каждого restart app?**  
A: Нет, только после rebuild binary. Чистый relaunch (kill + open) сохраняет permissions.

**Q: Что делает `tccutil reset Accessibility com.antigravity.krab-ear`?**  
A: Удаляет все grants для данного bundle ID. Следующий запуск app — будет prompt.

**Q: Как debug'ить если log не пишется?**  
A: После merge PR #153 — AgentLogger будет fallback на `NSLog` (stderr) при FileHandle fail. Смотреть в `Console.app`.

**Q: Reboot действительно нужен?**  
A: Да, в случае TCC kernel cache corruption. Обычный способ очистить — только reboot (tccd под SIP, kickstart не работает).

**Q: Apple знает про этот баг?**  
A: Да, [open issues на OpenRadar](https://openradar.appspot.com/search?query=TCC+Sequoia). Waiting fix в macOS 27.

## Reference

- Session findings: `~/.claude/projects/-Users-pablito-Antigravity-AGENTS-Krab-Ear/memory/debug_macos26_tcc_quirks.md`
- Apple docs: [Transparency, Consent, and Control](https://developer.apple.com/documentation/security/optimizing_app_launch_performance#3561455)
- TCC internals: [@tolitius/cr7](https://github.com/tolitius/cr7) (open-source analyzer)
