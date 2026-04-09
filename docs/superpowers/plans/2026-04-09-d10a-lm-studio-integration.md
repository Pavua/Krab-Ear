# D.10a LM Studio Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Интегрировать локальную LLM через LM Studio (OpenAI-compatible API) как sync pre-paste hook, исправляющий пунктуацию/орфографию/грамматику после D.7 cleanup. Падения LLM не должны ломать диктовку — fallback на raw text, circuit breaker устраняет log spam.

**Architecture:** Новый модуль `backend/llm_rewriter.py` содержит `LLMRewriter` класс (sync HTTP-клиент через `requests`) и `CircuitBreaker` (3-state machine с exponential backoff). `AudioEngine.transcribe()` вызывает rewriter условно после `TextUtils.cleanup_transcript`, `BackendService` инжектирует экземпляр в `Transcriber` → `AudioEngine`. Hybrid toggle: `settings.LLM_ENABLED` (env admin) AND `DEFAULT_SETTINGS.llm_rewrite_enabled` (runtime user) через IPC `update_settings` без рестарта.

**Tech Stack:** Python 3.9+ (Xcode), `requests` (sync HTTP), `pydantic-settings` (`.secrets` loading via env_file tuple), `unittest.mock.patch` для HTTP моков, LM Studio локально на порту 1234 с моделью `qwen3.5-9b@6bit`.

**Spec:** `docs/superpowers/specs/2026-04-09-d10a-lm-studio-integration-design.md`

---

## File Structure

### New files
- `KrabEar/backend/llm_rewriter.py` — `LLMRewriter` + `CircuitBreaker` + `LLMRewriteResult` (~250 строк)
- `KrabEar/tests/test_llm_rewriter.py` — unit tests (~300 строк, 25+ тестов)
- `KrabEar/tests/test_engine_llm_integration.py` — integration tests (~120 строк, 5 тестов)

### Modified files
- `KrabEar/core/config.py` — `.secrets` loading fix + новые LLM Settings поля + `DEFAULT_SETTINGS["llm_rewrite_enabled"]`
- `KrabEar/core/engine.py` — `AudioEngine.__init__` signature + `transcribe()` hook + return dict extension
- `KrabEar/backend/transcriber.py` — pass-through `llm_rewriter` и `settings_get` в `AudioEngine`
- `KrabEar/backend/service.py` — `BackendService.__init__` инициализирует LLMRewriter, новый `_handle_llm_status`, dispatch регистрация
- `KrabEar/backend/models.py` — `HistoryItem` получает `cleaned_text`, `llm_applied`, `llm_latency_ms` поля
- `KrabEar/tests/test_backend_service.py` — 3 новых теста для `llm_status` и runtime toggle

### Config files (outside repo)
- `~/Library/Application Support/KrabEar/.secrets` — обновление `KRAB_EAR_LLM_MODEL`, добавление `KRAB_EAR_LLM_ENABLED=true`

### Tests run command (project-wide)
```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest discover -s KrabEar/tests -v
```

Worktree использует Python 3.9 из Xcode, поэтому `unittest` а не `pytest`.

---

## Task 1: Config foundation — `.secrets` loading + LLM Settings + DEFAULT_SETTINGS toggle

**Files:**
- Modify: `KrabEar/core/config.py`
- Test: `KrabEar/tests/test_config_llm.py` (new)

**Why first:** всё остальное импортирует `settings` — фундамент. Без `.secrets` loading fix LLM env vars мертвы.

- [ ] **Step 1.1: Write failing test for .secrets file loading**

Create `KrabEar/tests/test_config_llm.py`:

```python
"""Тесты для LLM настроек в core/config.py."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch


class ConfigSecretsLoadingTestCase(unittest.TestCase):
    """Проверяет что .secrets файл правильно подхватывается pydantic-settings."""

    def test_secrets_file_path_is_absolute_and_correct(self):
        """config._SECRETS_FILE должен указывать на ~/Library/Application Support/KrabEar/.secrets."""
        from core.config import _SECRETS_FILE
        expected = Path.home() / "Library" / "Application Support" / "KrabEar" / ".secrets"
        self.assertEqual(_SECRETS_FILE, expected)

    def test_env_file_tuple_contains_secrets_and_dotenv(self):
        """model_config.env_file должен быть tuple из .secrets + .env."""
        from core.config import Settings, _SECRETS_FILE
        env_file = Settings.model_config.get("env_file")
        self.assertIsInstance(env_file, tuple)
        self.assertIn(str(_SECRETS_FILE), env_file)
        self.assertIn(".env", env_file)


class ConfigLLMFieldsTestCase(unittest.TestCase):
    """Проверяет что новые LLM поля существуют с правильными дефолтами."""

    def test_llm_enabled_default_false(self):
        from core.config import Settings
        s = Settings()
        self.assertFalse(s.LLM_ENABLED)

    def test_llm_base_url_default(self):
        from core.config import Settings
        s = Settings()
        self.assertEqual(s.LLM_BASE_URL, "http://localhost:1234/v1")

    def test_llm_model_default(self):
        from core.config import Settings
        s = Settings()
        self.assertEqual(s.LLM_MODEL, "qwen3.5-9b@6bit")

    def test_llm_timeout_sec_default(self):
        from core.config import Settings
        s = Settings()
        self.assertEqual(s.LLM_TIMEOUT_SEC, 4.0)

    def test_llm_circuit_fail_threshold_default(self):
        from core.config import Settings
        s = Settings()
        self.assertEqual(s.LLM_CIRCUIT_FAIL_THRESHOLD, 3)

    def test_llm_circuit_initial_reset_sec_default(self):
        from core.config import Settings
        s = Settings()
        self.assertEqual(s.LLM_CIRCUIT_INITIAL_RESET_SEC, 60)

    def test_llm_circuit_max_reset_sec_default(self):
        from core.config import Settings
        s = Settings()
        self.assertEqual(s.LLM_CIRCUIT_MAX_RESET_SEC, 600)

    def test_env_var_override(self):
        """KRAB_EAR_LLM_ENABLED=true переопределяет дефолт."""
        with patch.dict(os.environ, {"KRAB_EAR_LLM_ENABLED": "true"}):
            from core.config import Settings
            s = Settings()
            self.assertTrue(s.LLM_ENABLED)


class DefaultSettingsLLMToggleTestCase(unittest.TestCase):
    """Проверяет что llm_rewrite_enabled добавлен в DEFAULT_SETTINGS."""

    def test_llm_rewrite_enabled_in_default_settings(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn("llm_rewrite_enabled", DEFAULT_SETTINGS)

    def test_llm_rewrite_enabled_default_false(self):
        from core.config import DEFAULT_SETTINGS
        self.assertFalse(DEFAULT_SETTINGS["llm_rewrite_enabled"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/.claude/worktrees/ecstatic-moser"
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_config_llm -v
```

Expected: **FAIL** с ошибками типа `AttributeError: type object 'Settings' has no attribute 'LLM_ENABLED'` и `ImportError: cannot import name '_SECRETS_FILE'`.

- [ ] **Step 1.3: Implement config.py changes**

Modify `KrabEar/core/config.py`:

```python
"""Централизованная конфигурация Krab Ear на базе Pydantic-Settings.

Все параметры могут быть переопределены через переменные окружения (.env
или ~/Library/Application Support/KrabEar/.secrets).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import List, Any

# Абсолютный путь к .secrets — backend загружает его на старте через
# pydantic-settings env_file tuple. Env vars из launchd plist всё равно
# имеют более высокий приоритет (env > env_file).
_SECRETS_FILE = Path.home() / "Library" / "Application Support" / "KrabEar" / ".secrets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KRAB_EAR_",
        env_file=(str(_SECRETS_FILE), ".env"),
        extra="ignore",
    )

    # Директории
    DATA_DIR: Path = Path.home() / ".krab_ear_data"

    # Модели STT
    MODEL_BALANCED: str = "mlx-community/whisper-large-v3-turbo"
    MODEL_MAX_CANDIDATES: str = "mlx-community/whisper-large-v3-mlx,mlx-community/whisper-large-v3-turbo"

    # Промпты и язык
    TRANSCRIBE_PROMPT: str = "Ты транскрибируешь русскую речь. Сохраняй смысл, ставь корректную пунктуацию и заглавные буквы."
    TRANSCRIBE_LANGUAGE: str = "ru"
    HF_TOKEN: str = ""
    DIARIZATION_ENABLED: bool = True
    DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"

    # Сетевые настройки
    NETWORK_MODE: str = "offline_default"
    GATEWAY_URL: str = "http://127.0.0.1:18789/v1/chat/completions"
    STT_GATEWAY_URL: str = "http://127.0.0.1:18789/v1/audio/transcriptions"
    AI_MODEL: str = "google/gemini-2.0-flash"
    STT_MODEL: str = "whisper-1"

    # Лимиты
    MAX_AUDIO_MB: int = 50
    MAX_DURATION_SEC: int = 300
    TRANSCRIBE_TIMEOUT_SEC: int = 60

    # TTS
    SAY_VOICE: str = ""

    # Voice Gateway
    VOICE_GATEWAY_URL: str = "http://127.0.0.1:8090"

    # D.10a LM Studio integration (OpenAI-compatible LLM rewriter)
    LLM_ENABLED: bool = False
    LLM_BASE_URL: str = "http://localhost:1234/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "qwen3.5-9b@6bit"
    LLM_TIMEOUT_SEC: float = 4.0
    LLM_CIRCUIT_FAIL_THRESHOLD: int = 3
    LLM_CIRCUIT_INITIAL_RESET_SEC: int = 60
    LLM_CIRCUIT_MAX_RESET_SEC: int = 600

    @property
    def model_max_list(self) -> List[str]:
        """Возвращает список кандидатов для max-профиля."""
        parts = [p.strip() for p in self.MODEL_MAX_CANDIDATES.split(",") if p.strip()]
        if self.MODEL_BALANCED not in parts:
            parts.append(self.MODEL_BALANCED)
        return parts


# Singleton инстанс настроек
settings = Settings()

# Дефолтные настройки для UI и логики (из legacy моделей)
DEFAULT_SETTINGS: dict[str, Any] = {
    "mode": "headless",
    "show_dock_icon": True,
    "auto_start_enabled": False,
    "auto_paste": True,
    "play_start_sound": True,
    "quality_profile": "balanced",
    "network_mode": "offline_default",
    "hotkey": "right_option_toggle",
    "hotkey_profile": "default",
    "history_policy": "unlimited",
    "history_page_size": 50,
    "history_text_density": "normal",
    "realtime_preview_enabled": True,
    "cleanup_profile": "soft",
    "translation_mode": "off",
    "translate_and_paste": False,
    "translation_style": "neutral",
    "translation_glossary": {},
    "text_templates": {
        "follow_up_ru": "Здравствуйте! Подтверждаю: {text}. Следующий шаг: {next_step}.",
        "follow_up_es": "Hola. Confirmo: {text}. Siguiente paso: {next_step}.",
    },
    "clipboard_mode": "always_copy",
    "audio_ducking_enabled": True,
    "audio_ducking_percent": 50,
    "stop_tail_trim_ms": 180,
    "silence_guard_enabled": True,
    "silence_guard_rms_threshold": 0.0020,
    "silence_guard_peak_threshold": 0.0120,
    "silence_guard_active_ratio_threshold": 0.015,
    "background_guard_enabled": True,
    "background_guard_min_peak": 0.025,
    "background_guard_min_rms": 0.0040,
    "background_guard_uniform_frame_threshold": 0.0060,
    "background_guard_max_uniform_active_ratio": 0.92,
    "overlay_opacity_percent": 45,
    "voice_gateway_url": "http://127.0.0.1:8090",
    "voice_gateway_api_key": "",
    "update_channel": "stable",
    "call_notify_default": True,
    "call_auto_summary": True,
    "call_budget_usd": 2.0,
    "call_quick_templates": [
        {
            "name": "Повтори медленно",
            "text": "Повторите, пожалуйста, медленнее.",
            "source_lang": "ru",
            "target_lang": "es",
        },
        {
            "name": "Жду ответ",
            "text": "Буду ждать вашего ответа до конца дня.",
            "source_lang": "ru",
            "target_lang": "ru",
        },
    ],
    "capture_source_mode": "mic",
    "ui_last_tab": "history",
    "history_focus_mode": True,
    "onboarding_completed": False,
    # D.10a runtime toggle: юзер может включать/выключать LLM rewriter через
    # IPC update_settings без рестарта. Дефолт False — safety.
    "llm_rewrite_enabled": False,
}
```

