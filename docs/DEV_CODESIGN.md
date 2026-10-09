# DEV_CODESIGN — Локальная подпись для разработки

## Проблема: TCC сбрасывает права после каждого rebuild

macOS TCC (Transparency, Consent, and Control) управляет разрешениями
Accessibility и Microphone. Разрешённая запись содержит code requirement:
условия, которым должна соответствовать подпись приложения. Для ad-hoc подписи
(`codesign -s -`) это может быть конкретный **CDHash**. Если новый бинарник ему
не соответствует, включённый переключатель в System Settings сам по себе
не означает, что работающий процесс получил доступ.

## Решение: self-signed identity в Keychain

Self-signed сертификат «Krab Ear Dev Local» хранится в login Keychain.
`codesign -s "Krab Ear Dev Local"` подписывает бинарь стабильным ключом.
При certificate-based requirement TCC проверяет **bundle identifier**
(`com.antigravity.krab-ear`) и конкретный сертификат. CDHash при rebuild может
измениться: стабильной должна оставаться signing identity. Разрешение действует,
пока новая подпись удовлетворяет сохранённому requirement; одинакового имени
сертификата для этого недостаточно.

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

Локальные пути `make sign`/`make app`, `build_and_deploy.command`,
`update_agent.command` и repair-режимы проверки бинарников должны использовать
существующий `Krab Ear Dev Local`. Selector выбирает однозначный fingerprint
сертификата. Если Keychain недоступен, сертификат отсутствует/неоднозначен либо
подпись не удалась, установка останавливается. Автоматический переход на ad-hoc
в этих путях запрещён. Дистрибуционный assembler с явным `--identity` имеет
отдельный контракт.

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

Сначала сравните подпись **работающего пути** с доступным сертификатом и
сохранённым TCC requirement. Не удаляйте identity для устранения сбоя вставки:
новый сертификат с тем же именем не будет соответствовать старому разрешению.
В sandbox `security find-identity` может показать ноль сертификатов, а
`codesign --verify` — `CSSMERR_TP_NOT_TRUSTED`. Это требует проверки в штатном
контексте Keychain; не является основанием переподписывать приложение ad-hoc.

Проверить, что identity действительно используется при сборке:

```bash
codesign -dv --verbose=4 "Krab Ear.app" 2>&1 | grep -E "Authority|Identifier|Signature|CDHash"
codesign -d -r- "Krab Ear.app"
codesign --verify --deep --strict "Krab Ear.app"
```

Строгая проверка целостности подписи сама по себе не доказывает её соответствие
конкретному grant. Для этого дополнительно проверяется сохранённый requirement:
`codesign --verify --strict -R='...requirement...' "Krab Ear.app"`.
Accessibility находится в системной TCC-базе; пользовательская база может
содержать другие services и не показывать Accessibility.

При подтверждённом certificate/ad-hoc mismatch безопасная подготовка ремонта:
копия текущего `.app` → подпись прежним сертификатом → обе проверки. Установка
копии и relaunch только агента требуют разрешённого lifecycle scope, idle-gate
и rollback. Backend/REST, Keychain и TCC для такой подготовки не меняются.
Реальная диктовка с вставкой проверяется отдельно после применения.

## Ограничения

- **Self-signed ≠ Apple Developer certificate.** Gatekeeper по-прежнему будет
  блокировать сборку при запуске на чужой машине. Identity предназначена
  **только для локальной разработки**.
- **Не подходит для дистрибуции.** Для App Store / Notarization нужен Developer ID.
- **Срок действия сертификата — 10 лет.** Замена истёкшего сертификата —
  отдельная смена identity с проверкой и повторной выдачей применимых разрешений.
- **Машинозависимо.** На каждом Mac нужно запустить скрипт отдельно.

## Dry-run режим

Посмотреть, что именно будет сделано, без реальных изменений:

```bash
./scripts/create_local_signing_identity.command --dry-run
```
