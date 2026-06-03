# -*- coding: utf-8 -*-
"""W1211 — telegram_bridge: privacy guard on list_chats + 4096-char cap.

Covers:
  F2 MED: handle_list_telegram_chats must respect privacy_mode_enabled.
  F3 MED: TelegramBridge.send_message() must truncate text > 4096 UTF-8 chars.

Tests use AST-level verification for apple_integration_service.py (the live
home of handle_list_telegram_chats after the W797 in-class duplicate removal,
#47) and runtime tests for telegram_bridge.py which has no such constraint.
"""

from __future__ import annotations

import ast
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.telegram_bridge import TelegramBridge


# ---------------------------------------------------------------------------
# F2 — privacy guard on handle_list_telegram_chats (AST verification)
# ---------------------------------------------------------------------------


class TestListChatsPrivacyGuardAST(unittest.TestCase):
    """AST-verify that handle_list_telegram_chats contains the privacy guard.

    W797 follow-up (#47): the in-class BackendService._handle_list_telegram_chats
    duplicate was deleted. The live handler now lives in
    backend/apple_integration_service.py as handle_list_telegram_chats — these
    AST checks point at that module / method.
    """

    _SERVICE_PATH = os.path.join(PROJECT_ROOT, "backend", "apple_integration_service.py")
    _METHOD = "handle_list_telegram_chats"

    def _load_ast(self) -> ast.Module:
        with open(self._SERVICE_PATH, encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=self._SERVICE_PATH)

    def _find_method_body(self, tree: ast.Module, method_name: str) -> list[ast.stmt]:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == method_name:
                    return node.body
        return []

    def test_privacy_guard_present_in_handle_list_telegram_chats(self) -> None:
        """handle_list_telegram_chats must gate on 'privacy_mode_enabled'."""
        tree = self._load_ast()
        body = self._find_method_body(tree, self._METHOD)
        self.assertTrue(body, f"{self._METHOD} not found in apple_integration_service.py")

        full_dump = " ".join(ast.dump(stmt) for stmt in body)
        self.assertIn(
            "privacy_mode_enabled",
            full_dump,
            f"{self._METHOD} is missing privacy_mode_enabled guard",
        )

    def test_privacy_guard_returns_skipped_key(self) -> None:
        """The early-return under privacy_mode must include key 'skipped'."""
        tree = self._load_ast()
        body = self._find_method_body(tree, self._METHOD)
        self.assertTrue(body, f"{self._METHOD} not found in apple_integration_service.py")

        found_skipped = False
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and key.value == "skipped":
                        found_skipped = True
                        break
        self.assertTrue(
            found_skipped,
            f"No Return({{..., 'skipped': ...}}) found in {self._METHOD}",
        )

    def test_list_chats_skipped_in_privacy_mode(self) -> None:
        """AST: early-return dict must contain chats key with empty list literal."""
        tree = self._load_ast()
        body = self._find_method_body(tree, self._METHOD)
        self.assertTrue(body, f"{self._METHOD} not found in apple_integration_service.py")

        # Find the if-block that checks privacy_mode_enabled and verify it returns
        # a dict with an empty list for 'chats'.
        found = False
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if not isinstance(node, ast.If):
                continue
            # Check the condition contains 'privacy_mode_enabled'
            cond_dump = ast.dump(node.test)
            if "privacy_mode_enabled" not in cond_dump:
                continue
            # Inside this If, look for a Return with chats: []
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                    for k, v in zip(child.value.keys, child.value.values):
                        if isinstance(k, ast.Constant) and k.value == "chats":
                            if isinstance(v, ast.List) and len(v.elts) == 0:
                                found = True
                                break
        self.assertTrue(
            found,
            f"privacy_mode If-block in {self._METHOD} must return "
            "{'chats': [], ...}",
        )

    def test_list_chats_returns_chats_normally(self) -> None:
        """AST: handle_list_telegram_chats must have a normal return path with 'chats' key."""
        tree = self._load_ast()
        body = self._find_method_body(tree, self._METHOD)
        self.assertTrue(body, f"{self._METHOD} not found in apple_integration_service.py")

        # Find a Return node with {'chats': ...} that is NOT inside a privacy if-block.
        # We look for at least one Return whose dict value for 'chats' is NOT a list literal.
        found_normal_return = False
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and k.value == "chats":
                        # The normal return uses a Name node (a variable), not an empty list
                        if isinstance(v, ast.Name):
                            found_normal_return = True
                            break
        self.assertTrue(
            found_normal_return,
            f"{self._METHOD} is missing a normal return {{'chats': <var>}} path",
        )