- [ ] **Step 1.4: Run test to verify it passes**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_config_llm -v
```

Expected: **OK** (10 tests passed).

- [ ] **Step 1.5: Run full suite to verify no regression**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest discover -s KrabEar/tests -v 2>&1 | tail -20
```

Expected: все существующие тесты по-прежнему проходят.

- [ ] **Step 1.6: Commit**

```bash
git add KrabEar/core/config.py KrabEar/tests/test_config_llm.py
git commit -m "feat(ear): D.10a config foundation — .secrets loading + LLM settings

- core/config.py: env_file tuple for absolute .secrets path
- Settings: add LLM_* fields (enabled, base_url, api_key, model, timeout, circuit)
- DEFAULT_SETTINGS: add llm_rewrite_enabled runtime toggle
- tests/test_config_llm.py: 10 tests covering all new fields and .secrets loading"
```

---

## Task 2: CircuitBreaker — TDD state machine с exponential backoff

**Files:**
- Create: `KrabEar/backend/llm_rewriter.py` (skeleton with just CircuitBreaker)
- Test: `KrabEar/tests/test_llm_rewriter.py`

- [ ] **Step 2.1: Create test file with CircuitBreaker tests**

Create `KrabEar/tests/test_llm_rewriter.py`:

```python
"""Unit tests для backend/llm_rewriter.py — CircuitBreaker + LLMRewriter."""

import unittest
from unittest.mock import patch, MagicMock


class CircuitBreakerTestCase(unittest.TestCase):
    """Тесты state machine: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    def setUp(self):
        from backend.llm_rewriter import CircuitBreaker
        self.breaker = CircuitBreaker(fail_threshold=3, initial_reset_sec=60, max_reset_sec=600)

    def test_initial_state_closed(self):
        self.assertEqual(self.breaker.state, "closed")

    def test_closed_allows_requests(self):
        self.assertTrue(self.breaker.allow_request())

    def test_one_failure_stays_closed(self):
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.allow_request())

    def test_two_failures_stays_closed(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "closed")

    def test_three_consecutive_failures_opens(self):
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

    def test_success_resets_failure_counter(self):
        """fail, fail, success, fail, fail — circuit должен остаться CLOSED."""
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_success()
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "closed")

    def test_open_blocks_requests_immediately_after_open(self):
        for _ in range(3):
            self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")
        self.assertFalse(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_open_transitions_to_half_open_after_cooldown(self, mock_monotonic):
        """После reset_sec allow_request() переходит в HALF_OPEN и возвращает True."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1059.0
        self.assertFalse(self.breaker.allow_request())
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1061.0
        self.assertTrue(self.breaker.allow_request())
        self.assertEqual(self.breaker.state, "half_open")

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_blocks_second_request(self, mock_monotonic):
        """В HALF_OPEN только первый request проходит, остальные False."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()
        mock_monotonic.return_value = 1061.0
        self.assertTrue(self.breaker.allow_request())
        self.assertFalse(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_success_transitions_to_closed(self, mock_monotonic):
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()
        mock_monotonic.return_value = 1061.0
        self.breaker.allow_request()
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, "closed")
        self.assertTrue(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_half_open_success_resets_backoff(self, mock_monotonic):
        """После HALF_OPEN → CLOSED → новое открытие должно иметь initial_reset_sec cooldown."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()

        mock_monotonic.return_value = 1061.0
        self.breaker.allow_request()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1061.0 + 119.0
        self.assertFalse(self.breaker.allow_request())
        mock_monotonic.return_value = 1061.0 + 121.0
        self.assertTrue(self.breaker.allow_request())
        self.breaker.record_success()

        mock_monotonic.return_value = 2000.0
        for _ in range(3):
            self.breaker.record_failure()

        mock_monotonic.return_value = 2000.0 + 59.0
        self.assertFalse(self.breaker.allow_request())
        mock_monotonic.return_value = 2000.0 + 61.0
        self.assertTrue(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_exponential_backoff_doubles_on_probe_failure(self, mock_monotonic):
        """HALF_OPEN fail удваивает cooldown (60 → 120)."""
        mock_monotonic.return_value = 1000.0
        for _ in range(3):
            self.breaker.record_failure()

        mock_monotonic.return_value = 1061.0
        self.assertTrue(self.breaker.allow_request())
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, "open")

        mock_monotonic.return_value = 1061.0 + 119.0
        self.assertFalse(self.breaker.allow_request())
        mock_monotonic.return_value = 1061.0 + 121.0
        self.assertTrue(self.breaker.allow_request())

    @patch("backend.llm_rewriter.time.monotonic")
    def test_backoff_caps_at_max_reset_sec(self, mock_monotonic):
        """После многих неудачных проб cooldown не превышает max_reset_sec."""
        breaker = __import__("backend.llm_rewriter", fromlist=["CircuitBreaker"]).CircuitBreaker(
            fail_threshold=1, initial_reset_sec=60, max_reset_sec=300
        )
        t = 1000.0
        mock_monotonic.return_value = t
        breaker.record_failure()
        self.assertEqual(breaker.state, "open")

        for _ in range(10):
            t += 1000.0
            mock_monotonic.return_value = t
            self.assertTrue(breaker.allow_request())
            breaker.record_failure()

        t_open = t
        mock_monotonic.return_value = t_open + 299.0
        self.assertFalse(breaker.allow_request())
        mock_monotonic.return_value = t_open + 301.0
        self.assertTrue(breaker.allow_request())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2.2: Run tests to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v
```

Expected: **FAIL** с `ModuleNotFoundError: No module named 'backend.llm_rewriter'`.

- [ ] **Step 2.3: Create `llm_rewriter.py` with CircuitBreaker only**

Create `KrabEar/backend/llm_rewriter.py`:

```python
"""LLM rewriter для Krab Ear — пост-процессинг транскрипта через локальный LM Studio.

Модуль содержит:
- CircuitBreaker: state machine (CLOSED → OPEN → HALF_OPEN) с exponential backoff
- LLMRewriteResult: dataclass-результат попытки rewrite'а
- LLMRewriter: HTTP-клиент к OpenAI-compatible endpoint'у

Контракт LLMRewriter.rewrite(): НИКОГДА не raises, всегда возвращает LLMRewriteResult.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("KrabEar.Backend.LLMRewriter")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """3-state circuit breaker с exponential backoff.

    Thread safety: не требуется — IPC server в Krab Ear однопоточный.
    Если появится multi-threaded access, обернуть в threading.Lock.
    """

    def __init__(
        self,
        fail_threshold: int,
        initial_reset_sec: int,
        max_reset_sec: int = 600,
    ):
        self._fail_threshold = fail_threshold
        self._initial_reset_sec = initial_reset_sec
        self._max_reset_sec = max_reset_sec
        self._current_reset_sec = initial_reset_sec
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._half_open_probe_in_flight = False

    @property
    def state(self) -> str:
        """Публичное имя состояния ('closed' | 'open' | 'half_open')."""
        return self._state.value

    def allow_request(self) -> bool:
        """Можно ли сейчас делать HTTP запрос?"""
        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            if self._opened_at is None:
                return True
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._current_reset_sec:
                self._transition_to(CircuitState.HALF_OPEN)
                self._half_open_probe_in_flight = True
                return True
            return False

        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_probe_in_flight:
                return False
            self._half_open_probe_in_flight = True
            return True

        return False

    def record_success(self):
        self._half_open_probe_in_flight = False
        if self._state == CircuitState.HALF_OPEN:
            logger.info("Circuit breaker: HALF_OPEN -> CLOSED (проба успешна)")
            self._transition_to(CircuitState.CLOSED)
        self._consecutive_failures = 0

    def record_failure(self):
        self._half_open_probe_in_flight = False
        self._consecutive_failures += 1

        if self._state == CircuitState.HALF_OPEN:
            self._current_reset_sec = min(self._current_reset_sec * 2, self._max_reset_sec)
            logger.warning(
                "Circuit breaker: HALF_OPEN -> OPEN (проба провалилась), cooldown теперь %d сек",
                self._current_reset_sec,
            )
            self._transition_to(CircuitState.OPEN)
            return

        if (
            self._state == CircuitState.CLOSED
            and self._consecutive_failures >= self._fail_threshold
        ):
            logger.warning(
                "Circuit breaker: CLOSED -> OPEN (%d fails подряд), cooldown %d сек",
                self._consecutive_failures,
                self._current_reset_sec,
            )
            self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState):
        self._state = new_state
        if new_state == CircuitState.OPEN:
            self._opened_at = time.monotonic()
            self._consecutive_failures = 0
        elif new_state == CircuitState.CLOSED:
            self._opened_at = None
            self._consecutive_failures = 0
            self._current_reset_sec = self._initial_reset_sec
            self._half_open_probe_in_flight = False
```

- [ ] **Step 2.4: Run tests to verify all pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v
```

Expected: **OK** (13 tests passed).

- [ ] **Step 2.5: Commit**

```bash
git add KrabEar/backend/llm_rewriter.py KrabEar/tests/test_llm_rewriter.py
git commit -m "feat(ear): D.10a CircuitBreaker state machine with exponential backoff

