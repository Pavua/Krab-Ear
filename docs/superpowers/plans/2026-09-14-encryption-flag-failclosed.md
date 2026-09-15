# R1 Encryption Flag Fail-Closed Implementation Plan

**Goal:** `_read_encryption_flag_unlocked` при сбое чтения settings больше не
отдаёт тихий `False` (plaintext-запись истории), а громко уведомляет через
error_bus и считает шифрование включённым.

**Architecture:** Два таска в `KrabEar/backend/state_store.py`. T1: except-ветка
флага → `_push_error("history.encrypt_fail", …)` + `return True` (файл
отсутствует → `False` как раньше; валидный dict → флаг). T2 (sibling-sweep):
`_get_history_crypto` — флаг True, но `build_history_crypto()` вернул None
(Keychain недоступен) → `_push_error` (сейчас тихо None → plaintext).
Прецедент fail-closed в том же файле: `call_privacy_mode()` (raise на битых
настройках). Реестр кодов не трогаем (переиспользуем `history.encrypt_fail`);
статус-репортер не трогаем (наследует assume-enabled). Существующие тесты
`test_history_encryption_migration.py:588-652,689-702` обязаны остаться
зелёными (инжектят крипто напрямую / патчат build → не-None).

**Tech Stack:** unittest + MagicMock error_bus (`store._error_bus = MagicMock()`,
проверка `bus.push.call_args_list[*].args[0].code`), `patch` на
`backend.history_crypto.build_history_crypto` (функция импортирует внутри —
патч модуля работает, как в migration-тестах). StateStore напрямую
(`StateStore(tmp_path)`, `settings_path = data_dir / "settings.json"`) —
`service.close()` не нужен (нет BackendService).

**База:** `origin/codex/krab-ear-v2`. Worktree: эта ветка
(`fix/encryption-flag-failclosed-20260914`, параллельных сессий нет).

**Баны:** вставь список из `docs/EXECUTOR_PLAYBOOK.md` §1. Плюс: не включать
`history_encryption_enabled` в проде; мерж в колею — только после Fable-гейта
диффа (privacy-touching, см. `docs/BACKLOG-PAID-MODELS.md`); `git add` явными
путями.

---

### Task 1: флаг assume-enabled + громкий error

**Files:**
- Modify: `KrabEar/backend/state_store.py` (метод `_read_encryption_flag_unlocked`, ~строка 219)
- Test: `KrabEar/tests/test_encryption_flag_failclosed_2026_09_14.py` (новый)

- [ ] **Step 1: Write the failing test**

```python
"""R1: флаг шифрования fail-closed (NOW.md 14.09).

Битый settings.json больше не означает «шифрование выключено»:
assume-enabled + громкий history.encrypt_fail в error_bus.
Отсутствующий файл — свежий профиль, остаётся False.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_store(data_dir: Path):
    from backend.state_store import StateStore
    return StateStore(data_dir)


class EncryptionFlagFailClosedTest(unittest.TestCase):
    def _bus_codes(self, bus):
        return [c.args[0].code for c in bus.push.call_args_list]

    def test_corrupt_settings_assumes_enabled_and_shouts(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "settings.json").write_text("{corrupt", encoding="utf-8")
            store = _make_store(data_dir)
            bus = MagicMock()
            store._error_bus = bus
            self.assertTrue(store._read_encryption_flag_unlocked())
            self.assertIn("history.encrypt_fail", self._bus_codes(bus))

    def test_missing_settings_stays_off(self):
        with tempfile.TemporaryDirectory() as td:
            store = _make_store(Path(td))
            bus = MagicMock()
            store._error_bus = bus
            self.assertFalse(store._read_encryption_flag_unlocked())
            self.assertNotIn("history.encrypt_fail", self._bus_codes(bus))

    def test_flag_on_but_no_keychain_shouts(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            (data_dir / "settings.json").write_text(
                json.dumps({"history_encryption_enabled": True}), encoding="utf-8")
            store = _make_store(data_dir)
            bus = MagicMock()
            store._error_bus = bus
            with patch("backend.history_crypto.build_history_crypto",
                       return_value=None):
                self.assertIsNone(store._get_history_crypto())
            self.assertIn("history.encrypt_fail", self._bus_codes(bus))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_encryption_flag_failclosed_2026_09_14.py -v`
Expected: FAIL — `test_corrupt_settings_assumes_enabled_and_shouts`
(`False is not true`) и `test_flag_on_but_no_keychain_shouts`
(`'history.encrypt_fail' not found in []`); `test_missing_settings_stays_off`
PASS (сторож fresh-install пути).

- [ ] **Step 3: Write minimal implementation**

```python
    def _read_encryption_flag_unlocked(self) -> bool:
        """Читает флаг history_encryption_enabled из settings.json без захвата lock.

        Используется только из _get_history_crypto() (вызывается под lock).
        Безопасно: settings.json пишется атомарно через tmp + replace,
        поэтому неполные записи не встречаются.

        Fail-closed (R1, 2026-09-14): сбой чтения НЕ означает «шифрование
        выключено» — иначе битый settings тихо уводит историю в plaintext.
        Отсутствующий файл — свежий профиль (False); ошибка чтения —
        assume-enabled (True) + громкий history.encrypt_fail.
        """
        try:
            if not self.settings_path.exists():
                return False
            payload = safe_json_loads(
                self.settings_path.read_text(encoding="utf-8"),
                default=None,
                context="settings.json (encryption flag check)",
            )
            if isinstance(payload, dict):
                return bool(payload.get("history_encryption_enabled", False))
        except Exception as exc:
            logger.exception("StateStore._read_encryption_flag_unlocked: ошибка чтения")
            self._push_error(
                "history.encrypt_fail",
                f"encryption flag unreadable, assuming enabled: "
                f"{type(exc).__name__}: {exc}",
                severity="error",
            )
            return True
        return False
```

и в `_get_history_crypto`, ветка `if enabled:`:

```python
                if enabled:
                    from backend.history_crypto import build_history_crypto
                    self._history_crypto_instance = build_history_crypto()
                    if self._history_crypto_instance is None:
                        # Флаг включён, но ключ недоступен (Keychain) — молчать
                        # нельзя: иначе тихий plaintext при включённом флаге.
                        logger.error(
                            "StateStore: history_encryption_enabled, но "
                            "build_history_crypto вернул None"
                        )
                        self._push_error(
                            "history.encrypt_fail",
                            "encryption flag on but build_history_crypto "
                            "returned None (keychain unavailable?)",
                            severity="error",
                        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: та же команда pytest
Expected: 3 PASS. Плюс регрессия:
`PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_history_encryption_migration.py -v`
Expected: все PASS (инжект/патчи обходят новые ветки).

- [ ] **Step 5: Gate + commit (без мержа в колею)**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_encryption_flag_failclosed_2026_09_14.py
git add KrabEar/backend/state_store.py KrabEar/tests/test_encryption_flag_failclosed_2026_09_14.py docs/superpowers/plans/2026-09-14-encryption-flag-failclosed.md
git commit -m "fix(privacy): fail-closed флаг шифрования истории (R1)"
```

Мерж в `origin/codex/krab-ear-v2` — только после Fable-гейта
(`docs/BACKLOG-PAID-MODELS.md`).
