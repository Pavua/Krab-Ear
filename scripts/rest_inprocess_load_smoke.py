#!/usr/bin/env python3
"""Живой e2e-смок волны M2 «REST внутри процесса backend» (Task 8).

Поднимает ОТДЕЛЬНЫЙ throwaway backend (временный data_dir + СЛУЧАЙНЫЙ
свободный порт — никогда 5005) с включённым рубильником
``REST_IN_PROCESS_ENABLED`` и доказывает вживую, что:

  1. backend поднимается с in-process REST;
  2. контракт ``GET /health`` (то, что читает Voice Gateway) не сломан;
  3. ``get_diagnostics()["rest_in_process"]`` не врёт про running/port;
  4. мост событий IPC->REST сам себя выключил (анти-echo, спека §2.1);
  5. сервер держит нагрузку: 3 параллельных SSE-подписчика + 20+
     конкурентных HTTP-запросов, печатается p95;
  6. событие, эмитнутое в шину backend, доходит до КАЖДОГО SSE-подписчика
     РОВНО один раз — главная проверка волны (без анти-echo мост дал бы
     повторную доставку одного и того же конверта);
  7. после ``service.close()`` порт освобождён и REST-тред мёртв.

ИЗОЛЯЦИЯ (обязательна — на машине живёт ПРОДОВЫЙ backend владельца):
  - временный data_dir через tempfile.mkdtemp(), удаляется в finally;
  - случайный свободный порт (никогда 5005);
  - никаких launchctl/pkill/kill по имени процесса — только
    ``proc.terminate()``/``proc.kill()`` над СОБСТВЕННЫМ throwaway-сабпроцессом,
    который создал этот же скрипт;
  - подключение только к throwaway Unix-сокету во временном data_dir.

Паттерны переиспользованы из ``scripts/e2e_owner_gate_smoke.py`` (спавн
throwaway backend через subprocess, JSON-RPC поверх Unix-сокета, облегчение
окружения через set_settings) и ``scripts/run_e2e_bridge_smoke.command``
(идея проверять «дошло ровно один раз» живым HTTP/SSE трафиком, а не
юнит-моком).

Запуск (venv обязателен; интерпретатор из .venv_krab_ear, путь содержит
пробел — в реальной команде обязательно взять в кавычки):

    cd "<repo_root>"
    PYTHONPATH=$(pwd)/KrabEar ".venv_krab_ear/bin/python" \
        -u scripts/rest_inprocess_load_smoke.py

exit 0 — все фазы OK. exit 1 — минимум одна фаза FAIL (см. вывод, какая
именно и почему).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover — venv гарантирует requests (event_bridge.py уже зависит)
    print("FAIL: пакет requests не найден в интерпретаторе — активируй .venv_krab_ear", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
VENV_PY = Path(sys.executable)

# Урок волны R2: тесные таймауты режут валидную работу — ни одна фаза не
# короче 120с, даже если по факту укладывается за секунды.
PHASE_TIMEOUT_SEC = 150.0
SOCKET_READY_TIMEOUT_SEC = 120.0
SHUTDOWN_WAIT_SEC = 60.0

LOAD_REQUEST_COUNT = 24  # >= 20 конкурентных HTTP-запросов, требование задачи
LOAD_WORKERS = 10
SSE_SUBSCRIBER_COUNT = 3
SSE_SETTLE_SEC = 2.5      # даём генератору sse_stream() дойти до bus.subscribe()
SSE_DELIVERY_GRACE_SEC = 4.0  # окно, за которое возможный дубль успел бы прилететь
SSE_READ_TIMEOUT_SEC = 30.0

_STEPS: list[tuple[str, bool, str]] = []


def _step(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK  " if ok else "FAIL"
    line = f"{mark}: {label}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    _STEPS.append((label, ok, detail))
    return ok


def _run_phase(label: str, fn, timeout: float = PHASE_TIMEOUT_SEC):
    """Гоняет фазу в отдельном потоке с таймаутом; ловит любое исключение.

    Возвращает результат fn() либо None при провале/таймауте — сама фаза уже
    напечатала FAIL через _step() до того, как исключение долетело сюда.
    """
    print(f"\n--- Фаза: {label} ---", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            _step(label, False, f"фаза не уложилась в {timeout:.0f}с")
            return None
        except Exception as exc:  # noqa: BLE001 — смок обязан дойти до конца
            _step(label, False, f"необработанное исключение: {exc!r}")
            return None


# ---------------------------------------------------------------------------
# JSON-RPC поверх Unix socket (паттерн e2e_owner_gate_smoke.py::_call)
# ---------------------------------------------------------------------------

def _call(sock_path: Path, method: str, params: dict, timeout: float = 30.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    req = json.dumps({"id": f"m2-smoke-{method}", "method": method, "params": params}) + "\n"
    s.sendall(req.encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def _result(resp: dict) -> dict:
    if isinstance(resp.get("result"), dict):
        return resp["result"]
    return resp


def _find_free_port() -> int:
    """Короткий bind-and-release. TOCTOU-окно приемлемо для throwaway-смока —
    сервер поднимается через секунды после освобождения, конкурентов на этой
    машине для случайного эфемерного порта практически нет."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct * 100) - 1]