- backend/llm_rewriter.py: CircuitBreaker 3-state (CLOSED/OPEN/HALF_OPEN)
- Exponential backoff on HALF_OPEN probe failure (60 -> 120 -> ... -> 600)
- Reset backoff on successful HALF_OPEN -> CLOSED transition
- time.monotonic() for NTP-safe cooldown measurement
- Log only on state transitions (eliminates D.11 gateway-refused log spam pattern)
- tests/test_llm_rewriter.py: 13 CircuitBreaker tests with monkeypatch time.monotonic"
```

---

## Task 3: LLMRewriter — Result class + skeleton + postprocess + max_tokens estimator

**Files:**
- Modify: `KrabEar/backend/llm_rewriter.py`
- Modify: `KrabEar/tests/test_llm_rewriter.py`

- [ ] **Step 3.1: Add tests for LLMRewriteResult, postprocess, max_tokens**

Append to `KrabEar/tests/test_llm_rewriter.py`:

```python
class LLMRewriteResultTestCase(unittest.TestCase):
    def test_ok_result_returns_text(self):
        from backend.llm_rewriter import LLMRewriteResult
        r = LLMRewriteResult(ok=True, text="clean", fallback_reason=None, latency_ms=100)
        self.assertEqual(r.text_or_fallback("raw"), "clean")

    def test_failed_result_returns_fallback(self):
        from backend.llm_rewriter import LLMRewriteResult
        r = LLMRewriteResult(ok=False, text=None, fallback_reason="timeout", latency_ms=None)
        self.assertEqual(r.text_or_fallback("raw"), "raw")

    def test_ok_but_none_text_returns_fallback(self):
        """Edge case: ok=True но text=None (не должно случаться, но защищаемся)."""
        from backend.llm_rewriter import LLMRewriteResult
        r = LLMRewriteResult(ok=True, text=None, fallback_reason=None, latency_ms=100)
        self.assertEqual(r.text_or_fallback("raw"), "raw")


class LLMRewriterPostprocessTestCase(unittest.TestCase):
    """Тесты приватного _postprocess метода."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_strips_double_quotes(self):
        self.assertEqual(self.rewriter._postprocess('"Привет, мир."'), "Привет, мир.")

    def test_strips_french_quotes(self):
        self.assertEqual(self.rewriter._postprocess("«Привет, мир.»"), "Привет, мир.")

    def test_strips_curly_quotes(self):
        self.assertEqual(self.rewriter._postprocess("\u201cПривет, мир.\u201d"), "Привет, мир.")

    def test_strips_explanatory_prefix_ispravlenny(self):
        self.assertEqual(
            self.rewriter._postprocess("Исправленный текст: Привет, мир."),
            "Привет, мир.",
        )

    def test_strips_explanatory_prefix_ispravleno(self):
        self.assertEqual(
            self.rewriter._postprocess("Исправлено: Привет, мир."),
            "Привет, мир.",
        )

    def test_strips_explanatory_prefix_case_insensitive(self):
        self.assertEqual(
            self.rewriter._postprocess("исправленный текст: Привет, мир."),
            "Привет, мир.",
        )

    def test_takes_first_paragraph_on_double_newline(self):
        self.assertEqual(
            self.rewriter._postprocess("Привет, мир.\n\n**Пояснение**: я убрал запятую."),
            "Привет, мир.",
        )

    def test_empty_string_stays_empty(self):
        self.assertEqual(self.rewriter._postprocess(""), "")

    def test_whitespace_only_stays_empty(self):
        self.assertEqual(self.rewriter._postprocess("   \n  "), "")

    def test_passes_through_normal_text(self):
        self.assertEqual(
            self.rewriter._postprocess("Привет, как дела?"),
            "Привет, как дела?",
        )


class LLMRewriterMaxTokensTestCase(unittest.TestCase):
    """Тесты dynamic max_tokens estimator."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    def test_short_text_hits_floor(self):
        """Короткий текст (5 слов) → max_tokens = 256 (floor)."""
        result = self.rewriter._estimate_max_tokens("Привет как дела мой друг")
        self.assertEqual(result, 256)

    def test_medium_text_scales_linearly(self):
        """100 слов → примерно 100 * 3 * 1.3 + 50 = 440."""
        text = " ".join(["слово"] * 100)
        result = self.rewriter._estimate_max_tokens(text)
        self.assertEqual(result, 440)

    def test_long_text_hits_ceiling(self):
        """2000 слов → max_tokens = 4096 (ceiling)."""
        text = " ".join(["слово"] * 2000)
        result = self.rewriter._estimate_max_tokens(text)
        self.assertEqual(result, 4096)

    def test_empty_text_returns_floor(self):
        result = self.rewriter._estimate_max_tokens("")
        self.assertEqual(result, 256)
```

- [ ] **Step 3.2: Run tests to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v 2>&1 | tail -30
```

Expected: **FAIL** с `ImportError: cannot import name 'LLMRewriter' from 'backend.llm_rewriter'` и `LLMRewriteResult`.

- [ ] **Step 3.3: Implement LLMRewriteResult, LLMRewriter skeleton, _postprocess, _estimate_max_tokens**

Append to `KrabEar/backend/llm_rewriter.py`:

```python


SYSTEM_PROMPT = """Ты — редактор русской диктовки. Твоя задача — исправить пунктуацию, орфографию и грамматику в тексте, сохранив смысл и стиль автора.

Жёсткие правила:
1. НЕ добавляй слов, которых нет в оригинале.
2. НЕ удаляй слов, кроме явных filler'ов в начале ("э-э", "ну", "вот").
3. НЕ меняй порядок слов, кроме случаев когда этого требует грамматика.
4. НЕ переформулируй фразы — только исправляй ошибки.
5. Бренды и технические термины оставляй латиницей: Spotify, YouTube, GitHub, Claude, OpenAI, Docker, Python, Swift, macOS, iPhone, iPad, Mac, Telegram, WhatsApp, Slack, Notion, Figma, VS Code, Xcode, Linux, Linear, Jira.
6. Расставь правильные знаки препинания: запятые, точки, тире, двоеточия.
7. Заглавные буквы в начале предложений и у имён собственных.
8. Если текст пустой или бессмысленный — верни его без изменений.

Верни ТОЛЬКО исправленный текст. Без пояснений. Без кавычек. Без префиксов типа "Исправленный текст:"."""

_QUOTE_OPENERS = ('"', "«", "\u201c")
_QUOTE_CLOSERS = ('"', "»", "\u201d")
_EXPLANATORY_PREFIXES = (
    "Исправленный текст:",
    "Исправлено:",
    "Результат:",
    "Вот:",
)


@dataclass
class LLMRewriteResult:
    """Результат попытки rewrite'а. Всегда возвращается, никогда не raises."""

    ok: bool
    text: Optional[str]
    fallback_reason: Optional[str]
    latency_ms: Optional[int]

    def text_or_fallback(self, fallback: str) -> str:
        """Helper: вернуть rewritten text если ok=True и text непустой, иначе fallback."""
        if self.ok and self.text:
            return self.text
        return fallback


class LLMRewriter:
    """HTTP-клиент к OpenAI-compatible LLM endpoint'у (LM Studio).

    Контракт: rewrite() НИКОГДА не raises. Все ошибки возвращаются как
    LLMRewriteResult(ok=False, fallback_reason=...).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_sec: float = 4.0,
        circuit_fail_threshold: int = 3,
        circuit_initial_reset_sec: int = 60,
        circuit_max_reset_sec: int = 600,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_sec
        self._circuit = CircuitBreaker(
            fail_threshold=circuit_fail_threshold,
            initial_reset_sec=circuit_initial_reset_sec,
            max_reset_sec=circuit_max_reset_sec,
        )
        self._last_latency_ms: Optional[int] = None
        self._last_error: Optional[str] = None

    def _postprocess(self, content: str) -> str:
        """Убирает типичный мусор в ответе LLM (кавычки, префиксы, multi-paragraph)."""
        s = (content or "").strip()
        if not s:
            return ""

        if len(s) >= 2 and s[0] in _QUOTE_OPENERS and s[-1] in _QUOTE_CLOSERS:
            s = s[1:-1].strip()

        for prefix in _EXPLANATORY_PREFIXES:
            if s.lower().startswith(prefix.lower()):
                s = s[len(prefix):].strip()
                break

        if "\n\n" in s:
            s = s.split("\n\n", 1)[0].strip()

        return s

    def _estimate_max_tokens(self, text: str) -> int:
        """Динамический output cap на базе длины input'а.

        Русский ~2.5-3 токена на слово, output ≈ input по длине.
        30% headroom + 50 токенов буфера на знаки препинания.
        """
        word_count = len((text or "").split())
        input_tokens_estimate = word_count * 3
        max_tokens = int(input_tokens_estimate * 1.3) + 50
        return max(256, min(max_tokens, 4096))

    def _build_messages(self, text: str) -> list:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v 2>&1 | tail -30
```

Expected: **OK** (13 CircuitBreaker + 3 Result + 10 Postprocess + 4 MaxTokens = 30 tests passed).

- [ ] **Step 3.5: Commit**

```bash
git add KrabEar/backend/llm_rewriter.py KrabEar/tests/test_llm_rewriter.py
git commit -m "feat(ear): D.10a LLMRewriter skeleton with postprocess and token estimator

- LLMRewriteResult dataclass with text_or_fallback helper
- LLMRewriter class skeleton + system prompt constant with hardcoded brand whitelist
- _postprocess: strip quotes, explanatory prefixes, take first paragraph
- _estimate_max_tokens: dynamic output cap (floor 256, ceiling 4096)
- 17 new tests covering Result, postprocess edge cases, max_tokens scaling"
```

---

## Task 4: LLMRewriter.rewrite() — happy path + failure modes + circuit integration

**Files:**
- Modify: `KrabEar/backend/llm_rewriter.py`
- Modify: `KrabEar/tests/test_llm_rewriter.py`

- [ ] **Step 4.1: Add tests for rewrite() success and failure paths**

Append to `KrabEar/tests/test_llm_rewriter.py`:

