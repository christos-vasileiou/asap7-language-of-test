"""One shared project pool, process-tree cleanup, and cancellation for TetraMAX.

The default pool lives on the project filesystem, not per-node /tmp. One host
owns execution; remote ranks use TMAX_SERVER_URL. All local entry points share
its 16-slot allocation. A conflicting host/limit fails closed. Slot FDs are
inherited by a watchdog so killing a client does not abandon licensed children.
"""
from __future__ import annotations
from collections import OrderedDict
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid


class SimulationError(RuntimeError):
    """Infrastructure failure, never an ordinary negative model reward."""


class SimulationCancelled(SimulationError):
    pass


def _number(name, default, minimum=0):
    value = int(os.environ.get(name, default))
    if value < minimum:
        raise ValueError(f'{name} must be >= {minimum}')
    return value


def max_concurrent_seats():
    return _number('TMAX_MAX_CONCURRENT', 16)


def per_run_timeout_s():
    return _number('TMAX_TIMEOUT_S', 120, 1)


def acquire_timeout_s():
    return _number('TMAX_ACQUIRE_TIMEOUT_S', 120)


def result_cache_size():
    return _number('TMAX_RESULT_CACHE_SIZE', 1024)


def lock_dir():
    path = Path(os.environ.get('TMAX_LOCK_DIR', str(Path(__file__).resolve().parents[1] / '.runtime' / 'tetramax')))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_state(path, data):
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def _pool(path, seats):
    config = path / 'pool.json'
    with (path / 'pool.lock').open('a+') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        expected = {'host': socket.gethostname(), 'seats': seats}
        if config.exists():
            actual = json.loads(config.read_text())
            if actual != expected:
                raise SimulationError(f'TetraMAX pool belongs to {actual}; requested {expected}. '
                                      'Remote clients must use TMAX_SERVER_URL; drain before reconfiguration.')
        else:
            tmp = config.with_suffix('.tmp')
            tmp.write_text(json.dumps(expected))
            tmp.replace(config)


@contextmanager
def acquire_tmax_seat(wait_timeout_s=None, cancel=None):
    seats = max_concurrent_seats()
    if seats == 0:
        raise SimulationError('TetraMAX pool is disabled (TMAX_MAX_CONCURRENT=0)')
    path = lock_dir()
    _pool(path, seats)
    deadline = time.monotonic() + (acquire_timeout_s() if wait_timeout_s is None else wait_timeout_s)
    fd = None
    while fd is None:
        if cancel is not None and cancel.is_set():
            raise SimulationCancelled('TetraMAX request cancelled while queued')
        if (path / 'DRAIN').exists():
            raise SimulationError('TetraMAX pool is draining')
        for slot in range(seats):
            if (path / f'seat_{slot}.quarantined').exists():
                continue
            candidate = os.open(path / f'seat_{slot}.lock', os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(candidate)
                continue
            status = path / f'seat_{slot}.json'
            if status.exists() and json.loads(status.read_text()).get('state') != 'IDLE':
                (path / f'seat_{slot}.quarantined').write_text('Previous owner did not confirm process cleanup\n')
                os.close(candidate)
                continue
            fd = candidate
            break
        if fd is None:
            if time.monotonic() >= deadline:
                raise SimulationError('Timed out waiting for a TetraMAX worker')
            time.sleep(0.1)
    try:
        yield fd, path / f'seat_{slot}'
    finally:
        # Do not explicitly unlock: a surviving supervisor still owns the same
        # open file description. Its inherited descriptor keeps the slot held.
        os.close(fd)


def run_tmax_subprocess(cmd, *, env, cwd, timeout_s=None, acquire_wait_s=None, cancel=None):
    timeout_s = per_run_timeout_s() if timeout_s is None else timeout_s
    started = time.monotonic()
    with acquire_tmax_seat(acquire_wait_s, cancel) as (fd, state):
        status = state.with_suffix('.json')
        write_state(status, {'owner': os.getpid(), 'host': socket.gethostname(),
                             'state': 'RUNNING', 'started': time.time()})
        supervisor = [sys.executable, str(Path(__file__).with_name('tetramax_process.py')),
                      json.dumps(cmd), str(timeout_s), str(status), str(os.getpid())]
        try:
            proc = subprocess.Popen(supervisor, env=env, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True, pass_fds=(fd,))
        except OSError:
            write_state(status, {'state': 'IDLE', 'spawn_failed': True})
            raise
        interrupted = None
        stdout = stderr = ''
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise SimulationCancelled('TetraMAX request cancelled')
                try:
                    stdout, stderr = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException as exc:
            interrupted = exc
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                state.with_suffix('.quarantined').write_text(f'Unconfirmed cleanup for supervisor {proc.pid}\n')
                # Leave the supervisor alive with its inherited slot lock. A new
                # process must not steal this reservation on uncertain cleanup.
                raise SimulationError(f'TetraMAX cleanup unconfirmed; slot quarantined: {state}') from exc
        finally:
            if proc.poll() is not None:
                # Only the watchdog can certify cleanup. SIGKILL/crashes must
                # never turn an uncertain RUNNING reservation into an idle one.
                final = json.loads(status.read_text())
                if final.get('state') != 'IDLE':
                    state.with_suffix('.quarantined').write_text('Supervisor did not certify cleanup\n')
                    write_state(status, dict(final, state='QUARANTINED'))
        if state.with_suffix('.quarantined').exists():
            raise SimulationError(f'TetraMAX cleanup unconfirmed; slot quarantined: {state}')
        if interrupted is not None:
            raise interrupted
        if proc.returncode == 124:
            raise SimulationError(f'TetraMAX execution exceeded {timeout_s}s; supervised process group terminated')
        if proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def detection_cache_key(netlist, input_vector, fault):
    return hashlib.sha256(json.dumps([netlist, input_vector, fault], sort_keys=True).encode()).hexdigest()


class TetraMaxDetectionCache:
    def __init__(self, maxsize):
        self.maxsize = maxsize
        self.data = OrderedDict()
        self.lock = threading.Lock()
    def get(self, key):
        with self.lock:
            if key not in self.data:
                return None
            self.data.move_to_end(key)
            return list(self.data[key])
    def set(self, key, value):
        if not self.maxsize:
            return
        with self.lock:
            self.data[key] = list(value)
            self.data.move_to_end(key)
            while len(self.data) > self.maxsize:
                self.data.popitem(last=False)


_detection_cache = None

def get_detection_cache():
    global _detection_cache
    if _detection_cache is None:
        _detection_cache = TetraMaxDetectionCache(result_cache_size())
    return _detection_cache
