#!/usr/bin/env python3
"""e2e_event_bridge_smoke.py — двухпроцессный e2e для EventBridge.

Использование (вызывается из run_e2e_bridge_smoke.command, НЕ напрямую):
    python3 scripts/e2e_event_bridge_smoke.py <socket_path> <rest_base_url> <phase>

phase: "normal" | "after-kill" | "after-recovery" | "realtime_partial"
  normal            — оба процесса живы: emit -> SSE приходит <=200мс, latency печатается.
  after-kill        — REST убит: IPC-emit не блокируется (быстрый ok=True ответ),
                      команда не проверяет SSE (некому слушать).
  after-recovery    — REST поднят заново (тот же порт/data-dir): новое событие
                      доходит в течение <= BACKOFF_MAX_SEC + запас (см. константы
                      backend/event_bridge.py).
  realtime_partial  — поправка контролёра №2 (2026-07-07): доказывает, что
                      КОНКРЕТНО realtime.partial_transcript (5-я жертва гэпа —
                      StreamingPasteController.swift:121) проходит через мост до
                      SSE-подписчика, не только krab_error. Триггер — синтетический
                      event_bus.emit() внутри throwaway IPC-процесса (запланирован
                      run_e2e_bridge_smoke.command'ом через _ipc_driver.py, БЕЗ
                      реального захвата микрофона — недетерминированно и
                      приватность-инвазивно для безнадзорного smoke-теста).

Триггер события (krab_error): report_paste_failure IPC-метод (безопасный, без
side-effects на реальные данные — просто пушит KrabError в error_bus, который
эмитит "krab_error" на шину; backend/error_bus.py::push() -> event_bus.emit(
"krab_error", ...)).
"""
import json
import socket
import sys
import time

import requests


