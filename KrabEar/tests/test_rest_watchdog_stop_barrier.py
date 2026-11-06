"""Не теряем живой тред watchdog при незавершённом stop()."""

import threading
from unittest import mock

from backend.rest_watchdog import RestWatchdog


def test_timed_out_stop_preserves_thread_and_blocks_restart():
    entered = threading.Event()
    release = threading.Event()
    watchdog = RestWatchdog(owner=object(), tick_interval_sec=0.001)
    watchdog.check_once = lambda: (entered.set(), release.wait(5))
    try:
        watchdog.start()
        assert entered.wait(2)
        thread = watchdog._thread
        with mock.patch("backend.rest_watchdog.STOP_JOIN_TIMEOUT_SEC", 0.01):
            assert watchdog.stop() is False
        assert watchdog._thread is thread
        watchdog.start()
        assert watchdog._thread is thread
        assert watchdog._stop_event.is_set()
    finally:
        release.set()
        watchdog.stop()
        if watchdog._thread is not None:
            watchdog._thread.join(2)
            assert not watchdog._thread.is_alive()


def test_concurrent_stop_and_start_cannot_clear_stop_event():
    entered = threading.Event()
    release = threading.Event()
    watchdog = RestWatchdog(owner=object(), tick_interval_sec=0.001)
    watchdog.check_once = lambda: (entered.set(), release.wait(5))
    result = []
    stopper = None
    try:
        watchdog.start()
        assert entered.wait(2)
        thread = watchdog._thread
        stopper = threading.Thread(target=lambda: result.append(watchdog.stop()))
        stopper.start()
        assert watchdog._stop_event.wait(2)
        assert watchdog.stop() is False
        watchdog.start()
        assert watchdog._thread is thread
        assert watchdog._stop_event.is_set()
    finally:
        release.set()
        worker = watchdog._thread
        try:
            if stopper is not None and stopper.ident is not None:
                stopper.join(2)
        finally:
            watchdog.stop()
            if worker is not None and worker.ident is not None:
                worker.join(2)
        if stopper is not None and stopper.ident is not None:
            assert not stopper.is_alive()
        if worker is not None and worker.ident is not None:
            assert not worker.is_alive()
    assert result == [True]


def test_stop_without_thread_reports_success():
    assert RestWatchdog(owner=object()).stop() is True


def test_ambiguous_thread_start_stays_unknown_even_if_not_alive_yet():
    watchdog = RestWatchdog(owner=object())
    thread = mock.Mock()
    thread.start.side_effect = RuntimeError("synthetic ambiguous launch")
    thread.is_alive.return_value = False
    with mock.patch("backend.rest_watchdog.threading.Thread", return_value=thread) as factory:
        try:
            watchdog.start()
        except RuntimeError:
            pass
        else:
            raise AssertionError("start failure must propagate")
        assert watchdog.stop() is False
        assert watchdog._thread is thread
        assert watchdog._stop_event.is_set()
        watchdog.start()
        assert factory.call_count == 1


def test_interrupted_join_remains_unknown_if_thread_appears_dead():
    watchdog = RestWatchdog(owner=object())
    thread = mock.Mock()
    thread.is_alive.side_effect = [True, False]
    thread.join.side_effect = KeyboardInterrupt
    watchdog._thread = thread
    try:
        watchdog.stop()
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("join interruption must propagate")
    assert watchdog.stop() is False
    assert watchdog._thread is thread
    with mock.patch("backend.rest_watchdog.threading.Thread") as factory:
        watchdog.start()
        factory.assert_not_called()