```python
class LLMRewriterRewriteSuccessTestCase(unittest.TestCase):
    """Happy path tests для rewrite()."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=4.0,
        )

    def _mock_response(self, content: str, status_code: int = 200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = {
            "choices": [
                {"message": {"content": content}}
            ]
        }
        return mock_resp

    @patch("backend.llm_rewriter.requests.post")
    def test_successful_rewrite_returns_ok_result(self, mock_post):
        mock_post.return_value = self._mock_response("Привет, мир.")
        result = self.rewriter.rewrite("привет мир")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Привет, мир.")
        self.assertIsNone(result.fallback_reason)
        self.assertIsNotNone(result.latency_ms)

    @patch("backend.llm_rewriter.requests.post")
    def test_rewrite_calls_correct_endpoint(self, mock_post):
        mock_post.return_value = self._mock_response("ok")
        self.rewriter.rewrite("test")
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "http://localhost:1234/v1/chat/completions")

    @patch("backend.llm_rewriter.requests.post")
    def test_rewrite_sends_correct_payload(self, mock_post):
        mock_post.return_value = self._mock_response("ok")
        self.rewriter.rewrite("test input")
        kwargs = mock_post.call_args.kwargs
        payload = kwargs["json"]
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["stream"], False)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")
        self.assertEqual(payload["messages"][1]["content"], "test input")

    @patch("backend.llm_rewriter.requests.post")
    def test_rewrite_sends_authorization_header(self, mock_post):
        mock_post.return_value = self._mock_response("ok")
        self.rewriter.rewrite("test")
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-test")

    @patch("backend.llm_rewriter.requests.post")
    def test_rewrite_strips_quotes_from_response(self, mock_post):
        mock_post.return_value = self._mock_response('"Привет, мир."')
        result = self.rewriter.rewrite("привет мир")
        self.assertEqual(result.text, "Привет, мир.")

    @patch("backend.llm_rewriter.requests.post")
    def test_empty_input_returns_empty_input_without_http_call(self, mock_post):
        result = self.rewriter.rewrite("")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        mock_post.assert_not_called()

    @patch("backend.llm_rewriter.requests.post")
    def test_whitespace_only_input_returns_empty_input(self, mock_post):
        result = self.rewriter.rewrite("   \n  ")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")
        mock_post.assert_not_called()


class LLMRewriterRewriteFailuresTestCase(unittest.TestCase):
    """Failure mode tests: timeout, connection, HTTP errors, parse errors."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    @patch("backend.llm_rewriter.requests.post")
    def test_timeout_returns_fallback_and_records_failure(self, mock_post):
        import requests
        mock_post.side_effect = requests.Timeout("timeout")
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "timeout")

    @patch("backend.llm_rewriter.requests.post")
    def test_connection_error_returns_fallback(self, mock_post):
        import requests
        mock_post.side_effect = requests.ConnectionError("refused")
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "connection_error")

    @patch("backend.llm_rewriter.requests.post")
    def test_http_500_returns_fallback(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "http_500")

    @patch("backend.llm_rewriter.requests.post")
    def test_malformed_json_returns_parse_error(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        mock_post.return_value = mock_resp
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "parse_error")

    @patch("backend.llm_rewriter.requests.post")
    def test_missing_choices_returns_parse_error(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"error": "no choices"}
        mock_post.return_value = mock_resp
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "parse_error")

    @patch("backend.llm_rewriter.requests.post")
    def test_empty_content_returns_empty_response(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": ""}}]}
        mock_post.return_value = mock_resp
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_response")

    @patch("backend.llm_rewriter.requests.post")
    def test_circuit_opens_after_three_consecutive_failures(self, mock_post):
        import requests
        mock_post.side_effect = requests.ConnectionError("refused")
        for _ in range(3):
            self.rewriter.rewrite("test")
        result = self.rewriter.rewrite("test")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "circuit_open")
        self.assertEqual(mock_post.call_count, 3)


class LLMRewriterCircuitIntegrationTestCase(unittest.TestCase):
    """Integration: circuit breaker не блокирует запросы при empty_input."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
        )

    @patch("backend.llm_rewriter.requests.post")
    def test_empty_input_does_not_count_as_failure(self, mock_post):
        for _ in range(5):
            self.rewriter.rewrite("")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        mock_post.return_value = mock_resp
        result = self.rewriter.rewrite("real text")
        self.assertTrue(result.ok)
```

- [ ] **Step 4.2: Run tests to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v 2>&1 | tail -40
```

Expected: **FAIL** — новые тесты падают с `AttributeError: 'LLMRewriter' object has no attribute 'rewrite'`.

- [ ] **Step 4.3: Implement rewrite() method**

Append to `KrabEar/backend/llm_rewriter.py` (inside `LLMRewriter` class):

```python

    def rewrite(self, text: str) -> LLMRewriteResult:
        """Отправляет текст в LLM и возвращает исправленную версию.

        Контракт: НИКОГДА не raises. Все ошибки — через LLMRewriteResult.ok=False.
        """
        import requests

        # 1. Валидация входа
        cleaned_input = (text or "").strip()
        if not cleaned_input:
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="empty_input", latency_ms=None
            )

        # 2. Circuit breaker check
        if not self._circuit.allow_request():
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="circuit_open", latency_ms=None
            )

        # 3. Подготовка запроса
        payload = {
            "model": self._model,
            "messages": self._build_messages(cleaned_input),
            "temperature": 0.0,
            "max_tokens": self._estimate_max_tokens(cleaned_input),
            "stream": False,
            "stop": ["\n\n", "Исправленный текст:", "Исходный текст:"],
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        # 4. HTTP call with timing
        start = time.monotonic()
        try:
            response = requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
        except requests.Timeout:
            self._circuit.record_failure()
            self._last_error = "timeout"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="timeout", latency_ms=None
            )
        except (requests.ConnectionError, requests.RequestException) as exc:
            self._circuit.record_failure()
            self._last_error = f"connection_error: {exc}"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="connection_error", latency_ms=None
            )

        latency_ms = int((time.monotonic() - start) * 1000)
        self._last_latency_ms = latency_ms

        # 5. HTTP status check
        if response.status_code != 200:
            self._circuit.record_failure()
            self._last_error = f"http_{response.status_code}"
            return LLMRewriteResult(
                ok=False,
                text=None,
                fallback_reason=f"http_{response.status_code}",
                latency_ms=latency_ms,
            )

        # 6. Parse JSON response
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._circuit.record_failure()
            self._last_error = f"parse_error: {exc}"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="parse_error", latency_ms=latency_ms
            )

        # 7. Postprocess
        cleaned = self._postprocess(content)
        if not cleaned:
            self._circuit.record_failure()
            self._last_error = "empty_response"
            return LLMRewriteResult(
                ok=False, text=None, fallback_reason="empty_response", latency_ms=latency_ms
            )

        # 8. Success
        self._circuit.record_success()
        self._last_error = None
        return LLMRewriteResult(
            ok=True, text=cleaned, fallback_reason=None, latency_ms=latency_ms
        )
```

- [ ] **Step 4.4: Run tests to verify all pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v 2>&1 | tail -50
```

Expected: **OK** (30 + 7 success + 7 failures + 1 integration = 45 tests passed).

- [ ] **Step 4.5: Commit**

```bash
git add KrabEar/backend/llm_rewriter.py KrabEar/tests/test_llm_rewriter.py
git commit -m "feat(ear): D.10a LLMRewriter.rewrite() with full failure handling

- rewrite() sync HTTP call via requests.post with configurable timeout
- 8-step pipeline: validate, circuit check, build payload, HTTP, status, parse, postprocess, result
- Never raises — all errors flow through LLMRewriteResult.ok=False
- Circuit breaker records failures on timeout/connection/HTTP/parse/empty
- empty_input does NOT count as circuit failure (not server's fault)
- 15 new tests: 7 success, 7 failures, 1 circuit integration"
```

---

## Task 5: LLMRewriter — ping() и status() методы

**Files:**
- Modify: `KrabEar/backend/llm_rewriter.py`
- Modify: `KrabEar/tests/test_llm_rewriter.py`

- [ ] **Step 5.1: Add tests for ping and status**

Append to `KrabEar/tests/test_llm_rewriter.py`:

```python
class LLMRewriterPingTestCase(unittest.TestCase):
    """Тесты ping() health check метода."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="test-model",
            timeout_sec=2.0,
        )

    @patch("backend.llm_rewriter.requests.get")
    def test_ping_returns_true_on_200(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        self.assertTrue(self.rewriter.ping())

    @patch("backend.llm_rewriter.requests.get")
    def test_ping_returns_false_on_connection_error(self, mock_get):
        import requests
        mock_get.side_effect = requests.ConnectionError("refused")
        self.assertFalse(self.rewriter.ping())

    @patch("backend.llm_rewriter.requests.get")
    def test_ping_returns_false_on_timeout(self, mock_get):
        import requests
        mock_get.side_effect = requests.Timeout()
        self.assertFalse(self.rewriter.ping())

    @patch("backend.llm_rewriter.requests.get")
    def test_ping_returns_false_on_http_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp
        self.assertFalse(self.rewriter.ping())

    @patch("backend.llm_rewriter.requests.get")
    def test_ping_uses_models_endpoint(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        self.rewriter.ping()
        args, _ = mock_get.call_args
        self.assertEqual(args[0], "http://localhost:1234/v1/models")


class LLMRewriterStatusTestCase(unittest.TestCase):
    """Тесты status() diagnostic метода."""

    def setUp(self):
        from backend.llm_rewriter import LLMRewriter
        self.rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key="sk-test",
            model="qwen3.5-9b@6bit",
        )

    def test_status_returns_dict_with_required_keys(self):
        status = self.rewriter.status()
        self.assertIn("reachable", status)
        self.assertIn("model", status)
        self.assertIn("circuit_state", status)
        self.assertIn("last_latency_ms", status)
        self.assertIn("last_error", status)

    def test_status_model_matches_init(self):
        status = self.rewriter.status()
        self.assertEqual(status["model"], "qwen3.5-9b@6bit")

    def test_status_initial_circuit_state_is_closed(self):
        status = self.rewriter.status()
        self.assertEqual(status["circuit_state"], "closed")

    def test_status_initial_last_error_is_none(self):
        status = self.rewriter.status()
        self.assertIsNone(status["last_error"])

    @patch("backend.llm_rewriter.requests.post")
    def test_status_reachable_true_when_circuit_closed(self, mock_post):
        status = self.rewriter.status()
        self.assertTrue(status["reachable"])

    @patch("backend.llm_rewriter.requests.post")
    def test_status_reachable_false_when_circuit_open(self, mock_post):
        import requests
        mock_post.side_effect = requests.ConnectionError()
        for _ in range(3):
            self.rewriter.rewrite("test")
        status = self.rewriter.status()
        self.assertEqual(status["circuit_state"], "open")
        self.assertFalse(status["reachable"])
```

- [ ] **Step 5.2: Run tests to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v 2>&1 | tail -30
```

Expected: **FAIL** с `AttributeError: 'LLMRewriter' object has no attribute 'ping'`.

- [ ] **Step 5.3: Implement ping() and status() methods**

Append to `KrabEar/backend/llm_rewriter.py` (inside `LLMRewriter` class):

```python

    def ping(self) -> bool:
        """Проверка доступности LM Studio через GET /models.

        Не трогает circuit breaker — это отдельный health check, используется
        только на старте backend'а. Возвращает False на любую ошибку.
        """
        import requests

        try:
            response = requests.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            return response.status_code == 200
        except Exception:
            return False

    def status(self) -> dict:
        """Health info для llm_status IPC метода."""
        return {
            "reachable": self._circuit.state != "open",
            "model": self._model,
            "circuit_state": self._circuit.state,
            "last_latency_ms": self._last_latency_ms,
            "last_error": self._last_error,
        }
```

- [ ] **Step 5.4: Run tests to verify all pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_llm_rewriter -v 2>&1 | tail -30
```

Expected: **OK** (45 + 5 ping + 6 status = 56 tests passed).

- [ ] **Step 5.5: Commit**

```bash
git add KrabEar/backend/llm_rewriter.py KrabEar/tests/test_llm_rewriter.py
git commit -m "feat(ear): D.10a LLMRewriter ping and status diagnostic methods

- ping(): GET /models health check, no circuit breaker side effects
- status(): dict with reachable, model, circuit_state, last_latency_ms, last_error
- 11 new tests covering ping edge cases and status shape"
```

---

## Task 6: AudioEngine integration — hook point + settings_get callback

