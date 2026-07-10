"""
Licensed-seat control for Synopsys TetraMAX subprocess invocations.

TetraMAX holds a FlexLM (or similar) license for the lifetime of each ``tmax``
process.  GRPO reward loops and evaluation can invoke fault simulation thousands
of times; without caps, many ranks or completions can spawn concurrent ``tmax``
processes and exhaust seats for long periods.

Environment variables
---------------------
TMAX_MAX_CONCURRENT
    Maximum simultaneous ``tmax`` processes **on this host** (default ``1``).
    Implemented with non-blocking ``fcntl`` locks on ``$TMAX_LOCK_DIR/seat_*.lock``.

TMAX_LOCK_DIR
    Directory for seat lock files (default ``/tmp/tmax_seats_<uid>``).

TMAX_ACQUIRE_TIMEOUT_S
    Seconds to wait for a free seat before failing (default ``1800``).  Set ``0``
    to fail immediately if all seats are busy.

TMAX_TIMEOUT_S
    Per-invocation wall-clock limit for one ``tmax`` run (default ``600``).
    The subprocess is killed on expiry so licenses are released.

TMAX_RESULT_CACHE_SIZE
    In-process LRU entries for ``(netlist, input_vector, fault) → detected list``.
    Default ``0`` (disabled).  Safe per Python process (each DDP rank has its own
    cache); does not share across nodes.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional


def _env_int(name: str, default: int) -> int:
  raw = os.environ.get(name, "").strip()
  if not raw:
    return default
  return int(raw)


def max_concurrent_seats() -> int:
  return max(1, _env_int("TMAX_MAX_CONCURRENT", 1))


def per_run_timeout_s() -> int:
  return max(30, _env_int("TMAX_TIMEOUT_S", 600))


def acquire_timeout_s() -> int:
  return max(0, _env_int("TMAX_ACQUIRE_TIMEOUT_S", 1800))


def result_cache_size() -> int:
  return max(0, _env_int("TMAX_RESULT_CACHE_SIZE", 0))


def lock_dir() -> Path:
  custom = os.environ.get("TMAX_LOCK_DIR", "").strip()
  if custom:
    p = Path(custom)
  else:
    p = Path(f"/tmp/tmax_seats_{os.getuid()}")
  p.mkdir(parents=True, exist_ok=True)
  for i in range(max_concurrent_seats()):
    (p / f"seat_{i}.lock").touch()
  return p


@contextmanager
def acquire_tmax_seat(wait_timeout_s: Optional[int] = None) -> Iterator[None]:
  """
  Hold one TetraMAX license seat for the duration of the context.

  Blocks until a seat is free or ``wait_timeout_s`` is exceeded.
  """
  if wait_timeout_s is None:
    wait_timeout_s = acquire_timeout_s()

  seats = max_concurrent_seats()
  slot_dir = lock_dir()
  deadline = time.monotonic() + wait_timeout_s if wait_timeout_s > 0 else time.monotonic()

  held_fd = None
  try:
    while True:
      for i in range(seats):
        path = slot_dir / f"seat_{i}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
          fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
          held_fd = fd
          yield
          return
        except BlockingIOError:
          os.close(fd)
      if wait_timeout_s == 0 or time.monotonic() >= deadline:
        raise TimeoutError(
          f"No TetraMAX seat available within {wait_timeout_s}s "
          f"(TMAX_MAX_CONCURRENT={seats}). Another job may be holding licenses."
        )
      time.sleep(0.25)
  finally:
    if held_fd is not None:
      fcntl.flock(held_fd, fcntl.LOCK_UN)
      os.close(held_fd)


def run_tmax_subprocess(
  cmd: List[str],
  *,
  env: dict,
  cwd: str,
  timeout_s: Optional[int] = None,
  acquire_wait_s: Optional[int] = None,
) -> subprocess.CompletedProcess:
  """
  Run ``tmax`` under a seat lock with a hard timeout (kill on expiry).
  """
  if timeout_s is None:
    timeout_s = per_run_timeout_s()

  with acquire_tmax_seat(wait_timeout_s=acquire_wait_s):
    proc = subprocess.Popen(
      cmd,
      env=env,
      cwd=cwd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
    )
    try:
      stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
      proc.kill()
      proc.wait(timeout=30)
      raise RuntimeError(
        f"TetraMAX exceeded wall time ({timeout_s}s); process killed to release license."
      ) from exc

    if proc.returncode != 0:
      raise subprocess.CalledProcessError(
        proc.returncode, cmd, output=stdout, stderr=stderr
      )
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def detection_cache_key(netlist: str, input_vector: dict, fault: str) -> str:
  import hashlib
  import json as _json

  iv_json = _json.dumps(input_vector, sort_keys=True)
  h = hashlib.sha256()
  h.update(netlist.encode("utf-8", errors="replace"))
  h.update(b"\0")
  h.update(iv_json.encode("utf-8"))
  h.update(b"\0")
  h.update(fault.encode("utf-8"))
  return h.hexdigest()


class TetraMaxDetectionCache:
  """Simple in-process LRU for TetraMAX detected-fault lists."""

  def __init__(self, maxsize: int):
    self._maxsize = max(0, int(maxsize))
    self._data: Dict[str, List[str]] = {}
    self._order: List[str] = []

  def get(self, key: str) -> Optional[List[str]]:
    if key not in self._data:
      return None
    return list(self._data[key])

  def set(self, key: str, detected: List[str]) -> None:
    if self._maxsize <= 0:
      return
    if key in self._data:
      self._order.remove(key)
    elif len(self._order) >= self._maxsize:
      old = self._order.pop(0)
      self._data.pop(old, None)
    self._data[key] = list(detected)
    self._order.append(key)


_detection_cache: Optional[TetraMaxDetectionCache] = None


def get_detection_cache() -> TetraMaxDetectionCache:
  global _detection_cache
  if _detection_cache is None:
    _detection_cache = TetraMaxDetectionCache(result_cache_size())
  return _detection_cache
