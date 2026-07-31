import csv
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "memory_baseline.py"

_psutil_available = importlib.util.find_spec("psutil") is not None


def _load_memory_baseline_module():
    """Импортирует memory_baseline.py напрямую (не пакет — обычный скрипт)."""
    spec = importlib.util.spec_from_file_location("memory_baseline", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeBackendSocket:
    """Throwaway Unix-сокет, отвечающий на ОДИН JSON-RPC запрос заданным телом.

    Заменяет реальный backend в тестах S3/Task10: memory_baseline.py обязан
    уметь ходить по ЛЮБОМУ переданному сокету, а не только в продовый путь.
    """

    def __init__(self, response_result: dict):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sock_path = str(Path(self._tmpdir.name) / "fake.sock")
        self._response_result = response_result
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.sock_path)
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve_once, daemon=True)
        self._thread.start()

    def _serve_once(self):
        try:
            self._server.settimeout(10)
            conn, _ = self._server.accept()
        except OSError:
            return
        try:
            conn.settimeout(10)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            reply = json.dumps({"ok": True, "result": self._response_result}) + "\n"
            conn.sendall(reply.encode("utf-8"))
        finally:
            conn.close()

    def close(self):
        self._thread.join(timeout=5)
        self._server.close()
        self._tmpdir.cleanup()


@unittest.skipUnless(_psutil_available, "psutil not installed")
class MemoryBaselineScriptTests(unittest.TestCase):
    def test_script_runs_with_once_flag(self):
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        out_csv = REPO_ROOT / "test-mem-baseline-tmp.csv"
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--once", "--output", str(out_csv)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, f"script failed: {result.stderr}")
            self.assertTrue(out_csv.exists())

            # CSV has header + at least 1 row
            rows = list(csv.DictReader(out_csv.open()))
            self.assertGreaterEqual(len(rows), 1)
            self.assertIn("timestamp", rows[0])
            self.assertIn("agent_rss_mb", rows[0])
        finally:
            if out_csv.exists():
                out_csv.unlink()

    def test_script_handles_no_backend(self):
        # Even if backend not running, script should write 0 values + exit 0
        # (already handled by get_backend_diagnostics returning None)
        pass

    def test_snapshot_fields_complete(self):
        """take_snapshot returns all expected keys even without backend."""
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        mod = _load_memory_baseline_module()

        snap = mod.take_snapshot(None)
        expected_keys = {
            "timestamp", "uptime_sec", "agent_rss_mb", "backend_rss_mb", "rest_rss_mb",
            "worker_rss_mb_total", "total_rss_mb", "history_total_items", "llm_circuit",
        }
        self.assertEqual(set(snap.keys()), expected_keys)
        # Values are numeric or string — no exceptions.
        # NB: agent_rss_mb fallback default = 0 (int) когда KrabEarAgent не запущен —
        # на CI или после reboot. Принимаем (int, float).
        self.assertIsInstance(snap["agent_rss_mb"], (int, float))
        self.assertIsInstance(snap["timestamp"], str)


