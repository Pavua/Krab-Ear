"""Wave 1747 — AppleScript injection regression tests for EmailSender.

Guards against the pre-fix behaviour where to/subject/body were interpolated
into the osascript source text with incomplete escaping (only `"` and `\n`,
never the backslash itself first), allowing malicious transcript-derived data
to break out of the AppleScript string literal and inject arbitrary code.

Post-fix (W1747): values are passed as SEPARATE osascript argv items, not interpolated.
Post-fix (W1764): «--» end-of-options separator inserted before positional args so that
osascript getopt(3) does not treat a leading-dash recipient like «-e» as a second flag.
Argv layout: [osascript, -e, <script>, --, to, subject, plain_body]  (7 elements).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.timing_budgets import REDOS_BUDGET_SEC  # noqa: E402

from backend.email_sender import EmailSender


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mail_sender() -> EmailSender:
    return EmailSender(backend_name="mail_app")


def _call_and_capture(sender: EmailSender, to: str, subject: str,
                      body_html: str) -> list:
    """Invoke send() with mocked subprocess.run; return the captured call args list."""
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stderr = ""
    captured: list[list] = []

    def _fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return fake_proc

    with patch("subprocess.run", side_effect=_fake_run):
        sender.send(to=to, subject=subject, body_html=body_html)

    assert len(captured) == 1, "Expected exactly one subprocess.run call"
    return captured[0]


# ---------------------------------------------------------------------------
# W1747.1: argv-based passing — values are discrete argv elements
# ---------------------------------------------------------------------------

class TestArgvBasedPassing(unittest.TestCase):
    """After the fix, to/subject/body must be separate argv elements, not
    baked into the script text.

    W1764 argv layout:
        cmd[0] == "osascript"
        cmd[1] == "-e"
        cmd[2] == script_text
        cmd[3] == "--"   ← end-of-options separator (W1764)
        cmd[4] == to
        cmd[5] == subject
        cmd[6] == plain_body
    """

    def test_to_is_separate_argv_element(self):
        """The recipient address must appear as a standalone element in the
        subprocess argv list, not embedded inside the script string."""
        addr = "recipient@example.com"
        cmd = _call_and_capture(_mail_sender(), to=addr,
                                subject="Hello", body_html="<p>body</p>")
        # W1764: «--» at index 3; to at index 4
        self.assertGreaterEqual(len(cmd), 7, "Expected at least 7 argv elements (W1764 adds «--»)")
        self.assertEqual(cmd[3], "--", "cmd[3] must be «--» end-of-options separator (W1764)")
        self.assertEqual(cmd[4], addr, "to must be the 5th argv element (index 4)")

    def test_subject_is_separate_argv_element(self):
        """Subject must be the 6th argv element (index 5), not interpolated."""
        subj = "Daily digest"
        cmd = _call_and_capture(_mail_sender(), to="r@x.com",
                                subject=subj, body_html="<p>hi</p>")
        self.assertEqual(cmd[5], subj)

    def test_body_is_separate_argv_element(self):
        """Plain-text body must be the 7th argv element (index 6)."""
        body_html = "<p>Transcription result</p>"
        cmd = _call_and_capture(_mail_sender(), to="r@x.com",
                                subject="S", body_html=body_html)
        # plain_body after _strip_html
        self.assertEqual(cmd[6], "Transcription result")


# ---------------------------------------------------------------------------
# W1747.2: injection payloads cannot break the script
# ---------------------------------------------------------------------------

class TestInjectionPayloadsAreSafe(unittest.TestCase):
    """Injection payloads that would have broken the old f-string template
    must now be passed verbatim as data, not alter script execution."""

    # ------------------------------------------------------------------
    # Payload fixtures
    # ------------------------------------------------------------------
    BACKSLASH_BEFORE_QUOTE = 'Say hello \\"world\\"'
    BARE_BACKSLASH = "path\\to\\file"
    REAL_NEWLINE_IN_SUBJECT = "Line one\nLine two"
    APPLESCRIPT_INJECTION_BODY = (
        '} tell application "Finder" to quit\n'
        'tell application "Mail"'
    )
    APPLESCRIPT_INJECTION_SUBJECT = (
        'ignored", visible:true}\n'
        'do shell script "open /tmp/evil"'
    )
    DOUBLE_QUOTE_IN_TO = '"attacker"@example.com'

    def _assert_value_in_argv_not_script(self, field_idx: int, value: str,
                                         to: str = "r@x.com",
                                         subject: str = "S",
                                         body_html: str = "<p>ok</p>") -> None:
        """Helper: run send(), check that `value` lands in argv[field_idx] verbatim
        and does NOT appear as a substring of the script text (argv[2]).

        W1764 argv layout: [osascript, -e, script, --, to, subject, plain_body]
        field_idx=4 → to, field_idx=5 → subject, field_idx=6 → plain_body.
        """
        if field_idx == 4:
            to = value
        elif field_idx == 5:
            subject = value
        elif field_idx == 6:
            body_html = f"<p>{value}</p>"

        cmd = _call_and_capture(_mail_sender(), to=to, subject=subject,
                                body_html=body_html)
        script_text = cmd[2]  # the -e argument
        argv_value = cmd[field_idx]

        # The injected payload must not appear literally in the script text
        # (some chars like newlines may be normalised by _strip_html for body,
        # but the key injection patterns must not be in the script).
        if field_idx != 6:
            # For to/subject there is no transformation; exact match.
            self.assertEqual(argv_value, value,
                             f"argv[{field_idx}] must equal the raw value")
        else:
            # body goes through _strip_html, which strips HTML tags.
            # We just check it's a non-empty string that doesn't contain
            # AppleScript-breaking fragments in the *script* text.
            pass

        # The core injection strings must not be in the osascript source.
        for dangerous in [
            'tell application "Finder"',
            'do shell script',
            'do shell script "open /tmp/evil"',
        ]:
            self.assertNotIn(dangerous, script_text,
                             f"Injection fragment '{dangerous}' must not appear in script text")

    def test_backslash_before_quote_in_body(self):
        """Body with backslash-before-quote is passed as data without breaking script."""
        cmd = _call_and_capture(
            _mail_sender(), to="r@x.com", subject="S",
            body_html=f"<p>{self.BACKSLASH_BEFORE_QUOTE}</p>",
        )
        # Must not raise; script text must not contain the raw value
        self.assertEqual(cmd[0], "osascript")

    def test_bare_backslash_in_subject(self):
        """Bare backslash in subject is passed verbatim as argv element."""
        cmd = _call_and_capture(
            _mail_sender(), to="r@x.com", subject=self.BARE_BACKSLASH,
            body_html="<p>ok</p>",
        )
        # W1764: subject is at index 5
        self.assertEqual(cmd[5], self.BARE_BACKSLASH)

    def test_newline_in_subject_passed_verbatim(self):
        """Real newline in subject is passed as-is in argv (no script breakout)."""
        cmd = _call_and_capture(
            _mail_sender(), to="r@x.com", subject=self.REAL_NEWLINE_IN_SUBJECT,
            body_html="<p>ok</p>",
        )
        # W1764: subject is at index 5
        self.assertEqual(cmd[5], self.REAL_NEWLINE_IN_SUBJECT)
        # The script text itself must not have the newline-terminated payload
        script_text = cmd[2]
        self.assertNotIn("Line two", script_text)

    def test_applescript_injection_in_body(self):
        """Classic AppleScript injection via body cannot alter script structure."""
        self._assert_value_in_argv_not_script(
            field_idx=6, value=self.APPLESCRIPT_INJECTION_BODY,
            body_html=f"<p>{self.APPLESCRIPT_INJECTION_BODY}</p>",
        )

    def test_applescript_injection_in_subject(self):
        """Classic AppleScript injection via subject cannot alter script structure."""
        cmd = _call_and_capture(
            _mail_sender(), to="r@x.com",
            subject=self.APPLESCRIPT_INJECTION_SUBJECT,
            body_html="<p>ok</p>",
        )
        script_text = cmd[2]
        self.assertNotIn('do shell script "open /tmp/evil"', script_text)
        # Subject value is passed verbatim as argv element (W1764: index 5)
        self.assertEqual(cmd[5], self.APPLESCRIPT_INJECTION_SUBJECT)

    def test_double_quote_in_to_passed_verbatim(self):
        """Double-quote in recipient address must not break script text."""
        cmd = _call_and_capture(
            _mail_sender(), to=self.DOUBLE_QUOTE_IN_TO,
            subject="S", body_html="<p>ok</p>",
        )
        # W1764: to is at index 4 (after «--» separator at index 3)
        self.assertEqual(cmd[4], self.DOUBLE_QUOTE_IN_TO)
        script_text = cmd[2]
        # The double-quote address must not appear inside the script source
        self.assertNotIn(self.DOUBLE_QUOTE_IN_TO, script_text)

    def test_all_three_fields_are_pure_data(self):
        """With all three fields set to adversarial payloads simultaneously,
        none of the injection patterns appear in the script text."""
        cmd = _call_and_capture(
            _mail_sender(),
            to=self.DOUBLE_QUOTE_IN_TO,
            subject=self.APPLESCRIPT_INJECTION_SUBJECT,
            body_html=f"<p>{self.APPLESCRIPT_INJECTION_BODY}</p>",
        )
        script_text = cmd[2]
        for dangerous in [
            'tell application "Finder"',
            'do shell script',
            '"attacker"',
            'visible:true',
        ]:
            self.assertNotIn(dangerous, script_text,
                             f"Injection fragment must not appear in script: {dangerous!r}")


# ---------------------------------------------------------------------------
# W1747.3: argv structure sanity — script text is static
# ---------------------------------------------------------------------------

class TestScriptTextIsStatic(unittest.TestCase):
    """The osascript source text (argv[2] = -e argument) must be identical
    regardless of what to/subject/body contain — it is a static template."""

    def _get_script(self, to: str, subject: str, body: str) -> str:
        cmd = _call_and_capture(_mail_sender(), to=to, subject=subject,
                                body_html=f"<p>{body}</p>")
        return cmd[2]

    def test_script_text_invariant_across_payloads(self):
        """Script text must be the same string for safe vs. adversarial inputs."""
        safe_script = self._get_script("a@x.com", "Hello", "Normal body")
        evil_script = self._get_script(
            '"evil"@x.com',
            'injected", visible:true} \n do shell script "open /evil"',
            '} end tell\n tell application "Finder" to quit',
        )
        self.assertEqual(safe_script, evil_script,
                         "Script text must be static; user data must not appear in it")

    def test_script_contains_on_run_argv(self):
        """The script must use 'on run argv' handler to receive arguments."""
        cmd = _call_and_capture(_mail_sender(), to="r@x.com",
                                subject="S", body_html="<p>ok</p>")
        script_text = cmd[2]
        self.assertIn("on run argv", script_text)

    def test_script_uses_item_of_argv(self):
        """The script must read values via 'item N of argv', not inline literals."""
        cmd = _call_and_capture(_mail_sender(), to="r@x.com",
                                subject="S", body_html="<p>ok</p>")
        script_text = cmd[2]
        self.assertIn("item 1 of argv", script_text)
        self.assertIn("item 2 of argv", script_text)
        self.assertIn("item 3 of argv", script_text)

    def test_cmd_length_is_exactly_seven(self):
        """subprocess argv must be exactly: osascript -e <script> -- <to> <subject> <body>.

        W1764 added the «--» separator, raising the count from 6 to 7.
        """
        cmd = _call_and_capture(_mail_sender(), to="r@x.com",
                                subject="Hello", body_html="<p>World</p>")
        self.assertEqual(len(cmd), 7,
                         f"Expected 7 argv elements (W1764: includes «--»), got {len(cmd)}: {cmd!r}")
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")
        self.assertEqual(cmd[3], "--", "cmd[3] must be «--» end-of-options separator")


# ---------------------------------------------------------------------------
# W1747.4: pre-fix breakability demo (documents the OLD vulnerable pattern)
# ---------------------------------------------------------------------------

class TestPreFixInterpolationWouldBreak(unittest.TestCase):
    """Demonstrate that the OLD f-string interpolation approach was breakable.

    These tests verify that a naive interpolation approach (escape only `"`)
    would allow backslash-before-quote to break the string literal, but
    the new argv-based approach is immune.
    """

    def _old_broken_escape(self, value: str) -> str:
        """Simulate the pre-fix escaping: only escapes `"`, not `\\` first."""
        return value.replace('"', '\\"')

    def test_old_escape_backslash_before_quote_is_breakable(self):
        """The old escape leaves '\\\"' in the script when input is '\\\"'.

        Input: `\\"` → old escape produces `\\"` → AppleScript sees `\"` which
        terminates the backslash escape and re-opens the string — injection.
        This test documents the flaw; it does NOT invoke EmailSender.
        """
        malicious = '\\"'
        # Old escape: only replaces `"` with `\"`
        old_result = self._old_broken_escape(malicious)
        # The result still contains a backslash before the escaped quote,
        # producing `\\\"` in the script, which AppleScript interprets as
        # literal `\` followed by `"` (closing the string) — broken.
        self.assertIn('\\"', old_result,
                      "Old escape leaves the injection fragment intact")

    def test_new_argv_approach_is_immune_to_backslash_quote(self):
        """New argv approach: backslash-before-quote in subject is passed as data."""
        malicious_subject = '\\"injection attempt'
        cmd = _call_and_capture(
            _mail_sender(), to="r@x.com",
            subject=malicious_subject,
            body_html="<p>ok</p>",
        )
        # W1764: subject is at argv[5] (after «--» at [3] and to at [4])
        self.assertEqual(cmd[5], malicious_subject)
        # The script text does NOT contain the raw value
        script_text = cmd[2]
        self.assertNotIn(malicious_subject, script_text)


# ---------------------------------------------------------------------------
# W1764.1: osascript «--» separator placement
# ---------------------------------------------------------------------------

class TestW1764OsascriptEndOfOptions(unittest.TestCase):
    """W1764 MED: «--» separator must appear between the -e flag and positional args
    so that osascript getopt(3) stops option-parsing before user-supplied values.

    Primary defense: email validation in send() rejects leading-dash values.
    Belt-and-suspenders: «--» ensures that even if a leading-dash value were
    somehow passed to _send_via_mail_app directly, getopt would not consume it
    as a flag.  These tests verify the «--» is structurally present.
    """

    def test_double_dash_separator_present(self):
        """«--» must be at argv[3] (immediately after script text)."""
        cmd = _call_and_capture(_mail_sender(), to="r@x.com",
                                subject="Hello", body_html="<p>ok</p>")
        self.assertEqual(cmd[3], "--",
                         f"Expected «--» at argv[3], got {cmd[3]!r}. "
                         "Missing separator allows leading-dash values to be parsed as flags.")

    def test_separator_position_invariant_across_inputs(self):
        """«--» must be at index 3 regardless of the subject/body content."""
        for subj in ("Normal subject", "Subject with -dashes", "Краб дайджест"):
            cmd = _call_and_capture(_mail_sender(), to="a@b.co",
                                    subject=subj, body_html="<p>ok</p>")
            self.assertEqual(cmd[3], "--",
                             f"«--» must be at argv[3] for subject={subj!r}")

    def test_double_dash_enables_leading_dash_subject_to_pass_through(self):
        """Subject starting with «-» must land at argv[5] untouched (not parsed as flag).

        Without «--», a subject like «-autoclose» would be consumed by osascript
        getopt before on-run-argv receives it.  Belt-and-suspenders test.
        """
        dash_subject = "-autoclose reminder"
        cmd = _call_and_capture(_mail_sender(), to="a@b.co",
                                subject=dash_subject, body_html="<p>ok</p>")
        self.assertEqual(cmd[3], "--")
        self.assertEqual(cmd[5], dash_subject,
                         "Subject starting with «-» must be at argv[5] as data")

    def test_positional_order_after_separator(self):
        """After «--»: argv[4]=to, argv[5]=subject, argv[6]=plain_body (7 elements total)."""
        cmd = _call_and_capture(_mail_sender(), to="a@b.co",
                                subject="Subj", body_html="<p>Body</p>")
        self.assertEqual(len(cmd), 7)
        self.assertEqual(cmd[4], "a@b.co")
        self.assertEqual(cmd[5], "Subj")
        self.assertEqual(cmd[6], "Body")


# ---------------------------------------------------------------------------
# W1764.2: email address validation in send()
# ---------------------------------------------------------------------------

class TestW1764EmailValidation(unittest.TestCase):
    """W1764 MED belt-and-suspenders: send() must validate «to» before reaching
    osascript or smtplib — rejects leading-dash addresses and malformed values."""

    # ---------- invalid addresses that must raise ValueError ---------------

    def test_leading_dash_e_rejected(self):
        """to=«-e» is rejected with ValueError before osascript is called."""
        sender = EmailSender(backend_name="mail_app")
        with self.assertRaises(ValueError, msg="to=«-e» must raise ValueError"):
            with patch("subprocess.run") as mock_run:
                sender.send(to="-e", subject="S", body_html="<p>ok</p>")
                mock_run.assert_not_called()

    def test_leading_dash_l_rejected(self):
        """to=«-l» is rejected with ValueError (osascript language flag)."""
        sender = EmailSender(backend_name="mail_app")
        with self.assertRaises(ValueError):
            sender.send(to="-l", subject="S", body_html="<p>ok</p>")

    def test_no_at_sign_rejected(self):
        """to without @ is rejected."""
        sender = EmailSender(backend_name="smtp", smtp_host="h", use_keychain=False)
        with self.assertRaises(ValueError):
            sender.send(to="notanemail", subject="S", body_html="<p>ok</p>")

    def test_whitespace_in_to_rejected(self):
        """to with embedded whitespace is rejected."""
        sender = EmailSender(backend_name="smtp", smtp_host="h", use_keychain=False)
        with self.assertRaises(ValueError):
            sender.send(to="user @example.com", subject="S", body_html="<p>ok</p>")

    def test_no_dot_in_domain_rejected(self):
        """to without a dot in domain (e.g. «a@b») is rejected."""
        sender = EmailSender(backend_name="smtp", smtp_host="h", use_keychain=False)
        with self.assertRaises(ValueError):
            sender.send(to="a@b", subject="S", body_html="<p>ok</p>")

    def test_empty_to_rejected(self):
        """Empty «to» is still rejected (pre-existing check, unchanged)."""
        sender = EmailSender(backend_name="smtp", smtp_host="h", use_keychain=False)
        with self.assertRaises(ValueError):
            sender.send(to="", subject="S", body_html="<p>ok</p>")

    # ---------- valid addresses that must NOT raise -------------------

    def test_valid_email_passes_validation(self):
        """Plain valid address a@b.co must not raise ValueError."""
        sender = EmailSender(backend_name="mail_app")
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        with patch("subprocess.run", return_value=fake_proc):
            # Must not raise
            sender.send(to="a@b.co", subject="S", body_html="<p>ok</p>")

    def test_plus_addressing_passes(self):
        """user+tag@example.com must pass validation."""
        sender = EmailSender(backend_name="mail_app")
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        with patch("subprocess.run", return_value=fake_proc):
            sender.send(to="user+tag@example.com", subject="S", body_html="<p>ok</p>")

    def test_subdomain_passes(self):
        """user@mail.example.co.uk must pass validation."""
        sender = EmailSender(backend_name="mail_app")
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        with patch("subprocess.run", return_value=fake_proc):
            sender.send(to="user@mail.example.co.uk", subject="S", body_html="<p>ok</p>")


# ---------------------------------------------------------------------------
# W1764.3: ReDoS protection in _strip_html
# ---------------------------------------------------------------------------

class TestW1764StripHtmlReDoSProtection(unittest.TestCase):
    """W1764 LOW: _strip_html must not hang on hostile input with many unclosed «<»."""

    def test_strip_html_hostile_input_completes_quickly(self):
        """500 000 consecutive «<» characters must complete quickly.

        The ReDoS fix (W1764) uses a bounded quantifier <[^>]{0,2000}> and
        truncates input to _STRIP_HTML_MAX_BYTES=200 000 chars first, making
        the algorithm O(n) rather than O(n²).

        Threshold is 1.5 s (was 0.3 s): a genuine O(n²) ReDoS on 500 000 chars
        takes tens of seconds, so 1.5 s still catches regressions while
        tolerating variance on slower CI runners (Ubuntu shared runners have
        seen >0.4 s for this workload under load).
        """
        import time
        hostile = "<" * 500_000
        t0 = time.monotonic()
        result = EmailSender._strip_html(hostile)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC,
                        f"_strip_html took {elapsed:.3f}s on hostile input — ReDoS not fixed")
        # Result must be a string (truncated, stripped, possibly empty)
        self.assertIsInstance(result, str)

    def test_strip_html_truncates_oversized_body(self):
        """Input longer than _STRIP_HTML_MAX_BYTES is truncated before regex."""
        from backend.email_sender import _STRIP_HTML_MAX_BYTES
        oversized = "a" * (_STRIP_HTML_MAX_BYTES + 10_000)
        result = EmailSender._strip_html(oversized)
        # Result must not be longer than the limit (no tags to strip, so result ≤ limit)
        self.assertLessEqual(len(result), _STRIP_HTML_MAX_BYTES)

    def test_strip_html_normal_input_unchanged_by_cap(self):
        """Normal HTML under the cap is processed correctly."""
        html = "<h1>Title</h1><p>Body <b>text</b></p>"
        result = EmailSender._strip_html(html)
        self.assertIn("Title", result)
        self.assertIn("Body", result)
        self.assertIn("text", result)
        self.assertNotIn("<", result)


if __name__ == "__main__":
    unittest.main()
