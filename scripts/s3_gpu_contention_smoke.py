#!/usr/bin/env python3
"""S3/Task10: три сценария конкуренции за GPU под слитым REST (Р8 + §1.3 спеки,
находка ревью I-G).

Родительская §4.4 (docs/superpowers/specs/2026-07-16-m-series-rest-merge-design.md)
утверждала, что REST-transcribe и диктовка «уже сериализуются межпроцессным
flock — конкуренция не новая». S3-спека (§1.3) показала, что это ложная
посылка: ``mlx_inter_process_lock()`` — no-op без
``KRAB_EAR_MLX_INTER_PROCESS_LOCK=1`` (ни в одном плисте её нет), сегодня
REST-процесс и диктовка бьются за Metal свободно; после слияния оба пути
проходят через ОДИН ``mlx_lock()`` (см. ``core/mlx_lock.py``) — становится
РЕАЛЬНОЙ сериализацией, впервые.

Задеты не только разовые операции (диктовка), но и периодические конвейеры с
каденс-дедлайнами: живая встреча (``CHUNK_STT`` каждые 25с) и live-субтитры
(flush каждые 3с). Тридцатисекундный REST-transcribe от Voice Gateway
задерживает не разовую операцию, а копит отставание — качественно другой
эффект, чем разовая латентность диктовки.

Лаг (определение этой волны, зафиксировано в задаче 10 плана):
    время от подачи аудио-чанка до появления его текста в снапшоте.
    Для встречи — от meeting_start + подача чанка до появления текста в
    get_meeting_live_state. Для субтитров — от live_subs_ingest до
    соответствующего события live_subs.result.

Три сценария в этом скрипте:
  (A) латентность диктовки (proxy = ``transcribe_paths`` IPC — тот же
      mlx_lock-путь, что реальная остановка записи, БЕЗ физического мика) —
      baseline (без фона) vs под конкурентным POST /v1/stt/transcribe;
  (B) лаг live-субтитров (``live_subs_ingest`` — полностью синтетический
      PCM, БЕЗ мика/динамиков) — baseline vs под тем же фоном;
  (C) лаг снапшота встречи — ТРЕБУЕТ реального захвата микрофона
      (``meeting_session_service.py``: CHUNK_STT читает
      ``AudioRecorder.snapshot_range()``, IPC-инъекции чанков в meeting не
      существует). Разговорная тишина НЕ двигает ``last_updated_ts``
      (обновляется только при непустом тексте) — числовой лаг без реальной
      речи не измерить. Проигрывание синтетической речи через динамики
      ОПАСНО делать вслепую на этой сессии: `--play-audio` явно опционален
      и по умолчанию ВЫКЛЮЧЕН — играть звук через колонки рядом с чужим
      живым микрофоном без явного «да» владельца запрещено codex'ом
      (раздел "Explicit permission required" / "живые настройки и
      устройства пользователя"). Без флага скрипт лишь доказывает, что
      meeting остаётся живым (active=true, без краша) под тем же фоном —
      тот же критерий приёмки, что ``e2e_meeting_smoke.py``.

ИЗОЛЯЦИЯ: throwaway data_dir + случайный порт, никогда прод; сценарии A/B —
никакого реального аудио вообще (синтетический WAV/PCM). Сценарий C без
`--play-audio` тоже не трогает динамики — только слушает то, что уже
попадает в дефолтный вход (обычно тишина комнаты на throwaway-инстансе).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar ".venv_krab_ear/bin/python" \
        -u scripts/s3_gpu_contention_smoke.py [--play-audio]

exit 0 — все сценарии завершились без исключений (числа печатаются как
данные для журнала замеров, не как pass/fail — контраст baseline/contended
ЕСТЬ вердикт, конкретные абсолютные цифры на загруженной машине шумны по
определению задачи).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("FAIL: пакет requests не найден — активируй .venv_krab_ear", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
KRAB_EAR = REPO_ROOT / "KrabEar"
VENV_PY = Path(sys.executable)

SOCKET_READY_TIMEOUT_SEC = 120.0
CONTENTION_WORKERS = 4  # фоновый POST-шторм — источник конкуренции за mlx_lock
DICTATION_REPEATS = 5   # повторов на baseline/contended для p50/p95


def _make_silence_wav(path: Path, duration_sec: float = 1.0, sample_rate: int = 16000) -> Path:
    n_samples = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return path


def _call(sock_path: Path, method: str, params: dict, timeout: float = 60.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    req = json.dumps({"id": f"gpu-contention-{method}", "method": method, "params": params}) + "\n"
    s.sendall(req.encode("utf-8"))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def _result(resp: dict) -> dict:
    r = resp.get("result")
    return r if isinstance(r, dict) else resp


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct * 100) - 1]


def _spawn_backend(data_dir: Path, port: int, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(KRAB_EAR)
    env["KRAB_EAR_REST_IN_PROCESS_ENABLED"] = "true"
    env["KRAB_EAR_REST_SERVER_PORT"] = str(port)
    log_fh = log_path.open("wb")
    return subprocess.Popen(
        [str(VENV_PY), str(KRAB_EAR / "main.py"), "--data-dir", str(data_dir)],
        cwd=str(REPO_ROOT), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
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


def _lighten_settings(sock: Path) -> bool:
    settings = {
        "stt_gigaam_enabled": False,
        "diarization_enabled": False,
        "llm_rewrite_enabled": False,
        "realtime_partial_enabled": False,
    }
    resp = _call(sock, "set_settings", settings, timeout=30.0)
    if not resp.get("ok"):
        return False
    merged = _result(resp)
    return all(merged.get(k) == v for k, v in settings.items())


class _GpuContentionLoad:
    """Фоновый шторм конкурентных POST /v1/stt/transcribe — единственный
    источник реальной конкуренции за mlx_lock в этом скрипте. Работает,
    пока не остановлен, чтобы окно "под нагрузкой" было полностью покрыто
    измеряемым сценарием (а не гасло раньше времени)."""

    def __init__(self, base_url: str, wav_path: Path, workers: int = CONTENTION_WORKERS) -> None:
        self._base_url = base_url
        self._wav_path = wav_path
        self._workers = workers
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._count = 0
        self._lock = threading.Lock()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._wav_path.open("rb") as fh:
                    requests.post(
                        f"{self._base_url}/v1/stt/transcribe",
                        files={"file": ("silence.wav", fh, "audio/wav")},
                        data={"diarize": "false", "persist_history": "false"},
                        timeout=120,
                    )
                with self._lock:
                    self._count += 1
            except Exception:
                pass  # фон — сбой одного запроса не должен убить генератор

    def start(self) -> None:
        """Один инстанс переиспользуется между сценариями A/B/C (main()) — Python-
        треды стартуют РОВНО один раз (`RuntimeError: threads can only be started
        once`), поэтому свежие треды создаются на КАЖДЫЙ вызов start(), а не в
        конструкторе."""
        self._stop.clear()
        self._threads = [threading.Thread(target=self._loop, daemon=True) for _ in range(self._workers)]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10.0)
        self._threads = []

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


def _scenario_dictation_latency(sock: Path, wav_path: Path, load: _GpuContentionLoad) -> dict:
    """(A) Латентность "диктовки" (proxy transcribe_paths) — baseline vs под фоном.

    transcribe_paths — та же mlx_lock-цепочка, что реальная остановка записи
    (AudioEngine.transcribe), но без физического мика — безопасно гонять на
    throwaway-инстансе без риска задеть живую запись владельца.
    """
    def _one_call() -> float:
        t0 = time.monotonic()
        resp = _call(sock, "transcribe_paths", {"paths": [str(wav_path)]}, timeout=180.0)
        dt = time.monotonic() - t0
        result = _result(resp)
        # top-level ok — транспорт JSON-RPC (нет ли исключения); errors —
        # содержательная проверка (валидация путей/аудио могла молча вернуть
        # 0 обработанных файлов без исключения, см. _transcribe_paths_core).
        if not resp.get("ok") or result.get("errors"):
            raise RuntimeError(f"transcribe_paths провалился: {resp}")
        return dt

    baseline = [_one_call() for _ in range(DICTATION_REPEATS)]

    load.start()
    time.sleep(1.0)  # даём фону реально загрузить mlx_lock перед измерением
    try:
        contended = [_one_call() for _ in range(DICTATION_REPEATS)]
    finally:
        load.stop()

    return {
        "baseline_p50_ms": _percentile(baseline, 0.5) * 1000.0,
        "baseline_p95_ms": _percentile(baseline, 0.95) * 1000.0,
        "contended_p50_ms": _percentile(contended, 0.5) * 1000.0,
        "contended_p95_ms": _percentile(contended, 0.95) * 1000.0,
        "background_requests_completed": load.count,
        "n": DICTATION_REPEATS,
    }


def _live_subs_lag_once(sock: Path, chunk_bytes: bytes, chunk_sample_rate: int) -> float:
    """Один цикл: копим >=3с буфера, время последнего (флашащего) вызова —
    и есть лаг по определению волны (ingest -> появление текста синхронно
    в этом же ответе, см. LiveSubsService._flush — emit ДО return)."""
    # 2 неполных чанка (буфер копится) + финальный, который триггерит flush.
    for _ in range(2):
        _call(sock, "live_subs_ingest", {
            "audio_chunk": base64.b64encode(chunk_bytes).decode("ascii"),
            "sample_rate": chunk_sample_rate,
            "target_lang": "off",
            "is_final": False,
        }, timeout=30.0)
    t0 = time.monotonic()
    resp = _call(sock, "live_subs_ingest", {
        "audio_chunk": base64.b64encode(chunk_bytes).decode("ascii"),
        "sample_rate": chunk_sample_rate,
        "target_lang": "off",
        "is_final": True,
    }, timeout=180.0)
    dt = time.monotonic() - t0
    if not resp.get("ok"):
        raise RuntimeError(f"live_subs_ingest провалился: {resp}")
    return dt


def _scenario_live_subs_lag(sock: Path, load: _GpuContentionLoad) -> dict:
    """(B) Лаг live-субтитров — полностью синтетический PCM, baseline vs под фоном."""
    sample_rate = 16000
    chunk_sec = 1.1  # 3 чанка по 1.1с > порог автофлаша 3.0с (core/live_subs_service.py)
    silence_chunk = b"\x00\x00" * int(sample_rate * chunk_sec)

    baseline = []
    for _ in range(3):
        baseline.append(_live_subs_lag_once(sock, silence_chunk, sample_rate))
        _call(sock, "live_subs_stop", {}, timeout=30.0)  # сброс буфера между прогонами

    load.start()
    time.sleep(1.0)
    try:
        contended = []
        for _ in range(3):
            contended.append(_live_subs_lag_once(sock, silence_chunk, sample_rate))
            _call(sock, "live_subs_stop", {}, timeout=30.0)
    finally:
        load.stop()

    return {
        "baseline_p50_ms": _percentile(baseline, 0.5) * 1000.0,
        "contended_p50_ms": _percentile(contended, 0.5) * 1000.0,
        "background_requests_completed": load.count,
        "n": len(baseline),
    }


def _scenario_meeting_lag(sock: Path, load: _GpuContentionLoad, play_audio: bool) -> dict:
    """(C) Лаг снапшота встречи — требует реального мика; см. докстринг модуля."""
    if not play_audio:
        r = _call(sock, "meeting_start", {}, timeout=30.0)
        started_ok = bool(r.get("ok")) and bool(_result(r).get("ok", True))
        stopped = False
        try:
            load.start()
            time.sleep(5.0)  # даём CHUNK_STT-тику шанс отработать под фоном
            st = _call(sock, "get_meeting_live_state", {}, timeout=30.0)
            active_ok = bool(_result(st).get("active"))
        finally:
            load.stop()
            r2 = _call(sock, "meeting_stop", {}, timeout=60.0)
            stopped = bool(r2.get("ok"))
        return {
            "skipped_reason": (
                "без --play-audio meeting-встреча слушает только тишину комнаты "
                "(реального мика на throwaway-инстансе не требуется, но и текст "
                "никогда не появится — last_updated_ts обновляется ТОЛЬКО при "
                "непустом STT-тексте, см. meeting_session_service.py::_job_chunk_stt). "
                "Числовой лаг ПО ОПРЕДЕЛЕНИЮ волны не измерим без реальной речи. "
                "Это НЕ провал скрипта — задокументированное ограничение, см. "
                "докстринг модуля. Запусти с --play-audio на разгруженной машине "
                "и с явного 'да' владельца, чтобы получить число."
            ),
            "meeting_start_ok": started_ok,
            "meeting_stayed_active_under_background_load": active_ok,
            "meeting_stop_ok": stopped,
            "background_requests_completed": load.count,
        }

    # --play-audio: см. предупреждение в докстринге — воспроизводит короткую
    # синтетическую фразу через системные динамики (`say`), чтобы throwaway
    # meeting-сессия реально услышала непустую речь. НИКОГДА не вызывать без
    # явного "да" владельца в чате на КОНКРЕТНЫЙ прогон (Codex: "живые
    # настройки и устройства пользователя — только после явного 'да'").
    r = _call(sock, "meeting_start", {}, timeout=30.0)
    started_ok = bool(r.get("ok")) and bool(_result(r).get("ok", True))
    t_chunk_fed = time.monotonic()
    text_seen_at: float | None = None
    try:
        load.start()
        subprocess.run(
            ["say", "-v", "Milena", "проверка проверка это тестовая фраза для встречи"],
            timeout=30, check=False,
        )
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            st = _result(_call(sock, "get_meeting_live_state", {}, timeout=30.0))
            if st.get("transcript_len", 0) > 0:
                text_seen_at = time.monotonic()
                break
            time.sleep(1.0)
    finally:
        load.stop()
        _call(sock, "meeting_stop", {}, timeout=60.0)

    lag_ms = ((text_seen_at - t_chunk_fed) * 1000.0) if text_seen_at else None
    return {
        "meeting_start_ok": started_ok,
        "lag_ms": lag_ms,
        "background_requests_completed": load.count,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--play-audio", action="store_true",
        help="Играет короткую синтетическую фразу через ДИНАМИКИ для сценария "
             "встречи (см. предупреждение в докстринге модуля) — ТОЛЬКО с явного "
             "'да' владельца на конкретный прогон.",
    )
    args = ap.parse_args()

    if not KRAB_EAR.exists():
        print(f"Не найден {KRAB_EAR}", file=sys.stderr)
        return 2

    tmp_dir = Path(tempfile.mkdtemp(prefix="krab_gpu_contention_smoke_"))
    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_dir / "backend.log"
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    sock = data_dir / "krabear.sock"
    wav_path = _make_silence_wav(tmp_dir / "silence_fixture.wav")

    print("=" * 70)
    print("S3 GPU-CONTENTION SMOKE — throwaway data_dir:", data_dir)
    print(f"Случайный порт: {port}  play_audio={args.play_audio}")
    print("=" * 70)

    proc = None
    try:
        proc = _spawn_backend(data_dir, port, log_path)
        if not _wait_for_socket(sock, proc, SOCKET_READY_TIMEOUT_SEC):
            print("FAIL: backend не поднялся", file=sys.stderr)
            return 1
        if not _lighten_settings(sock):
            print("FAIL: set_settings не подтвердил облегчение окружения", file=sys.stderr)
            return 1
        print("backend поднялся, окружение облегчено\n")

        load = _GpuContentionLoad(base_url, wav_path)

        print("--- (A) Латентность диктовки (transcribe_paths) baseline vs под фоном ---")
        result_a = _scenario_dictation_latency(sock, wav_path, load)
        print(json.dumps(result_a, ensure_ascii=False, indent=2))

        print("\n--- (B) Лаг live-субтитров baseline vs под фоном ---")
        result_b = _scenario_live_subs_lag(sock, load)
        print(json.dumps(result_b, ensure_ascii=False, indent=2))

        print("\n--- (C) Лаг снапшота встречи ---")
        result_c = _scenario_meeting_lag(sock, load, args.play_audio)
        print(json.dumps(result_c, ensure_ascii=False, indent=2))

        print("\n" + "=" * 70)
        print("ГОТОВО — числа выше идут в журнал замеров as-is (машина под нагрузкой, шумно).")
        print("=" * 70)
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