def call(sock_path: str, method: str, params: dict, timeout: int = 10) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    req = json.dumps({"id": f"bridge-smoke-{method}", "method": method, "params": params}) + "\n"
    s.sendall(req.encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def wait_for_sse_event(base_url: str, filter_type: str, timeout_sec: float) -> tuple[dict | None, float]:
    """Открывает SSE, возвращает (первый data-payload с этим типом | None, elapsed_sec)."""
    start = time.monotonic()
    try:
        with requests.get(f"{base_url}/v1/events?filter={filter_type}",
                          stream=True, timeout=timeout_sec + 2) as resp:
            for line in resp.iter_lines(decode_unicode=True):
                if time.monotonic() - start > timeout_sec:
                    return None, time.monotonic() - start
                if line and line.startswith("data: "):
                    return json.loads(line[len("data: "):]), time.monotonic() - start
    except requests.exceptions.RequestException:
        pass
    return None, time.monotonic() - start


def trigger_event(sock_path: str, marker: str) -> dict:
    """Вызывает report_paste_failure — безопасный, детерминированный триггер krab_error."""
    return call(sock_path, "report_paste_failure",
                {"reason": "ax_denied", "app_bundle": f"com.test.e2e.{marker}"})


def main() -> int:
    sock_path, base_url, phase = sys.argv[1], sys.argv[2], sys.argv[3]

    if phase == "normal":
        # Открыть SSE ДО эмита (иначе подписка не успеет зарегистрироваться).
        import threading
        result_holder = {}

        def _listen():
            result_holder["data"], result_holder["elapsed"] = wait_for_sse_event(
                base_url, "krab_error", timeout_sec=5.0
            )

        t = threading.Thread(target=_listen, daemon=True)
        t.start()
        time.sleep(0.5)  # дать SSE зарегистрироваться в EventBus
        t_emit = time.monotonic()
        resp = trigger_event(sock_path, "normal")
        if not resp.get("ok"):
            print(f"FAIL: report_paste_failure вернул ok=False: {resp}")
            return 1
        t.join(timeout=6.0)
        elapsed_ms = (time.monotonic() - t_emit) * 1000
        if result_holder.get("data") is None:
            print("FAIL: SSE-событие krab_error не пришло за 5с")
            return 1
        print(f"OK: событие пришло за {elapsed_ms:.1f}мс")
        if elapsed_ms > 200:
            print(f"WARN: latency {elapsed_ms:.1f}мс > 200мс (DoD-порог) — расследовать")
            return 1
        return 0

    if phase == "after-kill":
        # ФИКС (обнаружено при живом прогоне T6): ErrorBus.push() дедупит
        # ПОВТОРНЫЙ push ТОГО ЖЕ error-кода в 30-секундном окне
        # (backend/error_bus.py::ErrorBus, default_dedupe_window_sec=30.0) —
        # если тут снова триггернуть paste.ax_denied (уже запушен Фазой 1
        # секундами раньше), push дедупится, event_bus.emit("krab_error", ...)
        # НЕ вызывается, и WARN о переходе в down никогда не логируется (не
        # баг моста — баг ЭТОГО smoke-теста, наивно переиспользовавшего один
        # код). Фикс: report_hotkey_conflict -> ДРУГОЙ код (hotkey.conflict),
        # не деденплицируется относительно paste.ax_denied.
        t0 = time.monotonic()
        resp = call(sock_path, "report_hotkey_conflict",
                    {"chord": "e2e-bridge-smoke-chaos"}, timeout=5)
        elapsed = time.monotonic() - t0
        if not resp.get("ok"):
            print(f"FAIL: IPC-вызов не вернул ok=True при мёртвом REST: {resp}")
            return 1
        if elapsed > 3.0:
            print(f"FAIL: IPC-вызов заблокировался на {elapsed:.1f}с при мёртвом REST (эмиттер НЕ должен блокироваться)")
            return 1
        print(f"OK: IPC-вызов не заблокирован при мёртвом REST ({elapsed:.2f}с)")
        return 0

    if phase == "after-recovery":
        # Backoff-потолок 30с (backend/event_bridge.py::BACKOFF_MAX_SEC) + запас.
        result, elapsed = wait_for_sse_event(base_url, "krab_error", timeout_sec=40.0)
        # Триггерим НОВОЕ событие ПОСЛЕ того как SSE начал слушать (иначе гонка).
        # (run_e2e_bridge_smoke.command вызывает trigger ПЕРЕД этой фазой — см. скрипт).
        if result is None:
            print("FAIL: событие не дошло после восстановления REST за 40с")
            return 1
        print(f"OK: событие дошло после восстановления REST за {elapsed:.1f}с")
        return 0

    if phase == "realtime_partial":
        # Поправка контролёра №2: 5-я жертва гэпа (streaming paste) специфично
        # использует realtime.partial_transcript — доказываем ИМЕННО этот тип
        # проходит через мост, не только krab_error. Триггер уже запланирован
        # внутри throwaway IPC-процесса (_ipc_driver.py, синтетический
        # event_bus.emit() через ~10с после старта) — здесь только слушаем.
        # Таймаут с большим запасом: покрывает время старта IPC+REST+Фазы 1
        # ДО того, как мы сюда доходим, плюс сама 10с задержка драйвера.
        result, elapsed = wait_for_sse_event(base_url, "realtime.partial_transcript", timeout_sec=25.0)
        if result is None:
            print("FAIL: realtime.partial_transcript не дошёл через мост за 25с")
            return 1
        # wait_for_sse_event уже возвращает распакованный data-payload (не конверт
        # {type, ts, data}) — см. её docstring.
        if result.get("session_id") != "e2e-bridge-smoke":
            print(f"FAIL: получен realtime.partial_transcript, но session_id не совпадает: {result!r}")
            return 1
        print(f"OK: realtime.partial_transcript (streaming-paste гэп) дошёл через мост за {elapsed:.1f}с")
        return 0

    print(f"FAIL: неизвестная фаза {phase!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