**Files:**
- Modify: `KrabEar/core/engine.py`
- Create: `KrabEar/tests/test_engine_llm_integration.py`

- [ ] **Step 6.1: Create integration test file**

Create `KrabEar/tests/test_engine_llm_integration.py`:

```python
"""Integration tests для AudioEngine LLM rewrite hook."""

import unittest
from unittest.mock import MagicMock, patch


class AudioEngineLLMHookTestCase(unittest.TestCase):
    """Тесты что engine.transcribe() правильно вызывает llm_rewriter при runtime toggle=true."""

    def _make_fake_whisper_result(self, text: str):
        return {
            "text": text,
            "segments": [{"avg_logprob": -0.2}],
            "engine": "fake-whisper",
            "model_used": "fake",
            "language": "ru",
        }

    def _make_engine_with_rewriter(self, rewriter, settings_get):
        from core.engine import AudioEngine
        engine = AudioEngine()
        engine._llm_rewriter = rewriter
        engine._settings_get = settings_get
        return engine

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_transcribe_without_rewriter_returns_cleaned_text(self, mock_diar, mock_fallback):
        """llm_rewriter=None → text == cleanup output, llm_applied=False."""
        from core.engine import AudioEngine
        mock_fallback.return_value = self._make_fake_whisper_result("привет мир")
        mock_diar.return_value = None
        engine = AudioEngine()
        result = engine.transcribe(audio_data="fake.wav")
        self.assertEqual(result["raw_text"], "привет мир")
        self.assertIn("cleaned_text", result)
        self.assertFalse(result["llm_applied"])
        self.assertIsNone(result["llm_latency_ms"])

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_transcribe_with_rewriter_uses_llm_output(self, mock_diar, mock_fallback):
        """Мокнутый rewriter ok=True → engine.transcribe() text = rewriter.text."""
        from backend.llm_rewriter import LLMRewriteResult
        mock_fallback.return_value = self._make_fake_whisper_result("привет мир")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        fake_rewriter.rewrite.return_value = LLMRewriteResult(
            ok=True, text="Привет, мир.", fallback_reason=None, latency_ms=1500
        )
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: True if k == "llm_rewrite_enabled" else d,
        )

        result = engine.transcribe(audio_data="fake.wav")
        self.assertEqual(result["text"], "Привет, мир.")
        self.assertTrue(result["llm_applied"])
        self.assertEqual(result["llm_latency_ms"], 1500)
        self.assertIsNone(result["llm_fallback_reason"])
        fake_rewriter.rewrite.assert_called_once()

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_runtime_toggle_false_skips_rewriter(self, mock_diar, mock_fallback):
        """settings_get('llm_rewrite_enabled')=False → rewriter НЕ вызван."""
        mock_fallback.return_value = self._make_fake_whisper_result("тест")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: False if k == "llm_rewrite_enabled" else d,
        )

        result = engine.transcribe(audio_data="fake.wav")
        fake_rewriter.rewrite.assert_not_called()
        self.assertFalse(result["llm_applied"])

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_llm_failure_falls_back_to_cleaned_text(self, mock_diar, mock_fallback):
        """rewriter ok=False → text = cleaned_text, llm_applied=False, fallback_reason set."""
        from backend.llm_rewriter import LLMRewriteResult
        mock_fallback.return_value = self._make_fake_whisper_result("привет мир")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        fake_rewriter.rewrite.return_value = LLMRewriteResult(
            ok=False, text=None, fallback_reason="timeout", latency_ms=None
        )
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: True if k == "llm_rewrite_enabled" else d,
        )

        result = engine.transcribe(audio_data="fake.wav")
        self.assertEqual(result["text"], result["cleaned_text"])
        self.assertFalse(result["llm_applied"])
        self.assertEqual(result["llm_fallback_reason"], "timeout")

    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_transcribe_returns_all_three_text_versions(self, mock_diar, mock_fallback):
        """Dict содержит raw_text, cleaned_text, text — все три версии."""
        from backend.llm_rewriter import LLMRewriteResult
        mock_fallback.return_value = self._make_fake_whisper_result("raw text here")
        mock_diar.return_value = None

        fake_rewriter = MagicMock()
        fake_rewriter.rewrite.return_value = LLMRewriteResult(
            ok=True, text="FINAL", fallback_reason=None, latency_ms=100
        )
        engine = self._make_engine_with_rewriter(
            fake_rewriter,
            lambda k, d: True,
        )

        result = engine.transcribe(audio_data="fake.wav")
        self.assertIn("raw_text", result)
        self.assertIn("cleaned_text", result)
        self.assertIn("text", result)
        self.assertEqual(result["raw_text"], "raw text here")
        self.assertEqual(result["text"], "FINAL")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6.2: Run tests to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_engine_llm_integration -v
```

Expected: **FAIL** — тесты падают потому что `cleaned_text`, `llm_applied`, etc отсутствуют в engine return dict; `_llm_rewriter` attribute не существует.

- [ ] **Step 6.3: Modify AudioEngine.__init__ and transcribe()**

Modify `KrabEar/core/engine.py` — добавить imports и изменить `__init__` (line 78) и `transcribe` (line 184):

Изменить импорты в начале файла (найти секцию `from typing import ...`):

```python
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.llm_rewriter import LLMRewriter, LLMRewriteResult
```

Изменить `__init__` (строки 78-91):

```python
    def __init__(
        self,
        llm_rewriter: Optional["LLMRewriter"] = None,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """Инициализирует двигатель, загружая настройки из централизованного конфига.

        Args:
            llm_rewriter: опциональный D.10a LLM клиент для post-cleanup rewrite'а.
                          Если None — LLM hook отключён, работает как до D.10a.
            settings_get: callback (key, default) -> value для runtime toggle'ов.
                          Инжектируется из BackendService чтобы engine не знал про StateStore.
        """
        self.current_model = settings.MODEL_BALANCED
        self.quality_profile = "balanced"
        self._unavailable_models: set[str] = set()
        self._diarization_pipeline: Pipeline | None = None
        self._diarization_load_error: str | None = None

        # D.10a: LLM rewriter integration
        self._llm_rewriter = llm_rewriter
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)

        logger.info(
            "AudioEngine инициализирован. Профиль=%s, Модель=%s, Max Candidates=%d, LLM=%s",
            self.quality_profile,
            self.current_model,
            len(settings.model_max_list),
            "enabled" if llm_rewriter is not None else "disabled",
        )

    def _llm_rewrite_allowed(self) -> bool:
        """Runtime check: включён ли LLM rewriter И user runtime toggle."""
        if self._llm_rewriter is None:
            return False
        return bool(self._settings_get("llm_rewrite_enabled", False))
```

Изменить `transcribe()` метод — заменить блок начиная со строки ~222 (`text = TextUtils.cleanup_transcript(raw_text, profile=cleanup_profile)`) и до return на:

```python
            # 4. Очистка результата через утилиты (D.7 normalization)
            cleaned_text = TextUtils.cleanup_transcript(raw_text, profile=cleanup_profile)
            text = cleaned_text

            # 4.5 D.10a: LLM rewrite hook (только если admin+runtime toggle=true)
            llm_result = None
            if self._llm_rewrite_allowed():
                llm_result = self._llm_rewriter.rewrite(cleaned_text)
                if llm_result.ok:
                    logger.info(
                        "LLM rewrite: %d chars -> %d chars, %d ms",
                        len(cleaned_text), len(llm_result.text), llm_result.latency_ms,
                    )
                    text = llm_result.text
                else:
                    logger.debug(
                        "LLM rewrite fallback: %s (latency=%s ms)",
                        llm_result.fallback_reason,
                        llm_result.latency_ms,
                    )

            # 5. Расчет метрик уверенности
            confidence = 0.0
            if segments:
                confidence = float(np.mean([np.exp(s.get("avg_logprob", -1.0)) for s in segments]))

            duration = time.time() - start_time
            logger.info("STT готово: %.2fs, уверенность: %.2f, язык: %s", duration, confidence, resolved_lang or "auto")

            return {
                "text": text,
                "raw_text": raw_text,
                "cleaned_text": cleaned_text,
                "llm_applied": bool(llm_result is not None and llm_result.ok),
                "llm_latency_ms": llm_result.latency_ms if llm_result else None,
                "llm_fallback_reason": (
                    llm_result.fallback_reason
                    if (llm_result is not None and not llm_result.ok)
                    else None
                ),
                "confidence": round(confidence, 3),
                "duration_ms": int(duration * 1000),
                "engine": result.get("engine", "mlx-whisper"),
                "model": result.get("model_used", self.current_model),
                "language": result.get("language", resolved_lang),
                "segments": segments if not is_preview else [],
                "diarization": diarization,
            }
```

- [ ] **Step 6.4: Run integration tests to verify they pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_engine_llm_integration -v
```

Expected: **OK** (5 tests passed).

- [ ] **Step 6.5: Run full suite to verify no regression**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest discover -s KrabEar/tests -v 2>&1 | tail -30
```

Expected: все тесты (включая existing D.7) проходят.

- [ ] **Step 6.6: Commit**

```bash
git add KrabEar/core/engine.py KrabEar/tests/test_engine_llm_integration.py
git commit -m "feat(ear): D.10a AudioEngine LLM rewrite hook

- AudioEngine.__init__ accepts optional llm_rewriter + settings_get callback
- Layering preserved: engine unaware of StateStore, settings_get injected
- transcribe() hook after cleanup_transcript (D.7 output)
- Return dict extended: cleaned_text, llm_applied, llm_latency_ms, llm_fallback_reason
- HistoryItem can now save raw, cleaned, and final text versions
- 5 integration tests for passthrough, rewrite, toggle-off, failure fallback, dict shape"
```

---

## Task 7: Transcriber wrapper + BackendService initialization

**Files:**
- Modify: `KrabEar/backend/transcriber.py`
- Modify: `KrabEar/backend/service.py`
- Test: `KrabEar/tests/test_backend_service.py` (extend)

- [ ] **Step 7.1: Add test for BackendService LLM initialization**

Append to `KrabEar/tests/test_backend_service.py`:

```python
class BackendServiceLLMInitializationTestCase(unittest.TestCase):
    """Тесты что BackendService правильно инициализирует LLMRewriter когда LLM_ENABLED."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from backend.state_store import StateStore
        self.tmpdir = tempfile.mkdtemp()
        self.store = StateStore(data_dir=Path(self.tmpdir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_llm_rewriter_none_when_admin_disabled(self):
        """settings.LLM_ENABLED=False → _llm_rewriter is None."""
        from unittest.mock import patch
        with patch("core.config.settings") as mock_settings:
            mock_settings.LLM_ENABLED = False
            from backend.service import BackendService
            service = BackendService(store=self.store)
            self.assertIsNone(service._llm_rewriter)

    def test_llm_rewriter_created_when_admin_enabled(self):
        """settings.LLM_ENABLED=True → _llm_rewriter is LLMRewriter instance."""
        from unittest.mock import patch
        with patch("backend.service.settings") as mock_settings, \
             patch("backend.llm_rewriter.requests.get") as mock_get:
            mock_settings.LLM_ENABLED = True
            mock_settings.LLM_BASE_URL = "http://localhost:1234/v1"
            mock_settings.LLM_API_KEY = "sk-test"
            mock_settings.LLM_MODEL = "test-model"
            mock_settings.LLM_TIMEOUT_SEC = 4.0
            mock_settings.LLM_CIRCUIT_FAIL_THRESHOLD = 3
            mock_settings.LLM_CIRCUIT_INITIAL_RESET_SEC = 60
            mock_settings.LLM_CIRCUIT_MAX_RESET_SEC = 600
            mock_get.return_value.status_code = 200

            from backend.service import BackendService
            from backend.llm_rewriter import LLMRewriter
            service = BackendService(store=self.store)
            self.assertIsInstance(service._llm_rewriter, LLMRewriter)
```