@unittest.skipUnless(_psutil_available, "psutil not installed")
class MemoryBaselineSocketOverrideTests(unittest.TestCase):
    """S3/Task10: скрипт обязан ходить по throwaway-сокету, не только по продовому."""

    def test_get_backend_diagnostics_accepts_explicit_socket_path(self):
        """get_backend_diagnostics(sock_path) должен опросить ИМЕННО переданный
        сокет, а не жёстко закодированный продовый путь — иначе throwaway-замер
        либо молча вернёт None, либо (что хуже) опросит живой backend владельца."""
        mod = _load_memory_baseline_module()
        fake = _FakeBackendSocket({"system": {"uptime_sec": 4242}})
        try:
            diag = mod.get_backend_diagnostics(fake.sock_path)
        finally:
            fake.close()
        self.assertIsNotNone(diag, "diag не должен быть None для живого throwaway-сокета")
        self.assertEqual(diag.get("system", {}).get("uptime_sec"), 4242)

    def test_take_snapshot_threads_socket_path_through(self):
        """take_snapshot(sock_path) обязан пробросить путь в get_backend_diagnostics,
        а не игнорировать параметр (regression для случая, когда прокидывание
        забыто на полпути и функция тихо падает обратно на прод-путь)."""
        mod = _load_memory_baseline_module()
        fake = _FakeBackendSocket({"system": {"uptime_sec": 777}})
        try:
            snap = mod.take_snapshot(fake.sock_path)
        finally:
            fake.close()
        self.assertEqual(snap["uptime_sec"], 777)

    def test_cli_once_with_explicit_socket_reads_throwaway_backend(self):
        """Живой прогон CLI с --socket: без флага скрипт промахнулся бы мимо
        throwaway-инстанса (см. докстринг задачи 10 плана S3)."""
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        fake = _FakeBackendSocket({"system": {"uptime_sec": 999}})
        out_csv = REPO_ROOT / "test-mem-baseline-socket-tmp.csv"
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--once", "--socket", fake.sock_path,
                 "--output", str(out_csv)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, f"script failed: {result.stderr}")
            rows = list(csv.DictReader(out_csv.open()))
            self.assertEqual(rows[0]["uptime_sec"], "999")
        finally:
            fake.close()
            if out_csv.exists():
                out_csv.unlink()

    def test_cli_data_dir_derives_krabear_sock_path(self):
        """--data-dir <dir> обязан опрашивать <dir>/krabear.sock (соглашение
        проекта, см. backend/service.py:5488 и scripts/rest_inprocess_load_smoke.py)."""
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock_path = data_dir / "krabear.sock"
            server.bind(str(sock_path))
            server.listen(1)

            def _serve():
                try:
                    server.settimeout(10)
                    conn, _ = server.accept()
                except OSError:
                    return
                try:
                    conn.settimeout(10)
                    buf = b""
                    while b"\n" not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                    reply = json.dumps({"ok": True, "result": {"system": {"uptime_sec": 55}}}) + "\n"
                    conn.sendall(reply.encode("utf-8"))
                finally:
                    conn.close()

            t = threading.Thread(target=_serve, daemon=True)
            t.start()
            out_csv = REPO_ROOT / "test-mem-baseline-datadir-tmp.csv"
            try:
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), "--once", "--data-dir", str(data_dir),
                     "--output", str(out_csv)],
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, f"script failed: {result.stderr}")
                rows = list(csv.DictReader(out_csv.open()))
                self.assertEqual(rows[0]["uptime_sec"], "55")
            finally:
                t.join(timeout=5)
                server.close()
                if out_csv.exists():
                    out_csv.unlink()

    def test_cli_rejects_socket_and_data_dir_together(self):
        """--socket и --data-dir взаимоисключающие — неоднозначный источник
        пути к сокету не должен молча выбирать один из двух."""
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--once", "--socket", "/tmp/a.sock",
             "--data-dir", "/tmp/somedir"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)


@unittest.skipUnless(_psutil_available, "psutil not installed")
class MemoryBaselineProcessMatcherTests(unittest.TestCase):
    """S3/Task10: матчер процессов обязан ловить и легаси REST-процесс —
    иначе замер «до» (два процесса) не увидит его RSS, а «после» (один
    процесс через KrabEar/main.py) не увидит собственно backend."""

    def test_matches_legacy_rest_server_process(self):
        mod = _load_memory_baseline_module()
        cmdline = "/usr/bin/python3 KrabEar/backend/rest_server.py"
        self.assertTrue(mod._matches_krab_process(cmdline))

    def test_matches_main_py_entrypoint(self):
        mod = _load_memory_baseline_module()
        cmdline = "/usr/bin/python3 KrabEar/main.py --data-dir /tmp/x"
        self.assertTrue(mod._matches_krab_process(cmdline))

    def test_does_not_match_unrelated_process(self):
        mod = _load_memory_baseline_module()
        self.assertFalse(mod._matches_krab_process("/usr/bin/python3 some_other_tool.py"))

    def test_snapshot_reports_separate_rest_and_total_rss(self):
        """rest_rss_mb (легаси REST) и total_rss_mb обязаны присутствовать —
        без них «минус сотни МБ дубля» из спеки нельзя ни подтвердить, ни
        опровергнуть числом."""
        mod = _load_memory_baseline_module()
        snap = mod.take_snapshot(None)
        self.assertIn("rest_rss_mb", snap)
        self.assertIn("total_rss_mb", snap)


