"""Child supervisor: terminate TetraMAX if its requesting process disappears.

Invoked only by tetramax_seats, inheriting its slot lock. This avoids releasing a
slot when a trainer is SIGKILLed while a licensed descendant is still running.
"""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
import ctypes
from pathlib import Path


def main():
    # Reap orphaned grandchildren as well as the wrapper's direct child.
    ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    owner = int(sys.argv[4])
    command = json.loads(sys.argv[1])
    timeout = float(sys.argv[2])
    cancelled = False
    def stop(*_):
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    from tetramax_seats import write_state
    state = Path(sys.argv[3])
    if os.getppid() != owner:
        write_state(state, {'state': 'IDLE', 'owner_lost': True})
        return 130
    try:
        child = subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL)
    except OSError:
        write_state(state, {'state': 'IDLE', 'spawn_failed': True})
        raise
    deadline = time.monotonic() + timeout
    expired = False
    try:
        write_state(state, {'state':'RUNNING', 'pid':os.getpid(), 'child_pid':child.pid,
                            'owner':owner, 'started':time.time()})
        while child.poll() is None:
            expired = time.monotonic() >= deadline
            if cancelled or expired or os.getppid() != owner:
                break
            time.sleep(0.1)
    finally:
        # Clean the entire group, including descendants left after the leader exits.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        reaped = False
        limit = time.monotonic() + 3
        while time.monotonic() < limit:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    time.sleep(0.05)
            except ChildProcessError:
                reaped = True
                break
        if len(sys.argv) > 3:
            state = Path(sys.argv[3])
            if not reaped:
                state.with_suffix('.quarantined').write_text('Unreaped TetraMAX descendants\n')
            write_state(state, {'pid':os.getpid(), 'state':'IDLE' if reaped else 'QUARANTINED',
                                'finished':time.time(),'owner_lost':os.getppid()!=owner,
                                'returncode':child.returncode,'expired':expired,'cancelled':cancelled})
    return 124 if expired else 130 if cancelled else child.returncode


if __name__ == '__main__':
    sys.exit(main())
