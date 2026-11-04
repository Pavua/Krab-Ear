# Maintenance Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans task-by-task.

**Goal:** Создать изолированное ядро durable admission state без права выдавать production drain.
**Architecture:** Один сериализующий owner и SQLite journal; короткие BEGIN IMMEDIATE транзакции. Неопределённые обязательства сохраняются, initial и recovery state CLOSED. Transport/auth/wiring, handoff и положительный production resume вне этого этапа.
**Tech Stack:** Python 3.12/3.14 stdlib sqlite3, threading, subprocess, unittest.
**Spec:** `docs/superpowers/specs/2026-11-04-admission-drain-design.md`.

## Global Constraints

- Никаких ML, сетевых запросов, production файлов, daemon/launchd или runtime.
- Каждый status явно `drain_authority=false`; этот пакет не является fence.
- Явное создание начинается CLOSED. Open-existing не создаёт/не чинит store.
- Missing/corrupt/wrong installation/schema и неизвестный owner — UNKNOWN/HOLD.
- При restart controller обязан оставаться CLOSED, outstanding grants сохраняются.
- Нет TTL-release и нет разрешающего public resume в этом этапе.
- Настоящая transport authentication и применение к clients — будущий этап; synthetic principal не является доказанной identity.

## Review Focus

1. Lost grant ACK и duplicate request key не создают второй grant.
2. begin и acquire сериализуются, нет запуска работы после успешного закрытия.
3. Crash/reopen и reset epoch не стирают неизвестные grants.
4. Чужая/stale session и terminal не меняют чужую операцию.
5. Частичная/повреждённая база и ошибка commit не превращаются в OPEN.

## Срез 1 — состояние и durable store

**Files:** create `KrabEar/core/maintenance_foundation.py`; create `KrabEar/tests/test_maintenance_foundation.py`.

**Proposed interface:** `initialize_new(path, installation_id)`, `open_existing(path, expected_installation_id)`, `activate_controller(expected_revision)`, `register_owner(role, owner_epoch, protocol)`, `acquire_root(session, kind, request_key)`, `claim_start(grant, session)`, `report_terminal(grant, session, outcome)`, `begin_maintenance(admin, expected_revision, request_key)`, `status()`. Полные типы/results и test-only OPEN fixture фиксируются до реализации; admin/session synthetic и НЕ transport auth.

- [ ] Закрепить exact types, schema, безопасную fixture OPEN и статус ошибок после independent review; не добавлять public production OPEN.
- [ ] Написать причинные тесты: явное создание CLOSED, reopen без autocreate, wrong ID/schema/corruption, crash recovery, duplicate/lost ACK, stale owner, чужой terminal, claim-once, конфликт revision, timeout/commit error.
- [ ] Запустить RED на отсутствующем API; отличать отсутствие feature от воспроизведённого runtime бага.
- [ ] Реализовать минимальный stdlib store; SQLite DELETE journal + synchronous EXTRA проверяются, transactions bounded, никаких ожиданий worker внутри транзакции.
- [ ] GREEN на Python 3.12 и 3.14, реальные отдельные процессы для begin/acquire, искусственные crash points и temp dirs; полный cleanup своих процессов.
- [ ] Проверить diff, lint и независимое adversarial review; повторно оценить все остающиеся UNKNOWN.

## Последующие самостоятельные срезы — ещё не implementation-ready

1. Reserve/claim/seal межпроцессного subtree, включая REST→RU IPC handoff и потерянный ACK.
2. Действительная transport auth, fencing owner incarnations и recovery без ложного OPEN.
3. IPC/REST/internal owners, existing WS frames и lifetime реальных Futures/worker trees.
4. Main/Gateway client admission до primary/fallback/provider side effects, согласованный wire contract.
5. Полные integration/E2E и exact CI; затем отдельно first-install/rollback старых процессов.

Ни один отдельный зелёный срез не снимает общий live HOLD. План требует
уточнения точной модели открытого состояния и identity до реализации.

## Черновой контракт следующего среза — на отдельное review

Это ещё не утверждённый API и не разрешение подключать его к runtime.

- Персистентный экземпляр store принадлежит единственному control owner под
  lifetime-lock. Межпроцессная конкуренция клиентов должна проходить через
  этот owner, а не через прямое параллельное открытие SQLite другими процессами.
  Транспорт, удостоверение peer и liveness owner пока не реализованы.
- Для моделирования begin/acquire в изоляции нужен отдельно обозначенный
  synthetic OPEN seed в тестовом fixture. Публичная операция OPEN и production
  resume в этом срезе отсутствуют; неверный/неполный seed не даёт допуска.
- Каждый root grant должен атомарно и устойчиво записываться до выдачи ACK;
  повтор того же request key с теми же параметрами возвращает то же решение,
  несовпадающие параметры являются конфликтом. Неизвестный результат записи
  не даёт положительного ACK. Механизм дедупликации и заявка составляют одну
  транзакцию; отдельное сохранение результата недопустимо.
- Begin и acquire должны иметь один сериализующий порядок: после commit begin
  новый grant не создаётся, а ранее выданный продолжает удерживать долг до
  проверенного terminal. Request timeout, потеря ACK, restart owner или смерть
  caller сами по себе не являются terminal. Нельзя очищать долг по TTL.
- После crash или reopen новые grants запрещены; persisted outstanding записи
  сохраняются как долг/UNKNOWN. Одной записи от клиента недостаточно для
  заключения о завершении subprocess tree. Пока не определены транспортная
  аутентификация, fencing инкарнаций и reconciliation, любой status сохраняет
  `drain_authority=false`, в том числе при нуле записей.
- Отдельное ревью обязано определить границы idempotency key/session/epoch,
  точные типы отказов и commit ambiguity, правила claimant/terminal, а также
  crash-test контроль сохранности ACK и отсутствие повторного запуска работы.
  База checkpoint и тестовая fixture не должны обходить эти инварианты в коде,
  который позже можно включить в production.
