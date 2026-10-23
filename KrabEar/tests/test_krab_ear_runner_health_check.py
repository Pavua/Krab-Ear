"""Контракт recovery-сообщений private Ear CI runner-а."""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "krab_ear_runner_health_check.py"


def load_module():
    # Скрипт сетевой: `import httpx` на верхнем уровне (строка 46). На
    # приватном runner-venv httpx нет, и exec_module падал ModuleNotFoundError
    # прямо на CI — красный backend-tests из-за файла, который этот PR не
    # трогает. Проверяем РОВНО тот импорт, что падает, и скипаем чисто
    # (playbook §1 про skip сторонних зависимостей).
    if importlib.util.find_spec("httpx") is None:
        raise unittest.SkipTest(
            "httpx отсутствует в окружении — сетевой скрипт не грузится"
        )
    spec = importlib.util.spec_from_file_location(
        "krab_ear_runner_health_check", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PrivateRunnerRecoveryMessageTest(unittest.TestCase):
    """Alert ведёт к private controller, а не к восстановлению public runner-а."""

    def test_actionable_message_targets_private_controller_only(self) -> None:
        mod = load_module()

        message = mod.runner_offline_message("krab-ear-m4max-private", "offline", 3)

        self.assertIn("Pavua/Krab-CI-Control", message)
        self.assertIn("actions-runner-krab-ci-control", message)
        self.assertNotIn("Pavua/Krab-Ear", message)
        self.assertNotIn("actions-runner-krab-ear", message)


class RunnerHTTPErrorRedactionTest(unittest.TestCase):
    def test_telegram_errors_hide_token_url_and_message(self):
        mod = load_module()
        marker = "SYNTHETIC_PRIVATE_MARKER"
        request = mod.httpx.Request(
            "POST", f"https://api.telegram.org/bot{marker}/sendMessage"
        )
        errors = [
            (
                mod.httpx.HTTPStatusError(
                    f"403 failed for {request.url}",
                    request=request,
                    response=mod.httpx.Response(403, request=request),
                ),
                "403",
            ),
            (
                mod.httpx.ConnectError(f"failed for {request.url}", request=request),
                "unknown",
            ),
        ]
        for error, status in errors:
            with self.subTest(error=type(error).__name__):
                stderr = io.StringIO()
                with (
                    patch.object(mod, "TG_TOKEN", marker),
                    patch.object(mod, "TG_CHAT", "synthetic-chat"),
                    patch.object(mod.httpx, "post", side_effect=error) as post,
                    redirect_stderr(stderr),
                ):
                    mod.send_telegram("synthetic alert")
                post.assert_called_once()
                log = stderr.getvalue()
                self.assertNotIn(marker, log)
                self.assertNotIn("api.telegram.org", log)
                self.assertIn(f"error_type={type(error).__name__}", log)
                self.assertIn(f"status={status}", log)

    def test_github_error_retains_exit2_without_raw_exception(self):
        mod = load_module()
        marker = "SYNTHETIC_PRIVATE_MARKER"
        request = mod.httpx.Request("GET", "https://api.github.com/synthetic")
        error = mod.httpx.HTTPStatusError(
            marker,
            request=request,
            response=mod.httpx.Response(401, request=request),
        )
        stderr = io.StringIO()
        with (
            patch.object(mod, "_load_dotenv"),
            patch.object(
                mod, "load_state", return_value={"streak": 0, "alerted": False}
            ),
            patch.object(mod, "fetch_runner_status", side_effect=error),
            patch.object(mod, "send_telegram") as send,
            patch.object(mod, "atomic_write_json") as persist,
            redirect_stderr(stderr),
        ):
            result = mod.main(["--dry-run"])
        self.assertEqual(result, 2)
        send.assert_not_called()
        persist.assert_not_called()
        self.assertNotIn(marker, stderr.getvalue())
        self.assertIn("error_type=HTTPStatusError status=401", stderr.getvalue())