- [ ] **Step 7.2: Run test to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_backend_service.BackendServiceLLMInitializationTestCase -v
```

Expected: **FAIL** — `AttributeError: 'BackendService' object has no attribute '_llm_rewriter'`.

- [ ] **Step 7.3: Modify Transcriber to accept llm_rewriter and settings_get**

Replace `KrabEar/backend/transcriber.py`:

```python
"""Слой транскрибации backend-сервиса Krab Ear.

Класс Transcriber является высокоуровневым интерфейсом для AudioEngine,
позволяя переключать профили качества и управлять контекстом (словарями).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TYPE_CHECKING

from core.engine import AudioEngine

if TYPE_CHECKING:
    from backend.llm_rewriter import LLMRewriter

logger = logging.getLogger("KrabEar.Backend.Transcriber")


class Transcriber:
    """Обёртка над AudioEngine для удобного вызова из API и IPC."""

    def __init__(
        self,
        engine: AudioEngine | None = None,
        llm_rewriter: Optional["LLMRewriter"] = None,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """Инициализация.

        Args:
            engine: опциональный AudioEngine. Если None — создаётся новый с
                    инжекцией llm_rewriter и settings_get.
            llm_rewriter: D.10a LLM клиент для post-cleanup rewrite'а (прокидывается в AudioEngine).
            settings_get: callback для runtime toggle'ов (прокидывается в AudioEngine).
        """
        if engine is None:
            self.engine = AudioEngine(llm_rewriter=llm_rewriter, settings_get=settings_get)
        else:
            self.engine = engine
            if llm_rewriter is not None and engine._llm_rewriter is None:
                engine._llm_rewriter = llm_rewriter
            if settings_get is not None:
                engine._settings_get = settings_get

    def transcribe(
        self,
        audio_data: Any,
        quality_profile: str = "balanced",
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary: list[str] | None = None,
        lang_hint: str | None = None,
    ) -> dict[str, Any]:
        """Транскрибирует аудио с учётом выбранного профиля и контекста."""
        self.engine.set_quality_profile(quality_profile)
        return self.engine.transcribe(
            audio_data,
            cleanup_profile=cleanup_profile,
            is_preview=False,
            domain=domain,
            extra_vocabulary=extra_vocabulary,
            lang_hint=lang_hint,
        )

    def transcribe_preview(self, audio_data: Any, quality_profile: str = "balanced") -> dict[str, Any]:
        """Быстрая транскрибация для realtime-превью (всегда в balanced режиме)."""
        self.engine.set_quality_profile("balanced")
        return self.engine.transcribe(audio_data, cleanup_profile="soft", is_preview=True)
```

- [ ] **Step 7.4: Modify BackendService.__init__ to create LLMRewriter**

Modify `KrabEar/backend/service.py` — найти `class BackendService` и заменить `__init__` (строки 55-78) на:

```python
    def __init__(
        self,
        store: StateStore,
        recorder: AudioRecorder | None = None,
        transcriber: Transcriber | None = None,
        translator: Translator | None = None,
    ) -> None:
        self.store = store
        self.recorder = recorder or AudioRecorder()

        # D.10a: LLM rewriter initialization (admin flag check via settings)
        self._llm_rewriter = self._init_llm_rewriter()

        if transcriber is None:
            self.transcriber = Transcriber(
                llm_rewriter=self._llm_rewriter,
                settings_get=self._get_runtime_setting,
            )
        else:
            self.transcriber = transcriber
            if self._llm_rewriter is not None:
                if hasattr(transcriber, "engine"):
                    if transcriber.engine._llm_rewriter is None:
                        transcriber.engine._llm_rewriter = self._llm_rewriter
                    transcriber.engine._settings_get = self._get_runtime_setting

        self.translator = translator or Translator()
        self._preview_lock = threading.Lock()
        self._preview_thread: threading.Thread | None = None
        self._preview_stop_event = threading.Event()
        self._preview_text = ""
        self._preview_duration_sec = 0.0
        self._preview_updated_at = 0.0
        self._call_assist_lock = threading.Lock()
        self._call_assist_state: dict[str, Any] = {
            "active": False,
            "status": "idle",
            "session_id": None,
            "gateway_session_id": None,
        }

    def _init_llm_rewriter(self):
        """Создаёт LLMRewriter если settings.LLM_ENABLED. Возвращает None иначе."""
        if not settings.LLM_ENABLED:
            return None

        try:
            from backend.llm_rewriter import LLMRewriter
            rewriter = LLMRewriter(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
                model=settings.LLM_MODEL,
                timeout_sec=settings.LLM_TIMEOUT_SEC,
                circuit_fail_threshold=settings.LLM_CIRCUIT_FAIL_THRESHOLD,
                circuit_initial_reset_sec=settings.LLM_CIRCUIT_INITIAL_RESET_SEC,
                circuit_max_reset_sec=settings.LLM_CIRCUIT_MAX_RESET_SEC,
            )
            if rewriter.ping():
                logger.info(
                    "LLM rewriter инициализирован: %s @ %s",
                    settings.LLM_MODEL,
                    settings.LLM_BASE_URL,
                )
            else:
                logger.warning(
                    "LLM rewriter не отвечает на ping (%s), будет circuit-break'нут при первом rewrite",
                    settings.LLM_BASE_URL,
                )
            return rewriter
        except Exception as exc:
            logger.exception("Не удалось инициализировать LLM rewriter: %s", exc)
            return None

    def _get_runtime_setting(self, key: str, default: Any) -> Any:
        """Callback для AudioEngine: читает runtime toggle из StateStore.

        Используется для проверки llm_rewrite_enabled на каждой транскрипции.
        """
        try:
            return self.store.load_settings().get(key, default)
        except Exception:
            return default
```

Убедись что `from core.config import settings` присутствует в импортах (он уже там через `from core.config import ...`).

- [ ] **Step 7.5: Run tests to verify they pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_backend_service -v 2>&1 | tail -30
```

Expected: **OK** — новые тесты проходят, существующие не падают.

- [ ] **Step 7.6: Run full suite for regression check**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest discover -s KrabEar/tests -v 2>&1 | tail -15
```

Expected: all tests passing.

- [ ] **Step 7.7: Commit**

```bash
git add KrabEar/backend/transcriber.py KrabEar/backend/service.py KrabEar/tests/test_backend_service.py
git commit -m "feat(ear): D.10a wire LLMRewriter into Transcriber and BackendService

- Transcriber.__init__ accepts llm_rewriter + settings_get, passes to AudioEngine
- BackendService._init_llm_rewriter: creates instance if settings.LLM_ENABLED, pings on startup
- BackendService._get_runtime_setting: callback reading llm_rewrite_enabled from StateStore
- Failed ping does NOT nullify rewriter — circuit breaker handles recovery later
- 2 new tests for initialization paths"
```

---

## Task 8: IPC — `llm_status` method

**Files:**
- Modify: `KrabEar/backend/service.py`
- Modify: `KrabEar/tests/test_backend_service.py`

- [ ] **Step 8.1: Add tests for llm_status handler**

Append to `KrabEar/tests/test_backend_service.py`:

```python
class LLMStatusIPCTestCase(unittest.TestCase):
    """Тесты IPC метода llm_status."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from backend.state_store import StateStore
        self.tmpdir = tempfile.mkdtemp()
        self.store = StateStore(data_dir=Path(self.tmpdir))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_llm_status_returns_disabled_when_not_initialized(self):
        """Без LLM_ENABLED → enabled=False, admin_enabled=False."""
        from unittest.mock import patch
        with patch("backend.service.settings") as mock_settings:
            mock_settings.LLM_ENABLED = False
            from backend.service import BackendService
            service = BackendService(store=self.store)
            response = service.handle_request({"id": "1", "method": "llm_status"})
            self.assertTrue(response["ok"])
            result = response["result"]
            self.assertFalse(result["enabled"])
            self.assertFalse(result["admin_enabled"])
            self.assertIsNone(result["model"])

    def test_llm_status_returns_full_info_when_initialized(self):
        from unittest.mock import patch
        with patch("backend.service.settings") as mock_settings, \
             patch("backend.llm_rewriter.requests.get") as mock_get:
            mock_settings.LLM_ENABLED = True
            mock_settings.LLM_BASE_URL = "http://localhost:1234/v1"
            mock_settings.LLM_API_KEY = "sk-test"
            mock_settings.LLM_MODEL = "qwen3.5-9b@6bit"
            mock_settings.LLM_TIMEOUT_SEC = 4.0
            mock_settings.LLM_CIRCUIT_FAIL_THRESHOLD = 3
            mock_settings.LLM_CIRCUIT_INITIAL_RESET_SEC = 60
            mock_settings.LLM_CIRCUIT_MAX_RESET_SEC = 600
            mock_get.return_value.status_code = 200

            from backend.service import BackendService
            service = BackendService(store=self.store)

            current = self.store.load_settings()
            current["llm_rewrite_enabled"] = True
            self.store.save_settings(current)

            response = service.handle_request({"id": "2", "method": "llm_status"})
            self.assertTrue(response["ok"])
            result = response["result"]
            self.assertTrue(result["admin_enabled"])
            self.assertTrue(result["runtime_enabled"])
            self.assertEqual(result["model"], "qwen3.5-9b@6bit")
            self.assertEqual(result["circuit_state"], "closed")
            self.assertTrue(result["enabled"])

    def test_llm_status_enabled_false_when_runtime_toggle_off(self):
        from unittest.mock import patch
        with patch("backend.service.settings") as mock_settings, \
             patch("backend.llm_rewriter.requests.get") as mock_get:
            mock_settings.LLM_ENABLED = True
            mock_settings.LLM_BASE_URL = "http://localhost:1234/v1"
            mock_settings.LLM_API_KEY = "sk-test"
            mock_settings.LLM_MODEL = "test"
            mock_settings.LLM_TIMEOUT_SEC = 4.0
            mock_settings.LLM_CIRCUIT_FAIL_THRESHOLD = 3
            mock_settings.LLM_CIRCUIT_INITIAL_RESET_SEC = 60
            mock_settings.LLM_CIRCUIT_MAX_RESET_SEC = 600
            mock_get.return_value.status_code = 200

            from backend.service import BackendService
            service = BackendService(store=self.store)

            response = service.handle_request({"id": "3", "method": "llm_status"})
            result = response["result"]
            self.assertTrue(result["admin_enabled"])
            self.assertFalse(result["runtime_enabled"])
            self.assertFalse(result["enabled"])
