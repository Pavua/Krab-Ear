"""wave-27 MED fixes: speaker alias injection guard + CSV formula injection + speaker cap.

B1 (MED injection): speaker alias sanitised at SET time — rejects CRLF, forbidden
Markdown/CSV chars (| [ ] ( ) < > = + - @ " ' &), and names > 100 chars.
B2 (MED CSV formula injection): covered by B1 — leading = + - @ chars rejected.
B3 (MED DoS): register_speaker raises when len(_fingerprints) >= MAX_SPEAKERS.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Standalone path setup
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.speaker_manager import (  # noqa: E402
    MAX_SPEAKERS,
    SpeakerManager,
    _ALIAS_MAX_LEN,
    _sanitize_alias,
)


# ---------------------------------------------------------------------------
# B1/B2 — _sanitize_alias unit tests
# ---------------------------------------------------------------------------

class TestSanitizeAliasUnit(unittest.TestCase):
    """Direct unit tests for the _sanitize_alias helper."""

    def test_clean_name_passes(self):
        """Обычное имя без запрещённых символов не изменяется."""
        self.assertEqual(_sanitize_alias("Паша"), "Паша")

    def test_strips_leading_trailing_spaces(self):
        """Ведущие и завершающие пробелы обрезаются."""
        self.assertEqual(_sanitize_alias("  Паша  "), "Паша")

    def test_rejects_crlf(self):
        """Имена с CR (\\r) отклоняются."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша\rМаша")

    def test_rejects_lf(self):
        """Имена с LF (\\n) отклоняются."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша\nМаша")

    def test_rejects_crlf_sequence(self):
        """Имена с CRLF (\\r\\n) отклоняются."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша\r\nМаша")

    def test_rejects_too_long(self):
        """Имена длиннее _ALIAS_MAX_LEN отклоняются."""
        long_name = "А" * (_ALIAS_MAX_LEN + 1)
        with self.assertRaises(ValueError):
            _sanitize_alias(long_name)

    def test_accepts_max_len(self):
        """Имя ровно _ALIAS_MAX_LEN символов принимается."""
        max_name = "А" * _ALIAS_MAX_LEN
        self.assertEqual(_sanitize_alias(max_name), max_name)

    def test_rejects_pipe(self):
        """Символ | (Markdown table separator) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша|Маша")

    def test_rejects_open_bracket(self):
        """Символ [ (Markdown link start) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша[0]")

    def test_rejects_close_bracket(self):
        """Символ ] (Markdown link end) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша]")

    def test_rejects_open_paren(self):
        """Символ ( (Markdown link URL start) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша (RU)")

    def test_rejects_close_paren(self):
        """Символ ) (Markdown link URL end) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша)")

    def test_rejects_less_than(self):
        """Символ < (HTML/XML injection) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша<script>")

    def test_rejects_greater_than(self):
        """Символ > (HTML/XML injection) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша>Маша")

    def test_rejects_double_quote(self):
        """Символ \" (attr injection) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias('Паша"Маша')

    def test_rejects_single_quote(self):
        """Символ ' (SQL/HTML injection) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша'Маша")

    def test_rejects_ampersand(self):
        """Символ & (HTML entity injection) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("Паша&Маша")

    def test_unicode_name_passes(self):
        """Unicode-имена (кириллица, иероглифы, эмодзи) разрешены."""
        self.assertEqual(_sanitize_alias("Александра 🎙"), "Александра 🎙")
        self.assertEqual(_sanitize_alias("田中さん"), "田中さん")
        self.assertEqual(_sanitize_alias("María José"), "María José")


# ---------------------------------------------------------------------------
# B2 — CSV formula injection via leading chars
# ---------------------------------------------------------------------------

class TestCSVFormulaInjection(unittest.TestCase):
    """B2: leading = + - @ chars are rejected (CSV formula injection guard)."""

    def test_rejects_leading_equals(self):
        """Имя начинающееся с '=' (Excel formula) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("=CMD|' /C calc'!A0")

    def test_rejects_leading_plus(self):
        """Имя начинающееся с '+' (Lotus formula) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("+1234567890")

    def test_rejects_leading_minus(self):
        """Имя начинающееся с '-' (formula prefix) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("-1+2")

    def test_rejects_leading_at(self):
        """Имя начинающееся с '@' (DDE formula) отклоняется."""
        with self.assertRaises(ValueError):
            _sanitize_alias("@SUM(A1:A2)")

    def test_allows_equals_in_middle(self):
        """Символ '=' в середине имени тоже запрещён (не только в начале)."""
        # = is in _ALIAS_FORBIDDEN_CHARS, so forbidden anywhere
        with self.assertRaises(ValueError):
            _sanitize_alias("Иван=Паша")

    def test_name_not_starting_with_formula_char_passes(self):
        """Имя без запрещённых символов проходит, даже если содержит цифры."""
        self.assertEqual(_sanitize_alias("Спикер123"), "Спикер123")


# ---------------------------------------------------------------------------
# B1 — set_alias integration tests
# ---------------------------------------------------------------------------

class TestSetAliasInjectionGuard(unittest.TestCase):
    """B1: set_alias enforces sanitisation."""

    def setUp(self):
        self.mgr = SpeakerManager()

    def test_set_alias_rejects_crlf(self):
        """set_alias: имя с CRLF отклоняется до сохранения."""
        with self.assertRaises(ValueError):
            self.mgr.set_alias("SPEAKER_00", "Паша\nМаша")
        # Alias should NOT be stored
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_set_alias_rejects_pipe(self):
        """set_alias: имя с | отклоняется."""
        with self.assertRaises(ValueError):
            self.mgr.set_alias("SPEAKER_00", "Паша|Маша")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_set_alias_rejects_bracket(self):
        """set_alias: имя с [ отклоняется."""
        with self.assertRaises(ValueError):
            self.mgr.set_alias("SPEAKER_00", "[Внедрение]")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_set_alias_rejects_too_long(self):
        """set_alias: имя длиннее 100 символов отклоняется."""
        long_name = "А" * 101
        with self.assertRaises(ValueError):
            self.mgr.set_alias("SPEAKER_00", long_name)
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_set_alias_rejects_csv_formula_leading_equals(self):
        """set_alias: имя начинающееся с '=' (CSV formula) отклоняется."""
        with self.assertRaises(ValueError):
            self.mgr.set_alias("SPEAKER_00", "=HYPERLINK(\"http://evil.com\")")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_set_alias_empty_name_deletes(self):
        """set_alias: пустое имя удаляет псевдоним (санитизация не применяется к пустому)."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_00", "")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_set_alias_whitespace_only_treated_as_empty(self):
        """set_alias: имя из пробелов strip'd → пустое → удаляет псевдоним."""
        self.mgr.set_alias("SPEAKER_00", "Паша")
        self.mgr.set_alias("SPEAKER_00", "   ")
        self.assertIsNone(self.mgr.get_alias("SPEAKER_00"))

    def test_set_alias_clean_name_accepted(self):
        """set_alias: безопасное имя (кириллица, эмодзи) принимается без изменений."""
        self.mgr.set_alias("SPEAKER_00", "Александра 🎙")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Александра 🎙")

    def test_set_alias_strips_whitespace_from_name(self):
        """set_alias: ведущие/завершающие пробелы обрезаются перед сохранением."""
        self.mgr.set_alias("SPEAKER_00", "  Паша  ")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша")


# ---------------------------------------------------------------------------
# B1 — handle_set_speaker_alias IPC integration
# ---------------------------------------------------------------------------

class TestIPCHandlerInjectionGuard(unittest.TestCase):
    """B1: handle_set_speaker_alias rejects dangerous aliases via IPC."""

    def setUp(self):
        self.mgr = SpeakerManager()

    def test_ipc_rejects_crlf_in_name(self):
        """IPC set_speaker_alias: CRLF в имени → ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({
                "speaker_id": "SPEAKER_00",
                "name": "Паша\ninjection",
            })

    def test_ipc_rejects_pipe_in_name(self):
        """IPC set_speaker_alias: | в имени → ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({
                "speaker_id": "SPEAKER_00",
                "name": "Паша|hack",
            })

    def test_ipc_rejects_formula_injection(self):
        """IPC set_speaker_alias: имя начинающееся с '=' → ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({
                "speaker_id": "SPEAKER_00",
                "name": "=HYPERLINK(\"http://evil.com\")",
            })

    def test_ipc_rejects_too_long_name(self):
        """IPC set_speaker_alias: имя > 100 символов → ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.handle_set_speaker_alias({
                "speaker_id": "SPEAKER_00",
                "name": "А" * 101,
            })

    def test_ipc_accepts_clean_name(self):
        """IPC set_speaker_alias: безопасное имя принимается."""
        result = self.mgr.handle_set_speaker_alias({
            "speaker_id": "SPEAKER_00",
            "name": "Паша Иванов",
        })
        self.assertEqual(result["name"], "Паша Иванов")
        self.assertEqual(self.mgr.get_alias("SPEAKER_00"), "Паша Иванов")


# ---------------------------------------------------------------------------
# B1 — register_speaker alias sanitization
# ---------------------------------------------------------------------------

class TestRegisterSpeakerAliasSanitize(unittest.TestCase):
    """B1: register_speaker sanitises the name parameter."""

    def setUp(self):
        self.mgr = SpeakerManager()

    def _make_embedding(self):
        import numpy as np
        return np.ones(512, dtype=np.float32)

    def test_register_speaker_rejects_crlf_in_name(self):
        """register_speaker: CRLF в имени → ValueError, спикер не регистрируется."""
        with self.assertRaises(ValueError):
            self.mgr.register_speaker("Паша\ninjection", self._make_embedding())
        self.assertEqual(len(self.mgr.get_all_fingerprints()), 0)

    def test_register_speaker_rejects_formula_name(self):
        """register_speaker: имя начинающееся с '=' → ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.register_speaker("=EVIL", self._make_embedding())
        self.assertEqual(len(self.mgr.get_all_fingerprints()), 0)

    def test_register_speaker_rejects_too_long_name(self):
        """register_speaker: имя > 100 символов → ValueError."""
        with self.assertRaises(ValueError):
            self.mgr.register_speaker("А" * 101, self._make_embedding())

    def test_register_speaker_empty_name_allowed(self):
        """register_speaker: пустое имя разрешено (отсутствие псевдонима)."""
        sid = self.mgr.register_speaker("", self._make_embedding())
        self.assertIsNotNone(sid)
        # No alias should be stored for empty name
        self.assertIsNone(self.mgr.get_alias(sid))

    def test_register_speaker_clean_name_stored(self):
        """register_speaker: безопасное имя сохраняется как псевдоним."""
        sid = self.mgr.register_speaker("Паша", self._make_embedding())
        self.assertEqual(self.mgr.get_alias(sid), "Паша")


# ---------------------------------------------------------------------------
# B3 — MAX_SPEAKERS cap
# ---------------------------------------------------------------------------

class TestMaxSpeakersCap(unittest.TestCase):
    """B3: register_speaker raises ValueError when MAX_SPEAKERS is reached."""

    def test_max_speakers_constant_value(self):
        """MAX_SPEAKERS должен быть равен 10_000."""
        self.assertEqual(MAX_SPEAKERS, 10_000)

    def test_max_speakers_constant_exported(self):
        """MAX_SPEAKERS экспортируется из модуля."""
        import backend.speaker_manager as mod
        self.assertTrue(hasattr(mod, "MAX_SPEAKERS"))

    def test_register_speaker_raises_at_cap(self):
        """register_speaker поднимает ValueError при достижении MAX_SPEAKERS."""
        import numpy as np

        mgr = SpeakerManager()
        emb = np.ones(512, dtype=np.float32)
        # Manually inject fingerprints to simulate cap without registering 10k speakers
        for i in range(MAX_SPEAKERS):
            mgr._fingerprints[f"Speaker_{i}"] = emb.tolist()

        # Next call must raise
        with self.assertRaises(ValueError) as ctx:
            mgr.register_speaker("Лишний", emb)
        self.assertIn(str(MAX_SPEAKERS), str(ctx.exception))

    def test_register_speaker_at_cap_minus_one_succeeds(self):
        """register_speaker при cap-1 спикерах работает нормально."""
        import numpy as np

        mgr = SpeakerManager()
        emb = np.ones(512, dtype=np.float32)
        # Fill to cap - 1
        for i in range(MAX_SPEAKERS - 1):
            mgr._fingerprints[f"Speaker_{i}"] = emb.tolist()
        mgr._auto_speaker_counter = MAX_SPEAKERS - 1

        # Should succeed
        sid = mgr.register_speaker("Последний", emb)
        self.assertIsNotNone(sid)
        self.assertEqual(len(mgr.get_all_fingerprints()), MAX_SPEAKERS)

    def test_register_speaker_after_cap_blocked(self):
        """После удаления одного спикера регистрация опять разрешена."""
        import numpy as np

        mgr = SpeakerManager()
        emb = np.ones(512, dtype=np.float32)
        # Fill to cap
        for i in range(MAX_SPEAKERS):
            mgr._fingerprints[f"Speaker_{i}"] = emb.tolist()

        # Delete one fingerprint
        mgr.delete_fingerprint("Speaker_0")
        self.assertEqual(len(mgr.get_all_fingerprints()), MAX_SPEAKERS - 1)

        # Now registration should succeed
        mgr._auto_speaker_counter = MAX_SPEAKERS
        sid = mgr.register_speaker("Новый", emb)
        self.assertIsNotNone(sid)

    def test_register_speaker_cap_error_message_contains_limit(self):
        """Сообщение об ошибке при достижении лимита содержит значение MAX_SPEAKERS."""
        import numpy as np

        mgr = SpeakerManager()
        emb = np.ones(512, dtype=np.float32)
        for i in range(MAX_SPEAKERS):
            mgr._fingerprints[f"Speaker_{i}"] = emb.tolist()

        with self.assertRaises(ValueError) as ctx:
            mgr.register_speaker("", emb)
        self.assertIn(str(MAX_SPEAKERS), str(ctx.exception))


# ---------------------------------------------------------------------------
# B3 — IPC handle_register_speaker cap
# ---------------------------------------------------------------------------

class TestIPCRegisterSpeakerCap(unittest.TestCase):
    """B3: IPC handle_register_speaker respects MAX_SPEAKERS cap."""

    def test_ipc_raises_at_cap(self):
        """IPC register_speaker: ValueError при достижении MAX_SPEAKERS через IPC."""
        import numpy as np

        mgr = SpeakerManager()
        emb = np.ones(512, dtype=np.float32)
        for i in range(MAX_SPEAKERS):
            mgr._fingerprints[f"Speaker_{i}"] = emb.tolist()

        with self.assertRaises(ValueError):
            mgr.handle_register_speaker({
                "name": "DoS",
                "embedding": emb.tolist(),
            })


# ---------------------------------------------------------------------------
# Integration: persistence does not bypass sanitisation
# ---------------------------------------------------------------------------

class TestPersistenceDoesNotBypassSanitisation(unittest.TestCase):
    """Loaded aliases are stored as-is (existing data), but new aliases via API are guarded."""

    def test_set_alias_after_reload_enforces_guard(self):
        """После reload новые set_alias проходят санитизацию."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr1 = SpeakerManager(data_dir=tmpdir)
            mgr1.set_alias("SPEAKER_00", "Паша")

            mgr2 = SpeakerManager(data_dir=tmpdir)
            # New alias via API must still be sanitised
            with self.assertRaises(ValueError):
                mgr2.set_alias("SPEAKER_00", "Паша\nInject")
            # Old alias unchanged
            self.assertEqual(mgr2.get_alias("SPEAKER_00"), "Паша")


if __name__ == "__main__":
    unittest.main()
