# Atomic Backend Socket Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Защитить backend Unix socket атомарным exact-endpoint flock и сделать startup diagnostics truthful до bind и после TTL.

**Architecture:** Новый `backend/socket_ownership.py` владеет canonical path, общим read-only probe и стабильным sidecar claim. `main()` захватывает claim до StateStore/Sentry/build_service и держит его до конца shutdown; `IPCServer` использует тот же claim для identity-safe bind/cleanup, а `StartupDiagnostics` получает immutable snapshot и точный socket path.

**Tech Stack:** Python 3.12+, POSIX `fcntl.flock`, Unix `AF_UNIX` sockets, `pathlib`, `unittest`/pytest, subprocess-based contention tests.

---

## File map

- Create `KrabEar/backend/socket_ownership.py`: canonical path, probe enums/data,
  ownership exceptions, `SocketOwnershipClaim` lifecycle.
- Create `KrabEar/tests/test_socket_ownership.py`: real filesystem/socket and
  fresh-process ownership tests.
- Modify `KrabEar/backend/ipc_server.py`: mandatory claim before stale cleanup,
  bound inode recording, identity-safe cleanup.
- Modify `KrabEar/tests/test_ipc_server_wave1767.py`: listener preservation,
  failure cleanup and replacement-inode regressions.
- Modify `KrabEar/backend/startup_diagnostics.py`: shared probe, exact path and
  snapshot-aware self-owner results.
- Modify `KrabEar/tests/test_startup_diagnostics.py`: path/status/identity cases.
- Modify `KrabEar/backend/service.py`: early production claim, optional wiring
  through `build_service`/`BackendService`, outer release after shutdown.
- Modify `KrabEar/tests/test_ipc_server_dedup_wave1768.py`: updated production
  constructor source contract.
- Create `KrabEar/tests/test_socket_ownership_wiring.py`: AST/main ordering and
  build-service propagation without constructing production resources.
- Create `.remember/FROM_CODEX_2026-08-22_M.md`: final exact-SHA handoff (ignored).

### Task 1: Ownership primitive and shared probe

**Files:**

- Create: `KrabEar/backend/socket_ownership.py`
- Create: `KrabEar/tests/test_socket_ownership.py`

- [ ] **Step 1: Write probe and claim RED tests**

Use temp paths only. Import inside each test so a missing module is a normal
test failure, not collection failure. The first group must cover this public
surface:

```python
def test_probe_distinguishes_missing_listening_stale_and_occupied(tmp_path):
    ownership = importlib.import_module("backend.socket_ownership")
    missing = tmp_path / "missing.sock"
    assert ownership.probe_unix_socket_path(missing).status is ownership.SocketPathStatus.MISSING

    live_path = tmp_path / "live.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(live_path))
    listener.listen(1)
    try:
        assert ownership.probe_unix_socket_path(live_path).status is ownership.SocketPathStatus.LISTENING
    finally:
        listener.close()
    assert ownership.probe_unix_socket_path(live_path).status is ownership.SocketPathStatus.STALE

    regular = tmp_path / "regular.sock"
    regular.write_text("keep", encoding="utf-8")
    assert ownership.probe_unix_socket_path(regular).status is ownership.SocketPathStatus.OCCUPIED
    assert regular.read_text(encoding="utf-8") == "keep"
```

Add separate tests for final symlink → `OCCUPIED`, ambiguous connect error →
`OCCUPIED`, canonical parent aliases, stable sidecar inode, idempotent release,
live legacy listener preservation, stale cleanup with inode re-check, fresh
subprocess contention, and reacquire after holder `os._exit(0)`.

- [ ] **Step 2: Run RED**

Run:

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_socket_ownership.py -v
```

Expected: FAIL because `backend.socket_ownership` does not exist.

- [ ] **Step 3: Implement the minimal ownership module**

Define concrete types and signatures used by all later tasks:

```python
class SocketPathStatus(str, Enum):
    MISSING = "missing"
    LISTENING = "listening"
    STALE = "stale"
    OCCUPIED = "occupied"


