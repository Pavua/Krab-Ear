"""Тесты для MetricsCollector — потокобезопасный сборщик метрик со скользящим окном."""

from backend.metrics_collector import MetricsCollector
import sys
import os
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestRecordAndSnapshot(unittest.TestCase):
    """test_record_and_snapshot — запись нескольких значений и проверка структуры снапшота."""

    def test_record_and_snapshot(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.9)
        mc.record(200.0, 0.8)
        mc.record(150.0, 0.85)

        summary = mc.get_summary()

        self.assertIn("total_requests", summary)
        self.assertEqual(summary["total_requests"], 3)
        self.assertIn("error_rate", summary)
        self.assertIn("window_size", summary)
        self.assertEqual(summary["window_size"], 3)
        self.assertIn("stt_metrics", summary)

        stt = summary["stt_metrics"]
        self.assertIn("latency_ms", stt)
        self.assertIn("confidence", stt)

        lat = stt["latency_ms"]
        for key in ("p50", "p95", "p99", "avg"):
            self.assertIn(key, lat)

        conf = stt["confidence"]
        for key in ("avg", "min", "max"):
            self.assertIn(key, conf)


class TestPercentiles(unittest.TestCase):
    """test_percentiles — запись известных значений, проверка p50/p95/p99."""

    def test_percentiles(self):
        mc = MetricsCollector()
        # 100 значений: 1..100 ms, confidence фиксированный
        for i in range(1, 101):
            mc.record(float(i), 0.9)

        summary = mc.get_summary()
        lat = summary["stt_metrics"]["latency_ms"]

        # numpy percentile для 1..100: p50≈50.5, p95≈95.05, p99≈99.01
        self.assertAlmostEqual(lat["p50"], 50.5, delta=1.0)
        self.assertAlmostEqual(lat["p95"], 95.05, delta=1.0)
        self.assertAlmostEqual(lat["p99"], 99.01, delta=1.0)
        self.assertAlmostEqual(lat["avg"], 50.5, delta=0.5)

    def test_confidence_stats(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.5)
        mc.record(100.0, 0.7)
        mc.record(100.0, 0.9)

        summary = mc.get_summary()
        conf = summary["stt_metrics"]["confidence"]

        self.assertAlmostEqual(conf["min"], 0.5, places=3)
        self.assertAlmostEqual(conf["max"], 0.9, places=3)
        self.assertAlmostEqual(conf["avg"], 0.7, places=3)


class TestSlidingWindow(unittest.TestCase):
    """test_sliding_window — заполнение сверх размера окна, старые записи выпадают."""

    def test_sliding_window(self):
        window = 5
        mc = MetricsCollector(window_size=window)

        # Записываем 10 значений; первые 5 должны быть вытеснены
        for i in range(1, 11):
            mc.record(float(i * 10), 0.9)

        summary = mc.get_summary()
        # window_size == len(latencies) == 5
        self.assertEqual(summary["window_size"], window)
        # total_requests считает все 10
        self.assertEqual(summary["total_requests"], 10)

        # Средняя задержка должна быть по последним 5 записям: 60,70,80,90,100 → avg=80
        lat = summary["stt_metrics"]["latency_ms"]
        self.assertAlmostEqual(lat["avg"], 80.0, delta=0.1)

    def test_window_size_one(self):
        mc = MetricsCollector(window_size=1)
        mc.record(100.0, 0.9)
        mc.record(200.0, 0.8)

        summary = mc.get_summary()
        self.assertEqual(summary["window_size"], 1)
        # Осталась только последняя запись
        lat = summary["stt_metrics"]["latency_ms"]
        self.assertAlmostEqual(lat["avg"], 200.0, delta=0.1)


