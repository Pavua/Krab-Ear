import threading
from backend.llm_rewriter import CircuitBreaker


def test_llm_rewriter_circuit_breaker_concurrent():
    circuit = CircuitBreaker(fail_threshold=3, initial_reset_sec=60)

    errors = []

    def worker_mutate():
        try:
            for _ in range(500):
                circuit.record_failure()
                circuit.record_success()
        except Exception as e:
            errors.append(e)

    def worker_read():
        try:
            for _ in range(500):
                _ = circuit.state
                _ = circuit.allow_request()
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=worker_mutate)
    t2 = threading.Thread(target=worker_read)
    t3 = threading.Thread(target=worker_mutate)

    t1.start()
    t2.start()
    t3.start()
    t1.join()
    t2.join()
    t3.join()

    assert not errors, f"Errors occurred: {errors}"