class SocketOwnershipState(str, Enum):
    UNCLAIMED = "unclaimed"
    CLAIMED = "claimed"
    LISTENING = "listening"


class SocketOwnershipError(RuntimeError):
    pass


class SocketAlreadyOwnedError(SocketOwnershipError):
    pass


class UnsafeSocketPathError(SocketOwnershipError):
    pass


@dataclass(frozen=True)
class SocketIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class SocketPathProbe:
    status: SocketPathStatus
    identity: SocketIdentity | None
    error: str | None = None


@dataclass(frozen=True)
class SocketOwnershipSnapshot:
    socket_path: Path
    state: SocketOwnershipState
    bound_identity: SocketIdentity | None
```

`probe_unix_socket_path()` must use `lstat`, reject non-sockets/symlinks, close
its probe FD in every branch, and classify only `ECONNREFUSED` as stale.
`canonical_socket_path()` resolves only the parent and appends the final name
back unchanged, so parent aliases share a lock domain while a final symlink is
still visible to `lstat` as `OCCUPIED`.

`SocketOwnershipClaim.acquire()` opens `<canonical-socket>.lock` with
`O_CREAT|O_RDWR|O_CLOEXEC` plus `O_NOFOLLOW` when available, validates
regular-file/effective-UID ownership, forces `0600`, and takes
`LOCK_EX|LOCK_NB`. On contention it closes the contender FD and raises
`SocketAlreadyOwnedError`. Invalid sidecar/path ownership, non-regular lock
files, `OCCUPIED` socket paths and identity changes before unlink raise
`UnsafeSocketPathError`.

`prepare_for_bind()` requires a held claim. It raises `SocketAlreadyOwnedError`
for `LISTENING` and `UnsafeSocketPathError` for `OCCUPIED`; for `STALE`, it
repeats `lstat` and unlinks only the same Unix socket identity.
`record_bound_socket`, `mark_listening`, `cleanup_bound_socket`,
`snapshot`, and mutex-serialized `release` implement the design spec exactly.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Expected: all tests in
`test_socket_ownership.py` PASS with no leaked child process or socket.

- [ ] **Step 5: Commit Task 1**

```bash
git add KrabEar/backend/socket_ownership.py KrabEar/tests/test_socket_ownership.py
git commit -m "feat(ipc): add atomic socket ownership claim"
```

### Task 2: Make IPCServer enforce ownership and inode-safe cleanup

**Files:**

- Modify: `KrabEar/backend/ipc_server.py`
- Modify: `KrabEar/tests/test_ipc_server_wave1767.py`

- [ ] **Step 1: Add IPCServer RED tests**

Add real-socket tests with a minimal fake service:

```python
def test_contender_server_preserves_live_listener(self):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(self.socket_path))
    listener.listen(1)
    original = self.socket_path.lstat()
    try:
        contender = IPCServer(socket_path=self.socket_path, service=MagicMock())
        with self.assertRaises(SocketAlreadyOwnedError):
            contender.serve_forever()
        current = self.socket_path.lstat()
        self.assertEqual((current.st_dev, current.st_ino), (original.st_dev, original.st_ino))
    finally:
        listener.close()
```

Add focused tests proving:

- regular/symlink paths survive startup failure;
- bind, chmod and listen failures close the listener, restore umask, clean only
  the newly-bound inode and release only a locally-created claim;
- normal stop removes the server's own inode;
- after the first server's pathname is externally removed and replaced by a
  second real listener, cleanup of the first preserves the replacement inode.

- [ ] **Step 2: Run IPCServer RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_ipc_server_wave1767.py -v
```

Expected: new preservation/ownership tests FAIL because current
`serve_forever()` unconditionally unlinks before bind and in `finally`.

- [ ] **Step 3: Implement ownership integration**

Keep the two positional constructor arguments and add one keyword-only argument:

```python
def __init__(
    self,
    socket_path: Path,
    service: "BackendService",
    *,
    ownership: SocketOwnershipClaim | None = None,
) -> None:
```

