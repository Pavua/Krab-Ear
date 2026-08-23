"""HMAC-цепочка privacy_audit обязана переживать запись из НЕСКОЛЬКИХ ПРОЦЕССОВ.

Живой инцидент 2026-08-23: `verify_chain()` на боевом логе владельца вернул
`valid=False, first_broken_index=2784` из 50 041 записи; разрыв датирован 03.06.
Механизм виден прямо в данных — запись 2783 даёт `hash=3478eb…`, а следующая
ссылается на `prev=9b6633…`, чужой хеш.

Корень (TOCTOU): `log_event` вычисляет `prev_hash`/`entry_hash` из КЭША
`self._last_hash` ДО захвата `flock`, а лок берётся только вокруг физической
записи. Между вычислением и записью другой процесс успевает дописать свою
запись — и цепочка расходится. `self._log_lock` (threading.Lock) защищает
только внутрипроцессную конкуренцию и между процессами бесполезен.

Писателей у лога минимум три: backend (`service.py`), REST (`rest_server.py`
импортирует `core.engine` + `backend.observability`) и тесты.

🔴 Тест НАМЕРЕННО кросс-процессный (subprocess, не threading): внутрипроцессную
гонку закрывает существующий `_log_lock`, и тред-тест остался бы зелёным при
полностью сломанном механизме — то есть проверял бы дыру.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.privacy_audit import PrivacyAuditLogger  # noqa: E402

# Каждый процесс пишет свою серию; параллелизм должен быть достаточным,
# чтобы окно TOCTOU открывалось надёжно, а не от случая к случаю.
_WRITERS = 4
_EVENTS_PER_WRITER = 25

_CHILD_SOURCE = """
import sys
sys.path.insert(0, {krab_ear!r})
from pathlib import Path
from backend.privacy_audit import PrivacyAuditLogger

log_path = Path({log_path!r})
tag = sys.argv[1]
lg = PrivacyAuditLogger(log_path=log_path)
for i in range({events}):
    lg.log_event("privacy", "concurrent_write", {{"writer": tag, "seq": i}})
"""


class PrivacyAuditChainMultiprocessTest(unittest.TestCase):
    """Цепочка остаётся валидной, когда в лог пишут параллельные процессы."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.log_path = Path(self.tmpdir.name) / "privacy_audit.log"
        PrivacyAuditLogger.reset_instance()
        self.addCleanup(PrivacyAuditLogger.reset_instance)
        # Первая запись создаёт HMAC-ключ в каталоге ДО старта детей —
        # иначе они гонялись бы ещё и за создание ключа, и упавшая цепочка
        # объяснялась бы разными ключами, а не отсутствием лока.
        PrivacyAuditLogger(log_path=self.log_path).log_event(
            "privacy", "seed", {"note": "ключ создаётся до конкуренции"}
        )

    def _spawn_writers(self) -> None:
        source = _CHILD_SOURCE.format(
            krab_ear=str(PROJECT_ROOT / "KrabEar"),
            log_path=str(self.log_path),
            events=_EVENTS_PER_WRITER,
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT / "KrabEar")
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", source, f"w{idx}"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for idx in range(_WRITERS)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=180)
            self.assertEqual(
                proc.returncode, 0, f"писатель упал: {err.decode('utf-8', 'replace')[-500:]}"
            )

    def test_chain_survives_concurrent_processes(self):
        self._spawn_writers()

        PrivacyAuditLogger.reset_instance()
        verifier = PrivacyAuditLogger(log_path=self.log_path)
        result = verifier.verify_chain()

        expected = 1 + _WRITERS * _EVENTS_PER_WRITER
        self.assertEqual(
            result["checked"], expected,
            f"часть записей потерялась: ожидали {expected}, в логе {result['checked']}",
        )
        self.assertTrue(
            result["valid"],
            "HMAC-цепочка разорвана параллельной записью: первая битая запись "
            f"#{result['first_broken_index']} из {result['checked']}. "
            "prev_hash обязан вычисляться из ФАЙЛА под тем же flock, что и запись.",
        )

    def test_no_entry_lost_and_prev_hash_links_are_unique(self):
        """Ни одна запись не потеряна и ни на один хеш не ссылаются дважды.

        Две записи с одинаковым prev_hash — это ветвление цепочки: обе «законны»
        по отдельности, но одна затирает историю другой. verify_chain читает
        линейно и такое ветвление может не заметить, поэтому проверяем отдельно.
        """
        self._spawn_writers()

        entries = []
        with self.log_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))

        self.assertEqual(len(entries), 1 + _WRITERS * _EVENTS_PER_WRITER)

        prev_links = [e["prev_hash"] for e in entries if e.get("prev_hash") is not None]
        duplicates = {h for h in prev_links if prev_links.count(h) > 1}
        self.assertEqual(
            duplicates, set(),
            f"цепочка разветвилась: на {len(duplicates)} хеш(ей) ссылаются несколько "
            "записей — параллельные процессы считали один и тот же tip",
        )


if __name__ == "__main__":
    unittest.main()