```

- [ ] **Step 8.2: Run tests to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_backend_service.LLMStatusIPCTestCase -v
```

Expected: **FAIL** с `unknown_method: llm_status`.

- [ ] **Step 8.3: Add `_handle_llm_status` method and register in dispatch**

Modify `KrabEar/backend/service.py`:

1. В `handle_request` handlers dict (строка 88) добавить новую строку после `"summarize_text": self._handle_summarize_text,`:

```python
            "llm_status": self._handle_llm_status,
```

2. Добавить метод `_handle_llm_status` — разместить рядом с `_handle_ping` (или в конце класса):

```python
    def _handle_llm_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает диагностическую информацию о LLM rewriter'е.

        D.10a. Используется для Swift UI статус-индикатора и dev smoke тестов.
        """
        runtime_enabled = bool(self.store.load_settings().get("llm_rewrite_enabled", False))

        if self._llm_rewriter is None:
            return {
                "enabled": False,
                "admin_enabled": bool(settings.LLM_ENABLED),
                "runtime_enabled": runtime_enabled,
                "reachable": False,
                "model": None,
                "circuit_state": None,
                "last_latency_ms": None,
                "last_error": "llm_rewriter не инициализирован",
            }

        status = self._llm_rewriter.status()
        status["admin_enabled"] = True
        status["runtime_enabled"] = runtime_enabled
        status["enabled"] = bool(
            status["admin_enabled"] and status["runtime_enabled"] and status["reachable"]
        )
        return status
```

- [ ] **Step 8.4: Run tests to verify they pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_backend_service -v 2>&1 | tail -30
```

Expected: **OK** — все тесты проходят.

- [ ] **Step 8.5: Commit**

```bash
git add KrabEar/backend/service.py KrabEar/tests/test_backend_service.py
git commit -m "feat(ear): D.10a llm_status IPC method

- _handle_llm_status: returns full diagnostic dict (enabled, admin, runtime, reachable, model, circuit)
- Registered in handle_request dispatch
- enabled = admin AND runtime AND reachable (hybrid toggle composition)
- 3 new tests for disabled/initialized/runtime-off paths"
```

---

## Task 9: HistoryItem extension — save raw/cleaned/final text versions

**Files:**
- Modify: `KrabEar/backend/models.py`
- Modify: `KrabEar/backend/service.py` (`_handle_stop_recording`)
- Test: `KrabEar/tests/test_models.py` (new or extend existing)

**Why:** DoD пункт "HistoryItem содержит raw_text, cleaned_text, text, llm_applied, llm_latency_ms" (section 5.2 spec). Позволяет диагностику и будущий "undo rewrite" в D.10d.

- [ ] **Step 9.1: Add test for extended HistoryItem**

Create or append to `KrabEar/tests/test_models.py`:

```python
"""Тесты для backend/models.py HistoryItem."""

import unittest


class HistoryItemLLMFieldsTestCase(unittest.TestCase):
    """Тесты новых D.10a полей HistoryItem."""

    def test_history_item_has_llm_fields(self):
        from backend.models import HistoryItem
        item = HistoryItem.create(text="test")
        self.assertEqual(item.cleaned_text, "")
        self.assertFalse(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 0)

    def test_history_item_create_accepts_llm_fields(self):
        from backend.models import HistoryItem
        item = HistoryItem.create(
            text="Привет, мир.",
            cleaned_text="привет мир",
            llm_applied=True,
            llm_latency_ms=1500,
        )
        self.assertEqual(item.text, "Привет, мир.")
        self.assertEqual(item.cleaned_text, "привет мир")
        self.assertTrue(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 1500)

    def test_history_item_to_dict_includes_llm_fields(self):
        from backend.models import HistoryItem
        item = HistoryItem.create(
            text="final",
            cleaned_text="cleaned",
            llm_applied=True,
            llm_latency_ms=1000,
        )
        d = item.to_dict()
        self.assertIn("cleaned_text", d)
        self.assertIn("llm_applied", d)
        self.assertIn("llm_latency_ms", d)

    def test_history_item_from_dict_handles_missing_llm_fields(self):
        """Backward compat: старые NDJSON записи без LLM полей должны загружаться с дефолтами."""
        from backend.models import HistoryItem
        legacy_payload = {
            "id": "abc",
            "ts": "2026-04-01T10:00:00",
            "text": "legacy entry",
        }
        item = HistoryItem.from_dict(legacy_payload)
        self.assertEqual(item.text, "legacy entry")
        self.assertEqual(item.cleaned_text, "")
        self.assertFalse(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 0)

    def test_history_item_from_dict_loads_llm_fields(self):
        from backend.models import HistoryItem
        payload = {
            "id": "abc",
            "ts": "2026-04-09T10:00:00",
            "text": "Привет, мир.",
            "cleaned_text": "привет мир",
            "llm_applied": True,
            "llm_latency_ms": 1500,
        }
        item = HistoryItem.from_dict(payload)
        self.assertEqual(item.cleaned_text, "привет мир")
        self.assertTrue(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 1500)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 9.2: Run test to verify failure**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_models -v
```

Expected: **FAIL** с `TypeError: HistoryItem.create() got unexpected keyword argument 'cleaned_text'`.

- [ ] **Step 9.3: Extend HistoryItem with LLM fields**

Modify `KrabEar/backend/models.py` — расширить dataclass, `create()`, `from_dict()`:

```python
"""Модели данных backend-сервиса Krab Ear."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any
import uuid


@dataclass(slots=True)
class HistoryItem:
    """Одна запись транскрибации в истории."""

    id: str
    ts: str
    text: str
    paste_status: str = "failed"
    source_text: str = ""
    translated_text: str = ""
    translation_mode: str = "off"
    source_lang: str = ""
    target_lang: str = ""
    translation_status: str = "not_requested"
    translation_engine: str = ""
    chat_id: str = ""
    message_id: str = ""
    # D.10a: LLM rewrite tracking
    cleaned_text: str = ""
    llm_applied: bool = False
    llm_latency_ms: int = 0

    @classmethod
    def create(
        cls,
        text: str,
        paste_status: str = "failed",
        source_text: str = "",
        translated_text: str = "",
        translation_mode: str = "off",
        source_lang: str = "",
        target_lang: str = "",
        translation_status: str = "not_requested",
        translation_engine: str = "",
        chat_id: str = "",
        message_id: str = "",
        cleaned_text: str = "",
        llm_applied: bool = False,
        llm_latency_ms: int = 0,
    ) -> "HistoryItem":
        """Создаёт новую запись с корректным идентификатором и временем."""
        return cls(
            id=str(uuid.uuid4()),
            ts=datetime.now().isoformat(timespec="seconds"),
            text=text,
            paste_status=paste_status,
            source_text=source_text.strip(),
            translated_text=translated_text.strip(),
            translation_mode=translation_mode.strip() or "off",
            source_lang=source_lang.strip(),
            target_lang=target_lang.strip(),
            translation_status=translation_status.strip() or "not_requested",
            translation_engine=translation_engine.strip(),
            chat_id=str(chat_id).strip(),
            message_id=str(message_id).strip(),
            cleaned_text=(cleaned_text or "").strip(),
            llm_applied=bool(llm_applied),
            llm_latency_ms=int(llm_latency_ms or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        """Преобразует dataclass в сериализуемый словарь."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HistoryItem":
        """Восстанавливает запись из JSON-словаря с мягкой валидацией.

        Backward compat: старые NDJSON записи без D.10a полей получают дефолты.
        """
        return cls(
            id=str(payload.get("id", "")).strip(),
            ts=str(payload.get("ts", "")).strip(),
            text=str(payload.get("text", "")).strip(),
            paste_status=str(payload.get("paste_status", "failed")).strip() or "failed",
            source_text=str(payload.get("source_text", "")).strip(),
            translated_text=str(payload.get("translated_text", "")).strip(),
            translation_mode=str(payload.get("translation_mode", "off")).strip() or "off",
            source_lang=str(payload.get("source_lang", "")).strip(),
            target_lang=str(payload.get("target_lang", "")).strip(),
            translation_status=str(payload.get("translation_status", "not_requested")).strip() or "not_requested",
            translation_engine=str(payload.get("translation_engine", "")).strip(),
            chat_id=str(payload.get("chat_id", "")).strip(),
            message_id=str(payload.get("message_id", "")).strip(),
            cleaned_text=str(payload.get("cleaned_text", "")).strip(),
            llm_applied=bool(payload.get("llm_applied", False)),
            llm_latency_ms=int(payload.get("llm_latency_ms", 0) or 0),
        )
```

- [ ] **Step 9.4: Find stop_recording history write and populate LLM fields**

Check how `_handle_stop_recording` creates HistoryItem:

```bash
grep -n "HistoryItem.create\|HistoryItem(" KrabEar/backend/service.py | head -10
```

Use Read tool to view the exact code block (likely near line ~173+ continuing `_handle_stop_recording`). In that code, find the `HistoryItem.create(text=...)` call and add the new kwargs from the transcribe result dict:

```python
# Example pattern — actual line numbers will differ:
item = HistoryItem.create(
    text=result.get("text", ""),
    # ... existing kwargs ...
    cleaned_text=result.get("cleaned_text", ""),
    llm_applied=bool(result.get("llm_applied", False)),
    llm_latency_ms=int(result.get("llm_latency_ms", 0) or 0),
)
```

If there are multiple `HistoryItem.create()` callsites in `service.py` — update ONLY the one inside the main transcribe flow (`_handle_stop_recording` path). Other callsites (e.g. for translation history) don't have LLM context and should be left with defaults.

- [ ] **Step 9.5: Run tests to verify they pass**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar.tests.test_models -v
PYTHONPATH=$(pwd)/KrabEar python -m unittest discover -s KrabEar/tests -v 2>&1 | tail -15
```

Expected: **OK** — new tests pass, no regression in existing tests.

- [ ] **Step 9.6: Commit**

```bash
git add KrabEar/backend/models.py KrabEar/backend/service.py KrabEar/tests/test_models.py
git commit -m "feat(ear): D.10a extend HistoryItem with LLM rewrite tracking

- HistoryItem: cleaned_text, llm_applied, llm_latency_ms fields with defaults
- create() and from_dict() accept new fields with backward compat
- stop_recording populates fields from AudioEngine.transcribe() return dict
- 5 new tests including backward compat for legacy NDJSON entries"
```

---

## Task 10: Update `.secrets`, smoke test script, full integration verification

**Files:**
- Modify: `~/Library/Application Support/KrabEar/.secrets` (outside repo)
- Create: `scripts/smoke_test_d10a.command` (new)

**This task has no new unit tests — it's the bridge between test coverage and real-world validation.**

- [ ] **Step 10.1: Update `.secrets` file**

Read current content:

```bash
cat "$HOME/Library/Application Support/KrabEar/.secrets"
```

Expected current (from session 3):