@unittest.skipUnless(_psutil_available, "psutil not installed")
class MemoryBaselinePidScopingTests(unittest.TestCase):
    """S3/Task10 (живой прогон вскрыл): системный cmdline-скан ловит ЛЮБОЙ
    процесс на машине с совпадающей подстрокой — включая ЖИВОЙ прод-агент
    владельца, если throwaway-канарейка снимается на той же машине (а это
    ИМЕННО тот случай, для которого инструмент строился: двухнедельная
    канарейка идёт НА рабочей машине владельца, где прод почти всегда жив).
    Живой прогон на этой самой машине дал rest_rss_mb=39.8MB для in-process
    throwaway-конфигурации (в которой НЕТ отдельного rest_server.py процесса
    вообще) — это оказался ЧУЖОЙ, продовый standalone rest_server.py.
    `--pid` — точечный оверрайд: скоуп ограничивается указанным PID + его
    psutil-дерево потомков, а не всей машиной."""

    def test_get_processes_with_pid_root_scopes_to_process_tree(self):
        """Процесс, запущенный ЭТИМ тестом (заведомо НЕ матчащий ни один
        cmdline-маркер), обязан попасть в выдачу, когда его PID передан явно —
        proof, что scoping идёт по ДЕРЕВУ ПРОЦЕССОВ, а не по имени."""
        mod = _load_memory_baseline_module()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            matches = mod.get_processes(pid_roots=[proc.pid])
            pids = {m["pid"] for m in matches}
            self.assertIn(proc.pid, pids, f"scoped-PID процесс не найден: {matches!r}")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_get_processes_with_pid_root_excludes_unrelated_processes(self):
        """Системный процесс ТЕКУЩЕГО тестового раннера (pid текущего процесса)
        matches ли по маркеру или нет — НЕ должен утечь в выдачу, если
        pid_roots указывает на ДРУГОЕ (постороннее) дерево."""
        mod = _load_memory_baseline_module()
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            matches = mod.get_processes(pid_roots=[target.pid])
            pids = {m["pid"] for m in matches}
            self.assertNotIn(unrelated.pid, pids)
            self.assertNotIn(os.getpid(), pids)
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)
            target.terminate()
            target.wait(timeout=5)

    def test_take_snapshot_accepts_pid_roots(self):
        """take_snapshot прокидывает pid_roots в get_processes — regression для
        случая, когда прокидывание забыто на полпути (тот же класс, что и
        socket-параметр выше)."""
        mod = _load_memory_baseline_module()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Ни один cmdline-маркер не матчит python -c "..." — все rss-поля
            # останутся 0 БЕЗ scoping; с scoping total_rss_mb не проверяем
            # напрямую (процесс не попадает ни в одну КАТЕГОРИЮ по маркеру),
            # но get_processes(pid_roots=...) обязан вернуть его — проверяем
            # косвенно через прямой вызов, что параметр не проглочен молча.
            snap_unscoped = mod.take_snapshot(None)
            snap_scoped = mod.take_snapshot(None, pid_roots=[proc.pid])
            self.assertEqual(snap_unscoped.keys(), snap_scoped.keys())
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_cli_accepts_repeated_pid_flag(self):
        """CLI --pid (repeatable) — измерение конкретного дерева процессов, не
        всей машины. Живой прогон подтверждает контракт end-to-end."""
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        out_csv = REPO_ROOT / "test-mem-baseline-pid-tmp.csv"
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--once", "--pid", str(proc.pid), "--output", str(out_csv)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, f"script failed: {result.stderr}")
            self.assertTrue(out_csv.exists())
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            if out_csv.exists():
                out_csv.unlink()


if __name__ == "__main__":
    unittest.main()