# ---------------------------------------------------------------------------
# F3 — 4096-char cap in TelegramBridge.send_message() (runtime + AST)
# ---------------------------------------------------------------------------


def _make_ok_response() -> MagicMock:
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {
        "ok": True,
        "message_id": 1,
        "sent_at": 1700000000.0,
        "chat_title": "TestChat",
    }
    return resp


class TestSendMessageCharCap(unittest.TestCase):
    """send_message() must truncate text > 4096 chars (UTF-8 char count, not bytes)."""

    @patch("requests.post")
    def test_send_message_truncates_above_4096(self, mock_post: MagicMock) -> None:
        """Text of 5000 chars must be truncated to 4096 chars total (4093 + '...')."""
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()

        long_text = "А" * 5000  # Cyrillic А — 1 char, 2 bytes in UTF-8
        bridge.send_message(text=long_text, chat_id=123)

        _, kwargs = mock_post.call_args
        sent_text: str = kwargs["json"]["text"]
        self.assertEqual(len(sent_text), 4096)
        self.assertTrue(sent_text.endswith("..."))
        self.assertEqual(sent_text[:4093], "А" * 4093)

    @patch("requests.post")
    def test_send_message_4096_exact_passes_through(self, mock_post: MagicMock) -> None:
        """Text of exactly 4096 chars must NOT be truncated."""
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()

        exact_text = "B" * 4096
        bridge.send_message(text=exact_text, chat_id=123)

        _, kwargs = mock_post.call_args
        sent_text: str = kwargs["json"]["text"]
        self.assertEqual(len(sent_text), 4096)
        self.assertFalse(sent_text.endswith("..."))

    @patch("requests.post")
    def test_send_message_short_text_passes_through(self, mock_post: MagicMock) -> None:
        """Short text well below 4096 chars must be sent unmodified."""
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()

        short_text = "Привет, мир!"
        bridge.send_message(text=short_text, chat_id=123)

        _, kwargs = mock_post.call_args
        sent_text: str = kwargs["json"]["text"]
        self.assertEqual(sent_text, short_text)

    @patch("requests.post")
    def test_send_message_4097_triggers_truncation(self, mock_post: MagicMock) -> None:
        """Text of 4097 chars must be truncated."""
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()

        text_4097 = "X" * 4097
        bridge.send_message(text=text_4097, chat_id=123)

        _, kwargs = mock_post.call_args
        sent_text: str = kwargs["json"]["text"]
        self.assertEqual(len(sent_text), 4096)
        self.assertTrue(sent_text.endswith("..."))


# ---------------------------------------------------------------------------
# AST-level check: truncation guard present in telegram_bridge.py
# ---------------------------------------------------------------------------


class TestSendMessageCapAST(unittest.TestCase):
    """AST-verify that send_message() contains the 4096-char truncation guard."""

    _BRIDGE_PATH = os.path.join(PROJECT_ROOT, "backend", "telegram_bridge.py")

    def _load_ast(self) -> ast.Module:
        with open(self._BRIDGE_PATH, encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=self._BRIDGE_PATH)

    def test_cap_constant_4096_in_send_message(self) -> None:
        """send_message() AST must contain the constant 4096."""
        tree = self._load_ast()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "send_message":
                    for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                        if isinstance(child, ast.Constant) and child.value == 4096:
                            return  # found
                    self.fail("Constant 4096 not found in send_message() body")
        self.fail("send_message() method not found in telegram_bridge.py")

    def test_ellipsis_suffix_in_send_message(self) -> None:
        """send_message() AST must contain the '...' suffix string constant."""
        tree = self._load_ast()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "send_message":
                    for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                        if isinstance(child, ast.Constant) and child.value == "...":
                            return  # found
                    self.fail("String constant '...' not found in send_message() body")
        self.fail("send_message() method not found in telegram_bridge.py")


if __name__ == "__main__":
    unittest.main()