class TestErrorRate(unittest.TestCase):
    """test_error_rate — запись ошибок и проверка расчёта error_rate."""

    def test_error_rate(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.9, is_error=False)
        mc.record(0.0, 0.0, is_error=True)
        mc.record(0.0, 0.0, is_error=True)
        mc.record(150.0, 0.85, is_error=False)

        summary = mc.get_summary()
        # 2 ошибки из 4 запросов → 0.5
        self.assertAlmostEqual(summary["error_rate"], 0.5, places=4)
        self.assertEqual(summary["total_requests"], 4)

    def test_all_errors(self):
        mc = MetricsCollector()
        mc.record(0.0, 0.0, is_error=True)
        mc.record(0.0, 0.0, is_error=True)

        summary = mc.get_summary()
        # Все запросы — ошибки, данных latency нет → status waiting_data
        self.assertEqual(summary["error_rate"], 1.0)
        self.assertEqual(summary["total_requests"], 2)
        self.assertEqual(summary.get("status"), "waiting_data")

    def test_no_errors(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.9)
        mc.record(200.0, 0.8)

        summary = mc.get_summary()
        self.assertEqual(summary["error_rate"], 0.0)


class TestThreadSafety(unittest.TestCase):
    """test_thread_safety — запись из нескольких потоков без краша."""

    def test_thread_safety(self):
        mc = MetricsCollector(window_size=500)
        errors = []
        num_threads = 10
        records_per_thread = 100

        def worker():
            try:
                for i in range(records_per_thread):
                    mc.record(float(i), 0.9, is_error=(i % 10 == 0))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Исключения в потоках: {errors}")

        summary = mc.get_summary()
        self.assertEqual(summary["total_requests"], num_threads * records_per_thread)
        # error_rate должен быть ~0.1 (каждый 10-й)
        self.assertAlmostEqual(summary["error_rate"], 0.1, delta=0.01)


class TestEmptySnapshot(unittest.TestCase):
    """test_empty_snapshot — снапшот без данных возвращает разумные значения по умолчанию."""

    def test_empty_snapshot(self):
        mc = MetricsCollector()
        summary = mc.get_summary()

        # Должны быть базовые ключи
        self.assertIn("total_requests", summary)
        self.assertEqual(summary["total_requests"], 0)
        self.assertIn("error_rate", summary)
        self.assertEqual(summary["error_rate"], 0)
        # Статус-маркер вместо stt_metrics
        self.assertEqual(summary.get("status"), "waiting_data")
        # stt_metrics отсутствует, когда данных нет
        self.assertNotIn("stt_metrics", summary)

    def test_empty_after_only_errors(self):
        mc = MetricsCollector()
        mc.record(0.0, 0.0, is_error=True)

        summary = mc.get_summary()
        # Один запрос, одна ошибка, latency не записана → waiting_data
        self.assertEqual(summary["total_requests"], 1)
        self.assertEqual(summary["error_rate"], 1.0)
        self.assertEqual(summary.get("status"), "waiting_data")


class TestErrorsExcludedFromLatencyWindow(unittest.TestCase):
    """Записи с is_error=True не попадают в окно latency/confidence."""

    def test_error_records_not_in_latency(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.9, is_error=False)
        mc.record(9999.0, 0.0, is_error=True)   # ошибка — не должна влиять на p50
        mc.record(100.0, 0.9, is_error=False)

        summary = mc.get_summary()
        lat = summary["stt_metrics"]["latency_ms"]
        # Только два успешных: avg=100, p50=100; 9999 игнорируется
        self.assertAlmostEqual(lat["avg"], 100.0, delta=0.1)
        self.assertEqual(summary["window_size"], 2)
        self.assertEqual(summary["total_requests"], 3)

    def test_error_records_not_in_confidence(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.8, is_error=False)
        mc.record(0.0, 0.0, is_error=True)
        mc.record(100.0, 0.6, is_error=False)

        summary = mc.get_summary()
        conf = summary["stt_metrics"]["confidence"]
        # Только confidence 0.8 и 0.6 — avg=0.7, min=0.6
        self.assertAlmostEqual(conf["avg"], 0.7, places=2)
        self.assertAlmostEqual(conf["min"], 0.6, places=2)
        self.assertAlmostEqual(conf["max"], 0.8, places=2)