At `serve_forever()` entry, use the injected held claim or create/acquire a local
one. Always call `prepare_for_bind()` before opening the listener. Immediately
after successful bind call `record_bound_socket()`, and only after successful
listen call `mark_listening()`.

In `finally`, close the listener, call `cleanup_bound_socket()`, and release only
the local claim. Production's injected claim remains held for outer process
lifecycle. Remove both unconditional `Path.unlink()` sites.

- [ ] **Step 4: Run IPCServer GREEN and Task 1 regression**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_socket_ownership.py \
  KrabEar/tests/test_ipc_server_wave1767.py -v
```

Expected: both files PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add KrabEar/backend/ipc_server.py KrabEar/tests/test_ipc_server_wave1767.py
git commit -m "fix(ipc): protect listener lifecycle with ownership claim"
```

### Task 3: Make startup diagnostics exact-path and self-owner aware

**Files:**

- Modify: `KrabEar/backend/startup_diagnostics.py`
- Modify: `KrabEar/tests/test_startup_diagnostics.py`
- Modify: `KrabEar/backend/service.py` (`BackendService` and `build_service` only)
- Create: `KrabEar/tests/test_socket_ownership_wiring.py` (factory portion)

- [ ] **Step 1: Add diagnostics RED tests**

Use real temp sockets and synthetic immutable snapshots:

```python
def test_default_socket_path_uses_data_dir_krabear_sock(self):
    diag = StartupDiagnostics(data_dir=self.tmpdir)
    result = diag._check_socket_path_available()
    self.assertEqual(result.details["path"], str(Path(self.tmpdir) / "krabear.sock"))


def test_owned_listener_with_matching_inode_is_self_ok(self):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(self.socket_path))
    listener.listen(1)
    stat_result = self.socket_path.lstat()
    snapshot = SocketOwnershipSnapshot(
        socket_path=self.socket_path,
        state=SocketOwnershipState.LISTENING,
        bound_identity=SocketIdentity(stat_result.st_dev, stat_result.st_ino),
    )
    diag = StartupDiagnostics(
        data_dir=self.tmpdir,
        socket_path=self.socket_path,
        socket_ownership_snapshot_getter=lambda: snapshot,
    )
    try:
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.details["owner"], "self")
    finally:
        listener.close()
```

Add separate tests for exact custom path, `CLAIMED+MISSING`, foreign listener,
stale socket, `OCCUPIED`, and `LISTENING` with missing/mismatched inode → warning.

- [ ] **Step 2: Run diagnostics RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_startup_diagnostics.py -v
```

Expected: fallback-path and snapshot-aware tests FAIL; the 60 pre-existing tests
remain green.

- [ ] **Step 3: Implement diagnostics and factory wiring**

Preserve existing positional `StartupDiagnostics` parameters and add:

```python
*,
socket_ownership_snapshot_getter: Callable[[], SocketOwnershipSnapshot] | None = None,
```

Replace the local connect logic with `probe_unix_socket_path`. Resolve fallback
through the shared `default_socket_path`, prefer `self._data_dir`, and map the
probe/snapshot matrix exactly as the design spec states. A listener is `self`
only when state and bound identity both match the probe.

Add matching optional keyword-only parameters to `BackendService.__init__` and
`build_service`, then pass them to `StartupDiagnostics`. Existing calls without
these parameters remain valid.

In `test_socket_ownership_wiring.py`, patch `BackendService` and assert
`build_service(data_dir, socket_path=..., socket_ownership_snapshot_getter=...)`
forwards both values without invoking production resources.

- [ ] **Step 4: Run diagnostics/factory GREEN**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_startup_diagnostics.py \
  KrabEar/tests/test_socket_ownership_wiring.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add KrabEar/backend/startup_diagnostics.py KrabEar/backend/service.py \
  KrabEar/tests/test_startup_diagnostics.py \
  KrabEar/tests/test_socket_ownership_wiring.py
git commit -m "fix(diagnostics): report exact owned backend socket"
```

