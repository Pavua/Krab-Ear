# DEV_CODESIGN — Локальная подпись для разработки

## Проблема: TCC сбрасывает права после каждого rebuild

macOS TCC (Transparency, Consent, and Control) — подсистема, которая управляет
разрешениями Accessibility и Microphone. При ad-hoc подписи (`codesign -s -`)
каждая пересборка Swift-агента создаёт **новый cdhash**. TCC идентифицирует
процесс именно по cdhash → считает его новым приложением → отзывает ранее
выданные разрешения → пользователь снова вручную даёт права.

## Решение: self-signed identity в Keychain

Self-signed сертификат «Krab Ear Dev Local» хранится в login Keychain.
`codesign -s "Krab Ear Dev Local"` подписывает бинарь стабильным ключом.
После первой подписи TCC начинает матчить по **bundle identifier**
(`com.antigravity.krab-ear`) + **signing identity**, а не по cdhash.
Права Accessibility/Microphone сохраняются между rebuild'ами.

## Как работает

```
openssl genrsa   →  RSA-2048 private key
openssl req      →  CSR  (CN = "Krab Ear Dev Local")
openssl x509     →  self-signed cert (3650 дней)
                    extensions: keyUsage=digitalSignature
                                extendedKeyUsage=codeSigning
openssl pkcs12   →  .p12 bundle
security import  →  login.keychain-db
security add-trusted-cert  →  доверие для codesigning
```

`update_agent.command` автоматически определяет наличие identity и переключается
с ad-hoc на `"Krab Ear Dev Local"` без ручных изменений.

## Первичная настройка (one-time)

```bash
./scripts/create_local_signing_identity.command
```

При первом запуске `codesign` с новой identity macOS покажет диалог Keychain.
Выберите **«Всегда разрешать» (Always Allow)** для `/usr/bin/codesign`.

После этого пересоберите агент:

```bash
./scripts/update_agent.command
```

Проверить, что identity активна:

```bash
security find-identity -v -p codesigning | grep "Krab Ear Dev Local"
# Ожидаемый вывод: 1) <hash> "Krab Ear Dev Local"
```

## Если что-то сломалось

Пересоздать identity с нуля:

```bash
security delete-identity -c "Krab Ear Dev Local"
./scripts/create_local_signing_identity.command
```

Проверить, что identity действительно используется при сборке:

```bash
codesign -dv "Krab Ear.app" 2>&1 | grep -E "Authority|Identifier"
```

Сброс TCC-прав (если всё равно revoke):

```bash
tccutil reset All com.antigravity.krab-ear
# После этого вручную добавьте приложение в System Settings → Privacy → Accessibility
```

## Ограничения

- **Self-signed ≠ Apple Developer certificate.** Gatekeeper по-прежнему будет
  блокировать сборку при запуске на чужой машине. Identity предназначена
  **только для локальной разработки**.
- **Не подходит для дистрибуции.** Для App Store / Notarization нужен Developer ID.
- **Срок действия сертификата — 10 лет.** После истечения повторите setup.
- **Машинозависимо.** На каждом Mac нужно запустить скрипт отдельно.

## Dry-run режим

Посмотреть, что именно будет сделано, без реальных изменений:

```bash
./scripts/create_local_signing_identity.command --dry-run
```