```
KRAB_EAR_LLM_BASE_URL=http://localhost:1234/v1
KRAB_EAR_LLM_API_KEY=sk-lm-aM6s3ukv:YBV64I1QrsqVg6LYbH9H
KRAB_EAR_LLM_PROVIDER=lmstudio
KRAB_EAR_LLM_MODEL=qwen3.5-9b-mlx@bf16
KRAB_EAR_LLM_MODEL_FAST=qwen3.5-4b-mlx
KRAB_EAR_LLM_MODEL_HEAVY=qwen3.5-27b-claude-4.6-opus-reasoning-distilled-qx64-hi-mlx
```

Write the updated file (need to preserve `KRAB_EAR_HF_TOKEN` if present — verify first):

```bash
grep "KRAB_EAR_HF_TOKEN" "$HOME/Library/Application Support/KrabEar/.secrets"
```

If HF_TOKEN is not in `.secrets` (currently only in plist), leave it — the `.secrets` file will only have LLM_* vars plus the new ENABLED flag. Overwrite the file with:

```
KRAB_EAR_LLM_ENABLED=true
KRAB_EAR_LLM_BASE_URL=http://localhost:1234/v1
KRAB_EAR_LLM_API_KEY=sk-lm-aM6s3ukv:YBV64I1QrsqVg6LYbH9H
KRAB_EAR_LLM_PROVIDER=lmstudio
KRAB_EAR_LLM_MODEL=qwen3.5-9b@6bit
KRAB_EAR_LLM_TIMEOUT_SEC=4.0
KRAB_EAR_LLM_MODEL_FAST=qwen3.5-4b-mlx
KRAB_EAR_LLM_MODEL_HEAVY=qwen3.5-27b-claude-4.6-opus-reasoning-distilled-qx64-hi-mlx
```

**Critical change:** `KRAB_EAR_LLM_MODEL` switches from `qwen3.5-9b-mlx@bf16` to `qwen3.5-9b@6bit` (6-bit quant — sweet spot for M4 Max 36 GB, verified in brainstorming).

- [ ] **Step 10.2: Create smoke test script**

Create `scripts/smoke_test_d10a.command`:

```bash
#!/bin/bash
# D.10a smoke test — end-to-end LLM rewriter verification.
#
# Что проверяет:
#   1. Backend инициализировал LLM rewriter (log grep)
#   2. llm_status IPC возвращает reachable=true, circuit=closed
#   3. Включает runtime toggle
#   4. Пингует backend чтобы убедиться что работает
#
# НЕ проверяет настоящую диктовку — это делается вручную через GUI.

set -e

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SOCKET="$HOME/Library/Application Support/KrabEar/krabear.sock"
LOG_FILE="$ROOT_DIR/logs/krab-ear-backend.out.log"

log() { printf '[smoke] %s\n' "$*"; }
fail() { printf '[smoke] ❌ %s\n' "$*" >&2; exit 1; }

# 1. Socket exists
[ -S "$SOCKET" ] || fail "socket not found: $SOCKET (backend not running?)"

# 2. Backend log contains LLM initialization message
if ! grep -q "LLM rewriter инициализирован" "$LOG_FILE" 2>/dev/null; then
  log "⚠️  Не найдено 'LLM rewriter инициализирован' в $LOG_FILE"
  log "    Возможные причины: KRAB_EAR_LLM_ENABLED=false или старый backend"
  log "    Попробуй: launchctl kickstart -k gui/$(id -u)/ai.krab.ear.backend"
  fail "LLM rewriter не инициализирован"
fi
log "✅ LLM rewriter инициализирован (найдено в логе)"

# 3. Call llm_status IPC
log "запрос llm_status..."
STATUS_JSON="$(python3 -c "
import socket, json, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('$SOCKET')
s.sendall((json.dumps({'id':'smoke','method':'llm_status','params':{}})+'\n').encode())
data = s.recv(8192).decode()
print(data.strip())
s.close()
" 2>&1)"

if ! echo "$STATUS_JSON" | grep -q '"ok":\s*true'; then
  fail "llm_status IPC failed: $STATUS_JSON"
fi

log "llm_status response: $STATUS_JSON"

MODEL="$(echo "$STATUS_JSON" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('model','<none>'))")"
CIRCUIT="$(echo "$STATUS_JSON" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('circuit_state','<none>'))")"
REACHABLE="$(echo "$STATUS_JSON" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('reachable',False))")"

log "  model=$MODEL"
log "  circuit_state=$CIRCUIT"
log "  reachable=$REACHABLE"

[ "$CIRCUIT" = "closed" ] || fail "circuit_state not closed: $CIRCUIT"
[ "$REACHABLE" = "True" ] || fail "reachable not True: $REACHABLE"

# 4. Enable runtime toggle
log "включение runtime toggle (llm_rewrite_enabled=true)..."
TOGGLE_JSON="$(python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('$SOCKET')
s.sendall((json.dumps({
    'id':'toggle',
    'method':'set_settings',
    'params':{'settings':{'llm_rewrite_enabled':True}}
})+'\n').encode())
data = s.recv(8192).decode()
print(data.strip())
s.close()
")"

if ! echo "$TOGGLE_JSON" | grep -q '"ok":\s*true'; then
  fail "set_settings toggle failed: $TOGGLE_JSON"
fi
log "✅ runtime toggle включён"

# 5. Re-check llm_status that enabled is now true
STATUS_JSON2="$(python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(5)
s.connect('$SOCKET')
s.sendall((json.dumps({'id':'smoke2','method':'llm_status','params':{}})+'\n').encode())
data = s.recv(8192).decode()
print(data.strip())
s.close()
")"

ENABLED="$(echo "$STATUS_JSON2" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('enabled',False))")"
RUNTIME="$(echo "$STATUS_JSON2" | python3 -c "import json,sys;d=json.loads(sys.stdin.read());print(d['result'].get('runtime_enabled',False))")"

log "  enabled=$ENABLED"
log "  runtime_enabled=$RUNTIME"

[ "$ENABLED" = "True" ] || fail "overall enabled not True after toggle: $ENABLED"

log "✅ D.10a smoke test пройден"
log ""
log "Следующий шаг — manual test: диктуй через Right Option и проверь что текст"
log "попал с правильной пунктуацией. Смотри $LOG_FILE на 'LLM rewrite:' строки."
```

Make executable:

```bash
chmod +x scripts/smoke_test_d10a.command
```

- [ ] **Step 10.3: Bounce backend to pick up new code and .secrets**

```bash
launchctl kickstart -k gui/$(id -u)/ai.krab.ear.backend
```

Wait 10 sec for Whisper model load.

- [ ] **Step 10.4: Run smoke test**

```bash
./scripts/smoke_test_d10a.command
```

Expected: все шаги зелёные, финальное сообщение "D.10a smoke test пройден".

If the smoke test fails at "LLM rewriter инициализирован" grep — check:
1. Is LM Studio running? `curl http://localhost:1234/v1/models | head`
2. Is `.secrets` correctly formatted? `cat ~/Library/Application\ Support/KrabEar/.secrets`
3. Did backend restart pick up the new code? `launchctl print gui/$(id -u)/ai.krab.ear.backend | grep -A2 "state ="`

- [ ] **Step 10.5: Manual dictation smoke test (user must do this)**

Press Right Option, dictate each of these:

1. Short: "привет как дела надо купить молоко и хлеб"
   - Expect pause ~1.5-2.5 sec before paste
   - Expect: "Привет, как дела? Надо купить молоко и хлеб."

2. With brands: "давай установим докер и питон потом запустим клод код"
   - Expect: "Docker", "Python", "Claude Code" латиницей
   - Expected: "Давай установим Docker и Python, потом запустим Claude Code."

3. Long (~50 words): длинное сообщение для Claude Code
   - Expect: latency 2-4 sec, текст цельный, ничего не обрезано

4. Tail `logs/krab-ear-backend.out.log` during dictation to see `LLM rewrite: N chars -> M chars, X ms` lines.

- [ ] **Step 10.6: Failure mode manual test**

1. Quit LM Studio
2. Dictate 4 short phrases
3. Tail log — expect exactly ONE warning "CLOSED -> OPEN" after 3rd failure
4. Dictate 5 more — expect NO new warnings (circuit open, no HTTP attempts)
5. Start LM Studio
6. Wait 65 sec
7. Dictate one phrase — expect "HALF_OPEN -> CLOSED" info log + proper rewrite

- [ ] **Step 10.7: Run full test suite one final time**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m unittest discover -s KrabEar/tests -v 2>&1 | tail -20
```

Expected: all tests passing (existing + new ~60).

- [ ] **Step 10.8: Commit**

```bash
git add scripts/smoke_test_d10a.command
git commit -m "feat(ear): D.10a smoke test script for LLM rewriter verification

- scripts/smoke_test_d10a.command: checks backend log, llm_status IPC, runtime toggle
- Fails fast on missing socket, non-initialized rewriter, non-closed circuit
- Not a replacement for manual dictation test — validates infrastructure only"
```

- [ ] **Step 10.9: Push branch**

```bash
git push -u origin claude/ecstatic-moser
```

Expected: branch pushed successfully. User can then open PR to `codex/krab-ear-v2`.

---

## Final Verification Checklist

Before marking D.10a complete, verify **all** items:

- [ ] All unit tests pass: `PYTHONPATH=$(pwd)/KrabEar python -m unittest discover -s KrabEar/tests -v`
- [ ] `llm_rewriter.py` has >= 55 unit tests
- [ ] `test_engine_llm_integration.py` has 5 integration tests
- [ ] `test_backend_service.py` has 5 new tests (2 init + 3 llm_status)
- [ ] `test_models.py` has 5 HistoryItem LLM tests
- [ ] `test_config_llm.py` has 10 config tests
- [ ] Backend log contains `LLM rewriter инициализирован` after kickstart
- [ ] `llm_status` IPC returns `reachable=true, circuit_state=closed, model=qwen3.5-9b@6bit`
- [ ] Smoke test script passes
- [ ] Manual dictation test: 3 phrases rewritten correctly with brands latinized
- [ ] Failure mode test: exactly 1 "CLOSED -> OPEN" warning in log after 3 fails, recovery to CLOSED after LM Studio restart
- [ ] `history.ndjson` new entries contain `cleaned_text`, `llm_applied`, `llm_latency_ms`
- [ ] `runtime_enabled=false` via IPC → dictation skips LLM without restart
- [ ] Branch pushed to remote
- [ ] No regressions in existing tests

---

## Rollback

If anything goes wrong in production:

```bash
# 1. Disable LLM via admin flag
sed -i '' 's/^KRAB_EAR_LLM_ENABLED=true/KRAB_EAR_LLM_ENABLED=false/' "$HOME/Library/Application Support/KrabEar/.secrets"

# 2. Kick backend to reload
launchctl kickstart -k gui/$(id -u)/ai.krab.ear.backend

# 3. Verify
./scripts/smoke_test_d10a.command
# Expected to fail at "LLM rewriter инициализирован" — this is correct
```

Zero code rollback needed. Dictation continues to work as in D.7 state.
