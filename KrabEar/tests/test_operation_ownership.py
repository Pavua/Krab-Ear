"""Контракт owned subtree: настоящие потоки/executor, без приложения и моделей."""
from concurrent.futures import Future, ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
import threading
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'core/operation_ownership.py'
spec = importlib.util.spec_from_file_location('ear_owned_contract', SOURCE)
owned = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = owned
spec.loader.exec_module(owned)
OwnedOperations, submit_owned = owned.OwnedOperations, owned.submit_owned


class OwnedOperationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.pools = []
        self.addCleanup(self.cleanup)
        self.owner = OwnedOperations(scope_id='rest_post_native_adapter')

    def cleanup(self):
        for event in self.events:
            event.set()
        for pool in self.pools:
            pool.shutdown(wait=True, cancel_futures=True)

    def pool(self):
        pool = ThreadPoolExecutor(max_workers=1)
        self.pools.append(pool)
        return pool

    def barrier(self):
        entered, release = threading.Event(), threading.Event()
        self.events.append(release)
        def leaf():
            entered.set()
            if not release.wait(3):
                raise AssertionError('owned test barrier not released')
            return 42
        return entered, release, leaf

    def node(self, token):
        return next(item for item in self.owner.snapshot()['operations']
                    if item['operation_id'] == token.operation_id)

    def invariant(self):
        snapshot = self.owner.snapshot()
        self.assertEqual(snapshot['schema_version'], 1)
        self.assertFalse(snapshot['scope_complete'])
        self.assertEqual(snapshot['admission'], 'open')
        self.assertIsNone(snapshot['fence_result'])
        return snapshot

    def test_root_and_child_ids_revision_and_sealed_completion(self):
        root = self.owner.admit_root()
        before = self.invariant()
        child = self.owner.register_child(root)
        self.assertNotEqual(root.operation_id, child.operation_id)
        self.assertEqual(self.node(child)['parent_operation_id'], root.operation_id)
        self.assertEqual(self.owner.run(child, lambda: 9), 9)
        self.assertEqual(self.node(root)['state'], 'running')
        self.assertEqual(self.owner.run(root, lambda: 7), 7)
        self.assertEqual(self.node(root)['state'], 'completed')
        self.assertTrue(self.node(root)['registration_sealed'])
        self.assertGreater(self.invariant()['revision'], before['revision'])

    def test_outer_return_does_not_release_running_adapter(self):
        pool = self.pool()
        entered, release, leaf = self.barrier()
        root = self.owner.admit_root()
        def outer():
            future = submit_owned(pool, leaf)
            self.assertTrue(entered.wait(1))
            return future
        future = self.owner.run(root, outer)
        self.assertFalse(future.done())
        state = self.node(root)
        self.assertEqual(state['callable_state'], 'completed')
        self.assertTrue(state['registration_sealed'])
        self.assertEqual(state['state'], 'running')
        children = [n for n in self.invariant()['operations'] if n['parent_operation_id'] == root.operation_id]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['state'], 'running')
        release.set()
        self.assertEqual(future.result(1), 42)
        self.assertEqual(self.node(root)['state'], 'completed')

    def test_nested_grandchild_remains_owned_after_parent_seals(self):
        first, second = self.pool(), self.pool()
        entered, release, leaf = self.barrier()
        root = self.owner.admit_root()
        child_future = self.owner.run(root, lambda: submit_owned(first, lambda: submit_owned(second, leaf)))
        grandchild_future = child_future.result(1)
        self.assertTrue(entered.wait(1))
        snapshot = self.invariant()
        self.assertEqual(len(snapshot['operations']), 3)
        self.assertEqual(self.node(root)['state'], 'running')
        child = next(n for n in snapshot['operations'] if n['parent_operation_id'] == root.operation_id)
        self.assertEqual(child['callable_state'], 'completed')
        self.assertTrue(child['registration_sealed'])
        self.assertEqual(child['state'], 'running')
        grandchild = next(n for n in snapshot['operations'] if n['parent_operation_id'] == child['operation_id'])
        self.assertEqual(grandchild['state'], 'running')
        release.set()
        self.assertEqual(grandchild_future.result(1), 42)
        self.assertEqual(self.node(root)['state'], 'completed')

    def test_exception_terminalizes_real_callable_and_preserves_exception(self):
        root = self.owner.admit_root()
        error = ValueError('synthetic failure')
        def fail():
            raise error
        future = self.pool().submit(self.owner.run, root, fail)
        self.owner.follow_future(root, future)
        with self.assertRaises(ValueError) as raised:
            future.result(1)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.node(root)['state'], 'completed')
        self.invariant()

    def test_cancel_before_start_releases_only_never_started_child(self):
        pool = self.pool()
        entered, release, leaf = self.barrier()
        blocker = pool.submit(leaf)
        self.assertTrue(entered.wait(1))
        root = self.owner.admit_root()
        called = []
        future = self.owner.run(root, lambda: submit_owned(pool, lambda: called.append(True)))
        self.assertTrue(future.cancel())
        self.assertEqual(self.node(root)['state'], 'completed')
        self.assertEqual(called, [])
        release.set()
        blocker.result(1)
        self.invariant()

    def test_cancel_running_does_not_release_child(self):
        entered, release, leaf = self.barrier()
        pool = self.pool()
        root = self.owner.admit_root()
        future = self.owner.run(root, lambda: submit_owned(pool, leaf))
        self.assertTrue(entered.wait(1))
        self.assertFalse(future.cancel())
        self.assertEqual(self.node(root)['state'], 'running')
        release.set()
        future.result(1)
        self.assertEqual(self.node(root)['state'], 'completed')
        self.invariant()

    def test_submit_failure_retains_unknown_and_rethrows(self):
        entered, release, leaf = self.barrier()
        pool = self.pool()
        error = RuntimeError('ambiguous submit after actual dispatch')
        handles = []
        class AmbiguousExecutor:
            def submit(self, fn, *args, **kwargs):
                handles.append(pool.submit(fn, *args, **kwargs))
                if not entered.wait(1):
                    raise AssertionError('dispatch never entered leaf')
                raise error
        root = self.owner.admit_root()
        with self.assertRaises(RuntimeError) as raised:
            self.owner.run(root, lambda: submit_owned(AmbiguousExecutor(), leaf))
        self.assertIs(raised.exception, error)
        self.assertEqual(self.node(root)['state'], 'unknown')
        self.assertFalse(handles[0].done())
        release.set()
        handles[0].result(1)
        # Only exact wrapper completion can settle dispatch ambiguity; never outer result.
        self.invariant()

    def test_seal_vs_late_child_registration_retains_uncertainty(self):
        entered, release, leaf = self.barrier()
        pool = self.pool()
        root = self.owner.admit_root()
        def late_registration():
            leaf()
            return self.owner.register_child(root)
        late = pool.submit(late_registration)
        self.assertTrue(entered.wait(1))
        self.owner.run(root, lambda: None)
        self.assertTrue(self.node(root)['registration_sealed'])
        release.set()
        self.assertIsNone(late.result(1))
        self.assertEqual(self.node(root)['state'], 'unknown')
        self.invariant()

    def test_foreign_epoch_duplicate_callback_and_fork_reset_cannot_clear_debt(self):
        root = self.owner.admit_root()
        future = self.pool().submit(self.owner.run, root, lambda: None)
        future.result(1)
        self.owner.follow_future(root, future)
        revision = self.invariant()['revision']
        self.owner.follow_future(root, future)
        self.assertEqual(self.invariant()['revision'], revision)
        old_epoch = self.invariant()['owner_epoch']
        self.owner._after_fork()  # pure incarnation seam; never forks any process
        fresh = self.owner.admit_root()
        snapshot = self.invariant()
        self.assertNotEqual(snapshot['owner_epoch'], old_epoch)
        self.assertTrue(snapshot['coverage_lost'])
        self.owner.follow_future(root, future)
        self.owner.mark_unknown(root)
        self.assertEqual(self.node(fresh)['state'], 'running')
        self.assertEqual(len(self.invariant()['operations']), 1)

    def test_capacity_overflow_is_unknown_without_blocking_callable(self):
        self.owner = OwnedOperations(scope_id='rest_post_native_adapter', capacity=2)
        first, second = self.owner.admit_root(), self.owner.admit_root()
        overflow = self.owner.admit_root()
        self.assertIsNone(overflow)
        self.assertEqual(self.owner.run(overflow, lambda: 'still runs'), 'still runs')
        self.assertEqual(self.owner.run(first, lambda: 1), 1)
        self.assertEqual(self.owner.run(second, lambda: 2), 2)
        snapshot = self.invariant()
        self.assertTrue(snapshot['coverage_lost'])
        self.assertLessEqual(len(snapshot['operations']), 2)

    def test_alien_cancelled_future_cannot_complete_queued_owner(self):
        pool = self.pool()
        entered, release, leaf = self.barrier()
        blocker = pool.submit(leaf)
        self.assertTrue(entered.wait(1))
        root = self.owner.admit_root()
        ran = []
        queued = pool.submit(self.owner.run, root, lambda: ran.append(True))
        self.owner.follow_future(root, queued)
        alien = Future()
        self.assertTrue(alien.cancel())
        self.owner.follow_future(root, alien)
        self.assertFalse(queued.done())
        self.assertEqual(ran, [])
        self.assertNotEqual(self.node(root)['state'], 'completed',
                            'ALIEN_FUTURE_COMPLETED_QUEUED_OWNER')
        release.set()
        blocker.result(1)
        queued.result(1)