### Task 4: Wire early claim through production main lifecycle

**Files:**

- Modify: `KrabEar/backend/service.py` (`main` lifecycle)
- Modify: `KrabEar/tests/test_socket_ownership_wiring.py` (main AST contract)
- Modify: `KrabEar/tests/test_ipc_server_dedup_wave1768.py`

- [ ] **Step 1: Add main-order RED tests**

Parse `service.py::main` with AST/source positions and assert:

```python
assert main_body.index("ownership.acquire()") < main_body.index("_early_store = StateStore")
assert main_body.index("ownership.acquire()") < main_body.index("init_sentry(")
assert main_body.index("ownership.acquire()") < main_body.index("build_service(")
assert main_body.index("_shutdown_backend(") < main_body.rindex("ownership.release()")
assert "ownership=ownership" in main_body
assert "socket_ownership_snapshot_getter=ownership.snapshot" in main_body
```

Update the W1768 constructor assertion to require the production ownership
argument while still proving `backend.ipc_server.IPCServer` is the live class.

- [ ] **Step 2: Run wiring RED**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_socket_ownership_wiring.py \
  KrabEar/tests/test_ipc_server_dedup_wave1768.py -v
```

Expected: ordering and production ownership assertions FAIL.

- [ ] **Step 3: Implement main lifecycle**

Import `canonical_socket_path`, `default_socket_path`, exceptions and claim.
Normalize the CLI socket exactly once with `canonical_socket_path`, call existing
`configure_logging`, then acquire and prepare the claim before `_early_store`.

On `SocketAlreadyOwnedError`, log without secrets and raise
`SystemExit(os.EX_TEMPFAIL)`. On unsafe/open path failure, raise
`SystemExit(os.EX_CANTCREAT)`. Do not construct service in either branch.

Wrap the remainder in an outer `try/finally` that calls `ownership.release()`.
Pass `socket_path` and `ownership.snapshot` to `build_service`; pass the same
claim to `IPCServer`. Preserve the signal callback and the single
`_shutdown_backend` coordinator unchanged inside its existing `finally`.

- [ ] **Step 4: Run complete targeted GREEN**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_socket_ownership.py \
  KrabEar/tests/test_ipc_server_wave1767.py \
  KrabEar/tests/test_startup_diagnostics.py \
  KrabEar/tests/test_socket_ownership_wiring.py \
  KrabEar/tests/test_ipc_server_dedup_wave1768.py \
  KrabEar/tests/test_shutdown_handler_wired_in_main.py \
  KrabEar/tests/test_bounded_single_owner_shutdown_W1787.py -v
```

Expected: all targeted tests PASS; no production path or process touched.

- [ ] **Step 5: Commit Task 4**

```bash
git add KrabEar/backend/service.py \
  KrabEar/tests/test_socket_ownership_wiring.py \
  KrabEar/tests/test_ipc_server_dedup_wave1768.py
git commit -m "fix(backend): claim IPC endpoint before startup"
```

### Task 5: Required gates, whole-diff review and handoff

**Files:**

- Create: `.remember/FROM_CODEX_2026-08-22_M.md` (ignored handoff)
- Update: `.remember/remember.md` (ignored rolling continuation note)
- Update: `/Users/pablito/.codex/memories/extensions/ad_hoc/notes/20260822T181217-krab-ear-socket-ownership-m-plan.md` only because the user explicitly requested persistent memory continuity.

- [ ] **Step 1: Re-run all targeted pytest files**

Use the Task 4 Step 4 command. Expected: all PASS, no warnings from worker
threads or leaked subprocesses.

- [ ] **Step 2: Run Python 3.12 parity per changed test file**

```bash
scripts/pre_merge_py312_check.sh \
  KrabEar/tests/test_socket_ownership.py \
  KrabEar/tests/test_ipc_server_wave1767.py \
  KrabEar/tests/test_startup_diagnostics.py \
  KrabEar/tests/test_socket_ownership_wiring.py \
  KrabEar/tests/test_ipc_server_dedup_wave1768.py
```

