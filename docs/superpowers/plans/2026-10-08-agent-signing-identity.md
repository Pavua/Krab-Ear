# Исправление локальной подписи агента — 08.10.2026

**Цель:** локальные пути установки не заменяют стабильную подпись ad-hoc и не
продолжают установку после ошибки подписи. Доступы TCC и сертификаты не меняются.

**База:** `origin/codex/krab-ear-v2` @ `efecb801`. Изолированная ветка:
`codex/ear-paste-signing-20261008`.

**Подтверждённый дефект:** живой агент вернул `AX=false` и
`accessibility_not_granted` при вставке в Codex. Сохранённый Accessibility grant
требует `com.antigravity.krab-ear` и существующий сертификат `Krab Ear Dev Local`;
текущий `.app` подписан ad-hoc и не соответствует этому requirement. `make sign`
и `make app` безусловно используют ad-hoc; соседние пути могут перейти на него
при отсутствии identity или скрыть ошибку `codesign`.

## Границы

- Не менять shared checkout, runtime, TCC, Keychain, backend/REST, Main или Gateway.
- Не запускать `make sign`, update/deploy/repair scripts на живом checkout.
- Не пересобирать Swift/ML: код приложения не меняется.
- Не создавать/удалять сертификаты и не сбрасывать разрешения.
- Не менять дистрибуционный `assemble_signed_app.sh --identity`.
- Тестовые команды выполняют shell-пути в временном fixture с fake tools.

## Реализация

1. В `KrabEar/tests/test_agent_signing_identity.py` воспроизвести поведение
   настоящих `make sign`/`make app`: при доступном сертификате обе подписи должны
   использовать его fingerprint; при отсутствии/ошибке чтения/неоднозначности
   сертификата установка должна завершиться до копирования. На старом Makefile
   эти проверки должны падать из-за `-s -` и отсутствия preflight.
2. Добавить один Bash 3.2-совместимый signing helper: точное имя существующего
   сертификата, однозначный fingerprint, nonzero при любой ошибке. Новый
   сертификат с тем же именем не является восстановлением прежней identity.
3. Подключить helper до мутаций в Makefile, `build_and_deploy.command`,
   `update_agent.command`, `verify_binaries.command --fix` и
   `check_two_binary_drift.sh --fix`. Ошибки подписи не скрывать; проверять
   подпись до запуска. `make app` не должен повторно подписывать ad-hoc.
4. Проверить behavioral GREEN, shell syntax и Python 3.12 parity. Независимый
   adversarial review всего diff — отдельный gate.
5. В `docs/DEV_CODESIGN.md` описать certificate-based identity, различие
   CDHash/identity, остановку при недоступном Keychain и безопасную диагностику.

Связанные воспроизводимые shell-ловушки в изменяемых путях закрываются тем же
набором проверок: `local path` в zsh не должен менять специальный `PATH`;
`make -n app` не должен выполнять signing recipe; report-only
`verify_binaries.command` при более свежей `.build` не должен выполнять
`make sign` из текста сообщения через command substitution.

## Отдельная проверка live repair

Подготовленная копия текущего `.app` может быть подписана тем же существующим
сертификатом без сборки. Перед предложением установки обязательны
`codesign --verify --deep --strict` и проверка сохранённого Accessibility
requirement через `codesign --verify --strict -R=...` в штатном контексте доверия
macOS. Это доказывает соответствие подписи, но не живую вставку.

Swap/relaunch только агента и E2E-диктовка — после явного разрешения владельца.
Перед swap повторно проверить неизменность исходного bundle, отсутствие активной
записи/встречи и конкурирующего установщика, подготовить rollback и согласовать
resource window. Живой `ai.krab.ear.agent` имеет `KeepAlive=true`: выполнять
agent-only `bootout` → подтвердить исчезновение job/PID → заменить проверенной
копией → `bootstrap`. Обычное закрытие + `open` может породить гонку respawn.
Зафиксировать backend/REST PID до и после; проверить passive BackendSupervisor,
чтобы завершение Swift-агента не остановило backend. При откате idle-gate повторить.

## DoD

- RED → GREEN с правильной причиной; проверены missing/ambiguous/error cases.
- Проверки подписи копии приложения прошли в штатном контексте macOS.
- Независимый review PASS; локальные тесты и exact-SHA PR CI указаны раздельно.
- Живые binaries, настройки и процессы не изменены до owner approval.
- Успех диктовки заявляется только после её фактического live E2E.

## Выполнение 08.10

- Source `6f690270902a821211a01427080ef902ca64c94a`: 13 behavioral tests PASS
  на Python 3.12/3.14, независимое whole-diff Astra Ultra review PASS. Стандартный
  Ubuntu harness не создан; прямой Python 3.12 не считается Ubuntu CI.
- Draft PR: https://github.com/Pavua/Krab-Ear/pull/2080. GitHub CI и merge
  учитываются отдельно от исправления текущей подписи.
- Владелец явно разрешил agent-only repair. В 03:39 применён прежний бинарник
  с подписью существующего сертификата: новый агент PID 2800, AX trusted=true,
  passive supervision; backend 3957 / REST 3924 не перезапускались. IPC ping и
  HTTP health PASS. TCC/Keychain/settings не изменялись, rollback сохранён.
- В 03:40 владелец подтвердил автоматическую вставку продиктованного текста
  в поле сообщения Codex без ручного Cmd+V. Другие UI/voice маршруты не заявлены.