class TestConfidenceTracking(unittest.TestCase):
    """Отдельная фиксация: запись confidence и получение статистики."""

    def test_record_confidence_and_get_stats(self):
        """record() принимает confidence; get_summary() возвращает avg/min/max."""
        mc = MetricsCollector()
        values = [0.60, 0.75, 0.90, 0.85, 0.70]
        for v in values:
            mc.record(50.0, v)

        summary = mc.get_summary()
        conf = summary["stt_metrics"]["confidence"]

        self.assertAlmostEqual(conf["min"], 0.60, places=2)
        self.assertAlmostEqual(conf["max"], 0.90, places=2)
        expected_avg = sum(values) / len(values)
        self.assertAlmostEqual(conf["avg"], expected_avg, delta=0.01)

    def test_single_record_confidence(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.77)

        summary = mc.get_summary()
        conf = summary["stt_metrics"]["confidence"]
        self.assertAlmostEqual(conf["min"], 0.77, places=2)
        self.assertAlmostEqual(conf["max"], 0.77, places=2)
        self.assertAlmostEqual(conf["avg"], 0.77, places=2)


class TestSlidingWindowTimeExpiry(unittest.TestCase):
    """Скользящее окно: oldest samples выпадают при превышении maxsize."""

    def test_old_samples_replaced_by_new(self):
        """Деке с maxlen вытесняет старые записи при заполнении."""
        mc = MetricsCollector(window_size=3)
        # Три старых значения
        for val in [10.0, 20.0, 30.0]:
            mc.record(val, 0.9)
        # Добавляем новое — вытесняет 10.0
        mc.record(40.0, 0.9)

        summary = mc.get_summary()
        self.assertEqual(summary["window_size"], 3)
        # Оставшиеся: 20, 30, 40 → avg=30
        self.assertAlmostEqual(summary["stt_metrics"]["latency_ms"]["avg"], 30.0, delta=0.1)
        # total_requests считает все 4
        self.assertEqual(summary["total_requests"], 4)

    def test_window_overflow_percentiles(self):
        mc = MetricsCollector(window_size=10)
        # Записываем 20 значений — в окне останутся 11..20
        for i in range(1, 21):
            mc.record(float(i), 0.9)

        summary = mc.get_summary()
        self.assertEqual(summary["window_size"], 10)
        # avg(11..20) = 15.5
        self.assertAlmostEqual(summary["stt_metrics"]["latency_ms"]["avg"], 15.5, delta=0.1)


class TestNonFiniteSafety(unittest.TestCase):
    """W966 F2 — NaN/Inf samples must not crash get_summary() or json.dumps()."""

    def test_record_silently_drops_nan_latency(self):
        """record() with NaN latency is silently dropped; total_requests not incremented."""
        import math
        mc = MetricsCollector()
        mc.record(100.0, 0.9)
        mc.record(float("nan"), 0.8)  # должна быть отброшена

        summary = mc.get_summary()
        # Только одна запись прошла
        self.assertEqual(summary["window_size"], 1)
        # total_requests не должен считать отброшенный сэмпл
        self.assertEqual(summary["total_requests"], 1)
        self.assertAlmostEqual(summary["stt_metrics"]["latency_ms"]["avg"], 100.0, delta=0.1)

    def test_record_silently_drops_inf_confidence(self):
        """record() with Inf confidence is silently dropped."""
        mc = MetricsCollector()
        mc.record(200.0, 0.7)
        mc.record(150.0, float("inf"))  # должна быть отброшена

        summary = mc.get_summary()
        self.assertEqual(summary["window_size"], 1)
        self.assertEqual(summary["total_requests"], 1)
        self.assertAlmostEqual(summary["stt_metrics"]["confidence"]["avg"], 0.7, delta=0.01)

    def test_get_summary_after_nan_does_not_raise(self):
        """get_summary() result is JSON-serialisable even after NaN/Inf attempts."""
        import json
        mc = MetricsCollector()
        mc.record(100.0, 0.9)
        # Попытки записи не-конечных значений — все отбрасываются
        mc.record(float("nan"), 0.5)
        mc.record(100.0, float("nan"))
        mc.record(float("inf"), 0.5)
        mc.record(-float("inf"), 0.5)

        summary = mc.get_summary()
        # Должно сериализоваться без TypeError/ValueError
        serialized = json.dumps(summary)
        self.assertIsInstance(serialized, str)
        # Только один валидный сэмпл
        self.assertEqual(summary["window_size"], 1)


if __name__ == "__main__":
    unittest.main()