Expected: ALL GREEN with mlx absent. If the harness enforces one file per
invocation, run the same five paths separately and record each result.

- [ ] **Step 3: Run exact CI flake8**

```bash
flake8 KrabEar/ \
  --max-line-length=150 \
  --extend-ignore=E501 \
  --exclude=KrabEar/.venv,KrabEar/tests/__pycache__,.venv_krab_ear,KrabEar/_legacy_tkinter_archive_2026-02-11 \
  --per-file-ignores='KrabEar/tests/*:F401,F541,F841,E203,E301,E302,E303,E305,E306,E401,E402,W391' \
  --statistics
```

Expected: exit 0, no output.

- [ ] **Step 4: Run backend audit and diff checks**

```bash
make audit-all
git diff --check 798e7d00b9cb5b6d1eab082642696e6b790c4e9d..HEAD
git status --short
```

Expected: all audits PASS, no whitespace errors, clean tracked worktree.

- [ ] **Step 5: Sol Ultra whole-diff review**

Review exact diff from `798e7d00` for lock-domain splitting, early-release,
foreign-path unlink, FD inheritance/leak, bind/listen phase errors, diagnostic
identity mismatch, shutdown ordering and test-only wiring. Fix any real finding
with a new RED regression and rerun affected gates.

- [ ] **Step 6: Write handoff and persistent continuation notes**

Record branch, final SHA, commits, files, RED evidence, every gate result,
remaining risks, no-push/no-merge/no-restart, and the next recommended model.
Keep `.remember/remember.md` under 20 lines and the Codex memory extension small.

- [ ] **Step 7: Final implementation commit if gate-driven fixes exist**

Only if Step 5 produced tracked corrections:

```bash
git add KrabEar/backend/socket_ownership.py \
  KrabEar/backend/ipc_server.py \
  KrabEar/backend/startup_diagnostics.py \
  KrabEar/backend/service.py \
  KrabEar/tests/test_socket_ownership.py \
  KrabEar/tests/test_ipc_server_wave1767.py \
  KrabEar/tests/test_startup_diagnostics.py \
  KrabEar/tests/test_socket_ownership_wiring.py \
  KrabEar/tests/test_ipc_server_dedup_wave1768.py
git commit -m "fix(ipc): close socket ownership review findings"
```

Then rerun Steps 1–4 and update the handoff to the new exact HEAD.

---

## Ревью-заметки приёмки (Claude, 2026-08-22) — учесть при исполнении

Спека и план одобрены (все три предпосылки сверены с живым кодом:
`startup_diagnostics.py:337` действительно проверяет `backend.sock`,
`ipc_server.py:158/216` действительно делает безусловный unlink). Три
дополнения к исполнению:

1. **Purge-гард**: `<socket>.lock` — намеренный survivor purge. Если после
   Task 1 `make audit-all` (audit_purge_coverage) пометит sidecar как
   непокрытый store — добавить строку в
   `scripts/purge_coverage_allowlist.txt` с `# reason: flock sidecar,
   не содержит данных, удаление под живым flock = inode-swap дыра`.
   Если гард молчит (путь не data-dir-rooted в модуле) — ничего не делать.
2. **Родительский каталог sidecar**: при кастомном `--socket-path` каталог
   может не существовать (штатный `default_socket_path` делает mkdir, кастомная
   ветка в `service.py:6103` — нет). `acquire()` обязан либо `mkdir(parents=True)`
   родителя ДО open, либо давать `UnsafeSocketPathError` с внятным текстом —
   выбрать mkdir (симметрично default-пути), покрыть тестом.
3. **AF_UNIX лимит пути (~104 байта на macOS)**: `canonical_socket_path`
   может УДЛИНИТЬ путь (resolve родителя). Bind обязан использовать тот же
   строковый путь, что и сейчас (не канонизированный), канонизация — только
   для lock-domain и probe-identity; в тестах держать temp-пути короткими.