# ---------------------------------------------------------------------------
# Спавн/останов throwaway backend
# ---------------------------------------------------------------------------

def _spawn_backend(data_dir: Path, port: int, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KRAB_EAR)
    # M2 рубильник + случайный порт — единственный способ включить in-process
    # REST это env-переменные Pydantic-Settings (core/config.py), читаются ОДИН
    # РАЗ на старте процесса, поэтому выставляются ДО spawn, не через set_settings.
    env["KRAB_EAR_REST_IN_PROCESS_ENABLED"] = "true"
    env["KRAB_EAR_REST_SERVER_PORT"] = str(port)
    log_fh = log_path.open("wb")
    return subprocess.Popen(
        [str(VENV_PY), str(KRAB_EAR / "main.py"), "--data-dir", str(data_dir)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )


def _wait_for_socket(sock_path: Path, proc: subprocess.Popen, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                if _call(sock_path, "ping", {}, timeout=5.0).get("ok"):
                    return True
            except (OSError, socket.timeout, ConnectionRefusedError, json.JSONDecodeError):
                pass
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def _prepare_lightweight_settings(sock: Path) -> None:
    """Облегчает окружение throwaway-инстанса (урок волны R2, F1 плана M2).

    Смок проверяет транспорт (HTTP/SSE поверх in-process REST), а не качество
    STT/диаризации/LLM — тяжёлые движки на throwaway-инстансе только едят
    минуты на холодный старт worker-ов и не участвуют ни в одной из 7 фаз.
    """
    _call(sock, "set_settings", {
        "stt_gigaam_enabled": False,
        "diarization_enabled": False,
        "llm_rewriter_enabled": False,
        "realtime_partial_enabled": False,
    }, timeout=30.0)


# ---------------------------------------------------------------------------
# Фазы
# ---------------------------------------------------------------------------

def _phase_health_contract(base_url: str) -> bool:
    """(2) Контракт Voice Gateway: GET /health -> 200 + status/profile."""
    resp = requests.get(f"{base_url}/health", timeout=10)
    ok_status = resp.status_code == 200
    _step("GET /health вернул 200", ok_status, f"status_code={resp.status_code}")
    if not ok_status:
        return False
    body = resp.json()
    has_fields = "status" in body and "profile" in body
    _step("тело /health содержит status и profile", has_fields, f"body={body}")
    return ok_status and has_fields


def _phase_diagnostics_rest_inprocess(sock: Path, port: int) -> bool:
    """(3) get_diagnostics()['rest_in_process'] не врёт про running/port."""
    diag = _result(_call(sock, "get_diagnostics", {}, timeout=30.0))
    section = diag.get("rest_in_process", {})
    running_ok = section.get("running") is True
    port_ok = section.get("port") == port
    _step("rest_in_process.running == True", running_ok, f"section={section}")
    _step("rest_in_process.port == выделенный порт", port_ok, f"port={section.get('port')} ожидали {port}")
    return running_ok and port_ok


def _phase_bridge_disabled(sock: Path) -> bool:
    """(4) Мост событий выключен — анти-echo, доказательство невозможности двойной доставки."""
    diag = _result(_call(sock, "get_diagnostics", {}, timeout=30.0))
    state = diag.get("event_bridge", {}).get("state")
    ok = state == "disabled"
    _step("event_bridge.state == 'disabled'", ok, f"state={state!r}")
    return ok


def _sse_reader(url: str, marker: str, out: dict, stop_event: threading.Event) -> None:
    """Слушает /v1/events?filter=krab_error и считает конверты с нашей меткой.

    ``out["count"]`` обновляется ЖИВЬЁМ на каждом совпадении (не только в
    конце функции) — sse_stream() шлёт keepalive раз в 15с, поэтому
    блокирующий next() внутри reader-потока мог бы не заметить stop_event
    ещё 15с; вызывающая сторона не обязана дожидаться выхода из цикла, чтобы
    прочитать актуальный счётчик (поток — daemon, интерпретатор не станет
    его ждать при завершении процесса).
    """
    out["count"] = 0
    resp = None
    try:
        resp = requests.get(url, stream=True, timeout=(10.0, SSE_READ_TIMEOUT_SEC))
        out["status_code"] = resp.status_code
        out["resp"] = resp
        if resp.status_code != 200:
            return
        for raw_line in resp.iter_lines(decode_unicode=True):
            if stop_event.is_set():
                break
            if not raw_line or not raw_line.startswith("data:"):
                continue
            try:
                payload = json.loads(raw_line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            if payload.get("code") == "hotkey.conflict" and payload.get("context", {}).get("chord") == marker:
                out["count"] += 1
    except Exception as exc:  # noqa: BLE001 — исход попадает в out, поток обязан завершиться
        out["error"] = repr(exc)
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def _timed_get(url: str) -> tuple[float, int]:
    t0 = time.monotonic()
    resp = requests.get(url, timeout=15)
    return (time.monotonic() - t0, resp.status_code)


def _phase_load_and_single_delivery(base_url: str, sock: Path) -> dict | None:
    """(5)+(6) Нагрузка (3 SSE + 20+ HTTP конкурентно) и проверка "ровно один раз"."""
    sse_url = f"{base_url}/v1/events?filter=krab_error"
    marker = f"m2-smoke-{uuid.uuid4().hex}"
    stop_event = threading.Event()
    outs: list[dict] = [{} for _ in range(SSE_SUBSCRIBER_COUNT)]
    threads = [
        threading.Thread(target=_sse_reader, args=(sse_url, marker, outs[i], stop_event), daemon=True)
        for i in range(SSE_SUBSCRIBER_COUNT)
    ]
    for t in threads:
        t.start()

    # Даём генераторам sse_stream() время дойти до bus.subscribe() ДО того,
    # как эмитнем маркерное событие. ВАЖНО: requests.get(..., stream=True) в
    # _sse_reader может НЕ вернуться из вызова ещё до 15с (см. sse_stream():
    # первый chunked-flush клиенту уходит либо с первым событием, либо с
    # keepalive-таймаутом _SSE_POLL_TIMEOUT_SEC=15с — до этого werkzeug не
    # шлёт заголовки ответа). bus.subscribe() при этом уже выполнен НА
    # СЕРВЕРЕ до первого блокирующего q.get() — значит подписка реально
    # готова к приёму задолго до того, как out["status_code"] станет видимым
    # этому скрипту. Поэтому здесь только информационный снимок (не gate);
    # настоящая проверка подключения — часть финальной проверки доставки ниже.
    time.sleep(SSE_SETTLE_SEC)
    print(
        f"    (информационно) status_codes подписчиков на {SSE_SETTLE_SEC:.1f}с: "
        f"{[o.get('status_code') for o in outs]}",
        flush=True,
    )

    latencies: list[float] = []
    statuses: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=LOAD_WORKERS) as ex:
        futures = [ex.submit(_timed_get, f"{base_url}/health") for _ in range(LOAD_REQUEST_COUNT)]
        # Эмитим маркерное событие ПОКА нагрузка ещё в полёте — "доставка под нагрузкой".
        trigger = _result(_call(sock, "report_hotkey_conflict", {"chord": marker}, timeout=30.0))
        for f in concurrent.futures.as_completed(futures):
            dt, status = f.result()
            latencies.append(dt)
            statuses.append(status)

    ok_trigger = bool(trigger.get("ok", False))
    _step("IPC report_hotkey_conflict принят", ok_trigger, f"resp={trigger}")

    all_2xx = all(200 <= s < 300 for s in statuses)
    p95_ms = _percentile(latencies, 0.95) * 1000.0
    p50_ms = _percentile(latencies, 0.50) * 1000.0
    _step(
        f"{LOAD_REQUEST_COUNT} конкурентных HTTP-запросов все успешны",
        all_2xx and len(statuses) == LOAD_REQUEST_COUNT,
        f"коды={sorted(set(statuses))} n={len(statuses)}",
    )
    print(f"    p50={p50_ms:.1f}мс  p95={p95_ms:.1f}мс  (n={len(latencies)}, воркеров={LOAD_WORKERS})", flush=True)

    # Окно на возможную повторную доставку — если бы анти-echo был сломан,
    # дубль долетел бы почти сразу следом за первым конвертом. Счётчики в
    # out["count"] обновляются ЖИВЬЁМ внутри reader-потоков, поэтому снимок
    # сразу после сна уже отражает всё, что успело прийти за грейс-окно —
    # выход потока из цикла (до 15с из-за keepalive) для этого не нужен.
    time.sleep(SSE_DELIVERY_GRACE_SEC)
    stop_event.set()
    snapshot = [(out.get("count", 0), out.get("error"), out.get("status_code")) for out in outs]

    # Останов подписчиков — best-effort уборка, НЕ влияет на вердикт выше:
    # закрытие resp из чужого потока не гарантированно мгновенно прерывает
    # блокирующий next() на всех платформах, поэтому join() ниже с коротким
    # таймаутом — просто попытка не оставить сокеты висеть дольше нужного.
    for out in outs:
        resp = out.get("resp")
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
    for t in threads:
        t.join(timeout=5.0)

    single_delivery_ok = True
    for i, (count, err, status_code) in enumerate(snapshot):
        ok = count == 1 and err is None and status_code == 200
        single_delivery_ok = single_delivery_ok and ok
        _step(
            f"SSE-подписчик #{i}: подключился (200) и получил событие ровно 1 раз",
            ok,
            f"status_code={status_code} count={count} error={err}",
        )

    return {
        "ok": ok_trigger and all_2xx and single_delivery_ok,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "n": len(latencies),
    }


def _phase_clean_stop(proc: subprocess.Popen, port: int, base_url: str) -> bool:
    """(7) После service.close() порт свободен и REST-тред мёртв."""
    proc.terminate()
    try:
        proc.wait(timeout=SHUTDOWN_WAIT_SEC)
        exited = True
    except subprocess.TimeoutExpired:
        exited = False
    _step("процесс backend завершился после SIGTERM", exited, f"уложился в {SHUTDOWN_WAIT_SEC:.0f}с")
    if not exited:
        proc.kill()
        proc.wait(timeout=10)
        return False

    # Порт свободен — доказательство успешным bind'ом нового сервера на него.
    port_free = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            port_free = True
    except OSError as exc:
        _step("порт освобождён (bind удался)", False, repr(exc))
        return False
    _step("порт освобождён (bind удался)", port_free)

    # REST-тред мёртв — HTTP-запрос к уже остановленному серверу обязан
    # получить отказ соединения, а не ответ.
    conn_refused = False
    try:
        requests.get(f"{base_url}/health", timeout=3)
    except requests.exceptions.ConnectionError:
        conn_refused = True
    except Exception as exc:
        _step("REST-тред мёртв (соединение отвергнуто)", False, f"неожиданное исключение: {exc!r}")
        return False
    _step("REST-тред мёртв (соединение отвергнуто)", conn_refused)
    return port_free and conn_refused


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _finish(tmp_dir: Path, log_path: Path) -> int:
    failed = [s for s in _STEPS if not s[1]]
    print("\n" + "=" * 70)
    print(f"ИТОГО: {len(_STEPS) - len(failed)}/{len(_STEPS)} проверок OK")
    if failed:
        print("\nПРОВАЛЕНО:")
        for label, _, detail in failed:
            print(f"  - {label} — {detail}")
        keep = Path(tempfile.gettempdir()) / f"rest_inprocess_smoke_fail_{int(time.time())}.log"
        try:
            shutil.copy2(log_path, keep)
            print(f"\nЛог backend сохранён: {keep}")
        except OSError:
            pass
    print("=" * 70)
    return 1 if failed else 0


def main() -> int:
    if not KRAB_EAR.exists():
        print(f"Не найден {KRAB_EAR}", file=sys.stderr)
        return 2

    tmp_dir = Path(tempfile.mkdtemp(prefix="krab_rest_inprocess_smoke_"))
    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_dir / "backend.log"
    proc: "subprocess.Popen | None" = None

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    sock = data_dir / "krabear.sock"

    print("=" * 70)
    print("M2 REST-IN-PROCESS LOAD SMOKE — throwaway data_dir:", data_dir)
    print(f"Случайный порт: {port}  (никогда 5005 — прод не задет)")
    print("=" * 70)

    try:
        proc = _spawn_backend(data_dir, port, log_path)

        def _phase1() -> bool:
            if not _wait_for_socket(sock, proc, SOCKET_READY_TIMEOUT_SEC):
                _step("backend поднялся (in-process REST рубильник включён)", False, "сокет не открылся")
                return False
            _step("backend поднялся (in-process REST рубильник включён)", True)
            _prepare_lightweight_settings(sock)
            _step("окружение облегчено (GigaAM/диаризация/LLM/realtime-partial выключены)", True)
            return True

        started = _run_phase("1. Поднять throwaway backend", _phase1)
        if not started:
            return _finish(tmp_dir, log_path)

        ok2 = _run_phase("2. Контракт /health (Voice Gateway)", lambda: _phase_health_contract(base_url))
        ok3 = _run_phase(
            "3. Диагностика rest_in_process не врёт",
            lambda: _phase_diagnostics_rest_inprocess(sock, port),
        )
        ok4 = _run_phase("4. Мост событий выключен (анти-echo)", lambda: _phase_bridge_disabled(sock))
        load_result = _run_phase(
            "5+6. Нагрузка (3 SSE + 20+ HTTP) и доставка ровно 1 раз",
            lambda: _phase_load_and_single_delivery(base_url, sock),
        )
        ok7 = _run_phase(
            "7. Чистый останов (порт свободен, тред мёртв)",
            lambda: _phase_clean_stop(proc, port, base_url),
        )
        proc = None  # phase 7 уже разобралась с процессом (успешно или через kill)

        del ok2, ok3, ok4, load_result, ok7  # статусы уже осели в _STEPS через _step()
        return _finish(tmp_dir, log_path)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
