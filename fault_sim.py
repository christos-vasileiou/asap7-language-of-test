"""
fault_sim.py
------------

Fault simulation engine for structural Verilog netlists.

Provides the ``OptimizedNetlist`` class for parsing gate-level netlists,
the ``fast_fault_sim`` function for Python good/bad machine simulation, and
:class:`TetraMaxFaultSimulator` / :func:`tetramax_fault_sim` for Synopsys
TetraMAX batch artifacts and optional ``tmax`` invocation (set
``FAULT_SIM_BACKEND=tetramax`` or ``hybrid``).

Optimizations over the naive single-pass approach:
  1. Gate functions are pre-compiled to lookup tables (LUTs) during
     ``OptimizedNetlist`` construction — no per-gate sympy/lambdify overhead.
  2. Instructions are topologically sorted, guaranteeing that all
     dependencies are resolved in a single forward pass.
  3. A multi-pass fallback handles any remaining unevaluated nets
     (e.g. due to cycles or missing dependencies).
  4. ``fault_propagation_path`` uses a ``set`` for O(1) membership checks.
  5. ``net_dependencies`` are pre-computed once per netlist (not per sim call).
  6. ``gate_func`` auto-detection: raw JSON dicts, file paths, or pre-parsed
     SymPy dicts are all handled transparently.
"""

import hashlib
import json
import os
import re as std_re
import shutil
import subprocess
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import regex as re

from sympy import symbols, lambdify, parse_expr
from sympy.core.symbol import Symbol

from netlist_utils import expand_nets, get_net_length


# ============================================================
# Small helpers
# ============================================================

def float_cols_to_int_with_x(df: pd.DataFrame) -> pd.DataFrame:
  """Replace NaN with 'x' and convert float columns to int."""
  df = df.copy()
  float_cols = df.select_dtypes(include=["float", "Float64"]).columns
  for c in float_cols:
    df[c] = df[c].map(lambda v: 'x' if pd.isna(v) else int(v))
  return df


def is_every_net_evaluated(machine: dict) -> bool:
  return all(isinstance(v, int) for v in machine.values())


def convert_string_to_dict(x, sep=':'):
  return {net.strip(): int(value.strip()) for net_value in x.split(',') for net, value in [net_value.split(sep)]}


# ============================================================
# Gate function compilation
# ============================================================

# Regex for valid identifiers (excludes pure-digit tokens)
_IDENT_RE = re.compile(r'[A-Za-z_]\w*')


def _normalize_gate_func(gate_func):
  """
  Normalise *gate_func* so that the returned dict is always
  ``{gate_type: {port: <expr_or_dict>}}``.

  Accepted inputs
  ---------------
  * **str**  – file path → load JSON, extract ``"gate_funcs"`` key.
  * **dict** with ``"gate_funcs"`` key  – extract the inner dict.
  * **dict** without that key            – assumed already normalised.
  """
  if isinstance(gate_func, str):
    with open(gate_func, 'r') as f:
      data = json.load(f)
    return data.get('gate_funcs', data)
  elif isinstance(gate_func, dict) and 'gate_funcs' in gate_func:
    return gate_func['gate_funcs']
  return gate_func


def _compile_expr_to_fn(expr_str: str):
  """
  Compile a boolean expression string to a callable function.

  Returns ``(fn, var_names)`` where *fn* is a callable taking integer
  arguments in the order given by sorted *var_names*.
  """
  var_names = sorted(set(_IDENT_RE.findall(expr_str)))

  if not var_names:
    # Constant expression (e.g. "1", "0")
    try:
      val = int(expr_str.strip())
      return (lambda v=val: v), []
    except ValueError:
      return None, []

  sym_objs = symbols(' '.join(var_names))
  if isinstance(sym_objs, Symbol):
    sym_objs = [sym_objs]
  symbol_map = dict(zip(var_names, sym_objs))

  try:
    expr = parse_expr(expr_str, local_dict=symbol_map, evaluate=False)
    syms = [symbols(n) for n in var_names]
    fn = lambdify(syms, expr, modules='numpy')
    return fn, var_names
  except Exception:
    return None, []


def _make_lut(fn, num_inputs: int) -> Optional[list]:
  """
  Pre-compute a lookup table for a gate function.

  For *n* inputs the table has 2ⁿ entries.  ``lut[i]`` is the output
  when the j-th input equals bit j of *i* (j-th entry in *sym_names*).
  Returns ``None`` when a LUT is impractical (>10 inputs) or *fn* is None.
  """
  if fn is None or num_inputs > 10 or num_inputs < 0:
    return None
  if num_inputs == 0:
    try:
      return [int(bool(fn()))]
    except Exception:
      return None

  size = 1 << num_inputs
  lut = [0] * size
  for i in range(size):
    args = [(i >> j) & 1 for j in range(num_inputs)]
    try:
      lut[i] = int(bool(fn(*args)))
    except Exception:
      return None      # Fall back to callable evaluation
  return lut


def _compile_gate_port(gate_type, port, gate_func):
  """
  Compile a single gate-port from any *gate_func* format.

  Handles
  -------
  * Raw expression strings  (from ``sim_config.json``)
  * Parsed dicts with ``function`` / ``symbols`` keys
    (from ``RewardFunctionFactory._parse_gate_function``)

  Returns ``(lut_or_fn, sym_names)``.
  """
  entry = gate_func[gate_type][port]

  if isinstance(entry, str):
    fn, sym_names = _compile_expr_to_fn(entry)
  elif isinstance(entry, dict) and 'function' in entry:
    expr = entry['function']
    sym_names = sorted(entry['symbols'].keys())
    # Filter out digit-only tokens (constants mis-identified as vars)
    sym_names = [s for s in sym_names if not s.isdigit()]
    syms = [symbols(n) for n in sym_names]
    try:
      fn = lambdify(syms, expr, modules='numpy')
    except Exception:
      fn = None
  else:
    return None, []

  # Build a LUT for fast evaluation
  lut = _make_lut(fn, len(sym_names))
  return (lut if lut is not None else fn), sym_names


# ============================================================
# Legacy API  (kept for backward compatibility)
# ============================================================

# Global cache for compiled gate functions — used by
# final_dataset_creation.py.  New code should rely on
# OptimizedNetlist's pre-compiled instructions instead.
_compiled_gate_cache = {}


def get_compiled_func(gate_type, port, gate_func):
  """
  Legacy helper — compiles a gate function via sympy/lambdify and
  caches the result in ``_compiled_gate_cache``.

  New code should use ``OptimizedNetlist`` which pre-compiles
  everything during construction.
  """
  key = (gate_type, port)
  if key in _compiled_gate_cache:
    return _compiled_gate_cache[key]

  gate_func = _normalize_gate_func(gate_func)
  entry = gate_func[gate_type][port]

  if isinstance(entry, str):
    fn, sym_names = _compile_expr_to_fn(entry)
    if fn is not None:
      _compiled_gate_cache[key] = (fn, sym_names)
    return fn, sym_names

  # Parsed dict format
  expr = entry['function']
  sym_names = sorted(entry['symbols'].keys())
  sym_names = [s for s in sym_names if not s.isdigit()]
  syms = [symbols(n) for n in sym_names]

  try:
    fn = lambdify(syms, expr, modules='numpy')
  except Exception as e:
    print(f"Error compiling lambda for {gate_type} {port}: {e}")
    fn = None

  _compiled_gate_cache[key] = (fn, sym_names)
  return fn, sym_names


# ============================================================
# OptimizedNetlist
# ============================================================

class OptimizedNetlist:
  """
  Pre-parsed **and** pre-compiled gate-level netlist for fast fault
  simulation.

  During construction the class:

  1. Normalises *gate_func* (handles file paths, nested JSON, raw
     expression strings, or pre-parsed SymPy dicts).
  2. Parses the Verilog text to extract gate instances and ``assign``
     statements.
  3. Compiles every gate function to a lookup table (LUT) or callable.
  4. Builds a dependency graph (``net_dependencies``).
  5. Topologically sorts instructions so a single forward pass
     resolves all nets.
  """

  def __init__(self, verilog_text, gate_func, decl_re, name_re):
    # Normalise gate_func regardless of how it was supplied
    gate_func = _normalize_gate_func(gate_func)

    # Cache net lists
    self.input_nets = expand_nets(get_net_length(verilog_text, "input", decl_re, name_re))
    self.output_nets = expand_nets(get_net_length(verilog_text, "output", decl_re, name_re))

    self.netlist = verilog_text
    self.instructions = []
    self.net_dependencies = {}        # net → set of nets it depends on

    self._parse_and_compile(verilog_text, gate_func)
    self._topological_sort()

  # ----------------------------------------------------------
  # Parsing + compilation
  # ----------------------------------------------------------
  def _parse_and_compile(self, verilog_text, gate_func):
    GATE_INGREDIENT  = r"((?:\\[^\s]+|\w+))\s+((?:\\[^\s]+|\w+))\s*\(\s*([\s\S]+?)\s*\)\s*;"
    GATE_CONNECTIONS = r"\.((?:\\[^\s]+|\w+))\s*\(\s*([^)]+?)\s*\)"
    ASSIGN_STATEMENT = r"assign\s+(?P<net>(?:\\[^\s]+|[\w\[\]\*]+))\s*=\s*(?P<val>(?:\\[^\s]+|[\w\[\]\*]+))\s*;"
    LOGIC_VALUE      = r"\*Logic(?P<val>[01]+)\*\s*"

    gate_ingredients_re = re.compile(GATE_INGREDIENT)
    gate_connections_re = re.compile(GATE_CONNECTIONS)
    assign_re           = re.compile(ASSIGN_STATEMENT)
    logic_value_re      = re.compile(LOGIC_VALUE)

    # --- assigns --------------------------------------------------
    for match in assign_re.finditer(verilog_text):
      assign_net, assign_value = match.groups()
      m = logic_value_re.search(assign_value)
      if m:
        assign_value = m.group('val')
      if assign_value.isdigit():
        val = int(assign_value)
        self.instructions.append(('assign_const', assign_net, val))
        self.net_dependencies[assign_net] = set()
      else:
        self.instructions.append(('assign_net', assign_net, assign_value))
        if not (isinstance(assign_value, str) and assign_value.isdigit()):
          self.net_dependencies[assign_net] = {assign_value}
        else:
          self.net_dependencies[assign_net] = set()

    # --- gates ----------------------------------------------------
    for match in gate_ingredients_re.finditer(verilog_text):
      gate_type, instance, connections_str = match.groups()
      if gate_type.startswith('module'):
        continue
      if gate_type not in gate_func:
        continue

      connections = dict(gate_connections_re.findall(connections_str))

      # Clean logic values in connections
      for p, n in connections.items():
        m = logic_value_re.search(n)
        if m:
          connections[p] = m.group('val')

      gate_output_ports = set(gate_func[gate_type].keys())
      instance_ports    = set(connections.keys())

      out_ports = instance_ports & gate_output_ports
      in_ports  = instance_ports - gate_output_ports

      for out_p in out_ports:
        out_net   = connections[out_p]
        input_map = {p: connections[p] for p in in_ports}

        # Compile the gate function (LUT or callable)
        lut_or_fn, sym_names = _compile_gate_port(gate_type, out_p, gate_func)
        if lut_or_fn is None:
          continue

        # Constant-output gates (e.g. TIEHI / TIELO)
        if not sym_names:
          try:
            val = lut_or_fn[0] if isinstance(lut_or_fn, list) else int(bool(lut_or_fn()))
            self.instructions.append(('assign_const', out_net, val))
            self.net_dependencies[out_net] = set()
          except Exception:
            pass
          continue

        # Build dependency set (actual net names this gate reads)
        dep_set = set()
        for sym in sym_names:
          p_net = input_map.get(sym)
          if p_net is not None and not (isinstance(p_net, str) and p_net.isdigit()):
            dep_set.add(p_net)

        self.net_dependencies[out_net] = dep_set

        # New pre-compiled instruction format:
        # ('gate', out_net, lut_or_fn, sym_names, input_map, dep_set)
        self.instructions.append(
          ('gate', out_net, lut_or_fn, sym_names, input_map, dep_set)
        )

  # ----------------------------------------------------------
  # Topological sort (Kahn's algorithm)
  # ----------------------------------------------------------
  def _topological_sort(self):
    """Sort instructions so that every net is computed before it is read."""
    n = len(self.instructions)
    if n <= 1:
      return

    # Map: output net → instruction index
    net_producer: Dict[str, int] = {}
    for i, instr in enumerate(self.instructions):
      # Index 1 is always the output net for all instruction types
      net_producer[instr[1]] = i

    # Build adjacency list and in-degree array
    adj       = [[] for _ in range(n)]
    in_degree = [0] * n

    for i, instr in enumerate(self.instructions):
      typ = instr[0]
      dep_indices = set()

      if typ == 'assign_net':
        src = instr[2]
        if src in net_producer:
          dep_indices.add(net_producer[src])
      elif typ == 'gate':
        dep_nets = instr[5]             # dep_set
        for net in dep_nets:
          if net in net_producer:
            dep_indices.add(net_producer[net])

      for dep_idx in dep_indices:
        if dep_idx != i:                # avoid self-loops
          adj[dep_idx].append(i)
          in_degree[i] += 1

    # Kahn's BFS
    queue = deque(i for i in range(n) if in_degree[i] == 0)
    sorted_indices: List[int] = []

    while queue:
      node = queue.popleft()
      sorted_indices.append(node)
      for neighbour in adj[node]:
        in_degree[neighbour] -= 1
        if in_degree[neighbour] == 0:
          queue.append(neighbour)

    if len(sorted_indices) != n:
      # Cycle detected (e.g. latch feedback) — keep original order
      return

    self.instructions = [self.instructions[i] for i in sorted_indices]


# ============================================================
# fast_fault_sim
# ============================================================

def fast_fault_sim(
  input_nets_and_vector: Union[str, Dict[str, int]],
  expected_output_nets_and_vector: Union[str, Dict[str, int]],
  fault: str,
  optimized_netlist: OptimizedNetlist,
  gate_func = None,
  module_name: Optional[str] = None,
  return_rewards: bool = False,
) -> Union[pd.DataFrame, Tuple[pd.DataFrame, Dict[str, bool]]]:
  """
  Performs fault simulation comparing good machine vs bad machine
  (with stuck-at fault).

  Args
  ----
  input_nets_and_vector
      Input net values — dict or ``"net: val, …"`` string.
  expected_output_nets_and_vector
      Expected output net values.
  fault
      Fault descriptor, e.g. ``"sa1 n8"``.
  optimized_netlist
      A pre-compiled :class:`OptimizedNetlist`.
  gate_func
      **Deprecated / optional.**  Gate functions are now pre-compiled
      inside the netlist.  Kept for backward API compatibility.
  return_rewards
      If True return ``(DataFrame, reward_dict)`` instead of just
      the DataFrame.
  """
  # ---- Parse fault --------------------------------------------------
  FAULT_RE = re.compile(r"sa(\d)\s*(.*)")
  try:
    match = FAULT_RE.findall(fault)
    if match:
      faulty_value_str, faulty_net = match[0]
      faulty_value = int(faulty_value_str)
    else:
      return pd.DataFrame([{"error": f"error parsing fault: {fault}.\n"}])
  except Exception:
    return pd.DataFrame([{"error": f"error parsing fault: {fault}.\n"}])

  # ---- Parse input / output vectors ---------------------------------
  if isinstance(input_nets_and_vector, str):
    sep = ':' if ':' in input_nets_and_vector else '='
    input_nets_and_vector = convert_string_to_dict(input_nets_and_vector, sep=sep)
  if isinstance(expected_output_nets_and_vector, str):
    sep = ':' if ':' in expected_output_nets_and_vector else '='
    expected_output_nets_and_vector = convert_string_to_dict(expected_output_nets_and_vector, sep=sep)

  _inputs  = list(input_nets_and_vector.keys())
  _outputs = list(expected_output_nets_and_vector.keys())

  # ---- Rewards (optional) -------------------------------------------
  if return_rewards:
    rewards = {
      'input_nets_match':  _inputs  == optimized_netlist.input_nets,
      'output_nets_match': _outputs == optimized_netlist.output_nets,
    }

  # ---- Initialise machines ------------------------------------------
  good_machine = input_nets_and_vector.copy()
  good_machine.update(expected_output_nets_and_vector)

  bad_machine = input_nets_and_vector.copy()
  bad_machine[faulty_net] = faulty_value

  # set for O(1) membership; list to preserve insertion order
  fault_prop_set  = {faulty_net}
  fault_prop_list = [faulty_net]

  # ---- Inline helpers -----------------------------------------------
  def resolve(machine, val):
    """Chase alias chains up to 20 hops."""
    if isinstance(val, int):
      return val
    if isinstance(val, str) and val.isdigit():
      return int(val)
    for _ in range(20):
      if val not in machine:
        return val
      nex = machine[val]
      if nex is val or nex == val:
        return nex
      val = nex
      if isinstance(val, int):
        return val
      if isinstance(val, str) and val.isdigit():
        return int(val)
    return val

  def eval_inputs(machine, sym_names, input_map):
    """Resolve all gate inputs; return list[int] or None."""
    args = []
    for sym in sym_names:
      p_net = input_map.get(sym)
      if p_net is None:
        return None
      val = resolve(machine, p_net)
      if isinstance(val, int):
        args.append(val)
      elif isinstance(val, str) and val.isdigit():
        args.append(int(val))
      else:
        return None
    return args

  def eval_lut(lut, args):
    """Evaluate a pre-computed LUT. Bit-j of the index ↔ args[j]."""
    idx = 0
    for i, a in enumerate(args):
      idx |= (a << i)
    return lut[idx]

  # ---- Single-pass evaluation (topo-sorted) -------------------------
  unevaluated = []

  for instr in optimized_netlist.instructions:
    typ = instr[0]

    if typ == 'assign_const':
      _, net, val = instr
      if net not in good_machine:
        good_machine[net] = val
      # Bad machine — skip if fault-stuck
      if net not in fault_prop_set:
        if net not in bad_machine:
          bad_machine[net] = val

    elif typ == 'assign_net':
      _, net, src = instr
      # Good machine
      good_machine[net] = resolve(good_machine, src)
      # Bad machine — skip if fault-stuck
      if net in fault_prop_set:
        continue
      bad_machine[net] = resolve(bad_machine, src)
      # Fault propagation through alias
      if src in fault_prop_set:
        fault_prop_set.add(net)
        fault_prop_list.append(net)

    elif typ == 'gate':
      _, out_net, lut_or_fn, sym_names, input_map, dep_set = instr

      # --- Good Machine ---
      args_gm = eval_inputs(good_machine, sym_names, input_map)
      if args_gm is None:
        unevaluated.append(instr)
        continue

      if isinstance(lut_or_fn, list):
        out_val_gm = eval_lut(lut_or_fn, args_gm)
      else:
        out_val_gm = int(bool(lut_or_fn(*args_gm)))
      good_machine[out_net] = out_val_gm

      # --- Bad Machine ---
      if out_net in fault_prop_set:
        continue                    # stuck at faulty_value

      args_bm = eval_inputs(bad_machine, sym_names, input_map)
      if args_bm is not None:
        if isinstance(lut_or_fn, list):
          out_val_bm = eval_lut(lut_or_fn, args_bm)
        else:
          out_val_bm = int(bool(lut_or_fn(*args_bm)))
        bad_machine[out_net] = out_val_bm

        # Fault propagation check
        if out_val_gm != out_val_bm:
          fault_prop_set.add(out_net)
          fault_prop_list.append(out_net)

  # ---- Multi-pass fallback ------------------------------------------
  for _pass in range(10):
    if not unevaluated:
      break
    still_unevaluated = []
    for instr in unevaluated:
      _, out_net, lut_or_fn, sym_names, input_map, dep_set = instr

      args_gm = eval_inputs(good_machine, sym_names, input_map)
      if args_gm is None:
        still_unevaluated.append(instr)
        continue

      if isinstance(lut_or_fn, list):
        out_val_gm = eval_lut(lut_or_fn, args_gm)
      else:
        out_val_gm = int(bool(lut_or_fn(*args_gm)))
      good_machine[out_net] = out_val_gm

      if out_net not in fault_prop_set:
        args_bm = eval_inputs(bad_machine, sym_names, input_map)
        if args_bm is not None:
          if isinstance(lut_or_fn, list):
            out_val_bm = eval_lut(lut_or_fn, args_bm)
          else:
            out_val_bm = int(bool(lut_or_fn(*args_bm)))
          bad_machine[out_net] = out_val_bm
          if out_val_gm != out_val_bm:
            fault_prop_set.add(out_net)
            fault_prop_list.append(out_net)

    if len(still_unevaluated) == len(unevaluated):
      break                         # no progress — stop
    unevaluated = still_unevaluated

  # ---- Final cleanup: resolve remaining aliases ---------------------
  for machine in (good_machine, bad_machine):
    for _ in range(5):
      changed = False
      for net in list(machine.keys()):
        val = machine[net]
        if not isinstance(val, int):
          res = resolve(machine, val)
          if res != val:
            machine[net] = res
            changed = True
      if not changed:
        break

  # Remove stray numeric keys
  for machine in (good_machine, bad_machine):
    for k in [k for k in machine if isinstance(k, int) or (isinstance(k, str) and k.isdigit())]:
      machine.pop(k)

  # ---- Backtrace: sensitising inputs --------------------------------
  net_deps = optimized_netlist.net_dependencies

  def compute_backtrack_path(propagation_set, net_deps, primary_inputs):
    backtrack_inputs = set()
    visited = set()
    to_visit = set(propagation_set)
    while to_visit:
      net = to_visit.pop()
      if net in visited:
        continue
      visited.add(net)
      if net in primary_inputs:
        backtrack_inputs.add(net)
      elif net in net_deps:
        to_visit.update(net_deps[net])
        backtrack_inputs.update(net_deps[net])
    return list(backtrack_inputs)

  _inputs_set = set(_inputs)
  backtrack_fault_path = compute_backtrack_path(
    fault_prop_set, net_deps, _inputs_set
  )

  # ---- Build result DataFrame ---------------------------------------
  simulation = pd.concat(
    [pd.Series(good_machine), pd.Series(bad_machine)], axis=1
  )
  simulation[2] = simulation.index.isin(_inputs)
  simulation[3] = simulation.index.isin(_outputs)
  simulation[4] = simulation.index.isin(fault_prop_set)
  simulation[5] = simulation.index.isin(backtrack_fault_path)

  simulation = float_cols_to_int_with_x(simulation)
  simulation.columns = [
    "Good Machine", "Bad Machine", "PIs", "POs",
    "Fault Propagation Path", "Backtrack Sensitizing Inputs",
  ]

  priority = {
    (True,  False): 0,
    (False, False): 1,
    (False, True):  2,
  }
  simulation["Priority"] = (
    simulation[["PIs", "POs"]].apply(tuple, axis=1).map(priority)
  )
  simulation = simulation.sort_values("Priority").drop(columns=["Priority"])

  if return_rewards:
    return simulation, rewards
  return simulation


# ============================================================
# TetraMAX fault simulation (ASAP7 / language-of-test)
# ============================================================
#
# Dataset construction (final_dataset_creation.py):
#   1. TetraMAX ``run_fault_sim`` per ATPG pattern → ``detected_faults`` CSV
#   2. Python :func:`fast_fault_sim` per detected fault → ``snapshot`` field
#
# GRPO / tool rewards on *model-generated* vectors should re-run TetraMAX for
# authoritative detection; snapshots stay on :func:`fast_fault_sim` for speed and
# schema compatibility with training records.

TETRAMAX_FAULT_STATUS_DETECTED = "DS"
TETRAMAX_FAULT_RE = re.compile(r"sa(\d)\s+(.+)", re.IGNORECASE)
TETRAMAX_FAULT_SIM_STATS_RE = std_re.compile(
  r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)%",
  std_re.MULTILINE,
)
_DATA_PREPROCESSING_DIR = Path(__file__).resolve().parent
_ASAP7_GATE_FUNC = _DATA_PREPROCESSING_DIR / "sim_config.json"
_TMAX_VECTOR_TCL = _DATA_PREPROCESSING_DIR / "scripts" / "tmax_vector_fault_sim.tcl"


def fault_sim_backend() -> str:
  """
  ``fast`` — Python only (default).
  ``tetramax`` — run Synopsys TetraMAX for detection; Python for snapshot table.
  ``hybrid`` — same as tetramax when ``tmax`` is on PATH, else fast.
  """
  return os.environ.get("FAULT_SIM_BACKEND", "fast").strip().lower()


def _use_tetramax_backend() -> bool:
  mode = fault_sim_backend()
  if mode == "fast":
    return False
  if mode in ("tetramax", "hybrid"):
    from tmax import infer_tmax_binary
    return shutil.which(infer_tmax_binary()) is not None
  return False


def parse_tetramax_fault(fault: str) -> Tuple[int, str]:
  match = TETRAMAX_FAULT_RE.match(fault.strip())
  if not match:
    raise ValueError(f"Invalid TetraMAX fault descriptor: {fault!r}")
  return int(match.group(1)), match.group(2).strip()


def parse_detected_faults_list(raw: Union[str, List[str], None]) -> List[str]:
  if raw is None:
    return []
  if isinstance(raw, list):
    return [str(f).strip() for f in raw if str(f).strip()]
  text = str(raw).strip()
  if not text:
    return []
  parts = re.split(r",\s*(?=sa)", text, flags=re.IGNORECASE)
  return [p.strip() for p in parts if p.strip()]


def parse_vector_field(raw: Union[str, Dict[str, Any]]) -> Dict[str, int]:
  if isinstance(raw, dict):
    return {str(k): int(v) for k, v in raw.items()}
  text = str(raw).strip()
  if text.startswith("{"):
    return {str(k): int(v) for k, v in json.loads(text).items()}
  sep = ":" if ":" in text else "="
  return convert_string_to_dict(text, sep=sep)


def _normalize_fault_key(fault: str) -> str:
  stuck, net = parse_tetramax_fault(fault)
  return f"sa{stuck} {net}"


def parse_tetramax_detected_faults(path: Union[str, Path]) -> pd.DataFrame:
  return pd.read_csv(
    path, sep=r"\s+", header=None, names=["fault_type", "status", "net"]
  )


def detected_faults_from_csv(
  path: Union[str, Path], status: str = TETRAMAX_FAULT_STATUS_DETECTED
) -> List[str]:
  df = parse_tetramax_detected_faults(path)
  mask = df["status"].astype(str).str.upper() == status.upper()
  rows = df.loc[mask, ["fault_type", "net"]]
  return [_normalize_fault_key(f"{ft} {net}") for ft, net in rows.itertuples(index=False, name=None)]


def asap7_cell_libs(use_asap7_28: bool = True) -> Tuple[str, str]:
  """Return (verilog_paths, liberty_paths) as space-separated strings for TetraMAX env."""
  from tmax import cell_liberty_paths_from_env_or_kit, cell_verilog_paths_from_env_or_kit

  v = cell_verilog_paths_from_env_or_kit(_DATA_PREPROCESSING_DIR, use_asap7_28=use_asap7_28)
  try:
    lib = cell_liberty_paths_from_env_or_kit(_DATA_PREPROCESSING_DIR, use_asap7_28=use_asap7_28)
  except FileNotFoundError:
    lib = []
  return " ".join(v), " ".join(lib)


class TetraMaxFaultSimulator:
  """Run TetraMAX vector fault simulation and parse detected-fault artifacts."""

  def __init__(
    self,
    detected_faults: Optional[Union[str, List[str]]] = None,
    *,
    design_output_dir: Optional[Union[str, Path]] = None,
    pattern_idx: int = 0,
  ):
    self.design_output_dir = (
      Path(design_output_dir) if design_output_dir is not None else None
    )
    self.pattern_idx = int(pattern_idx)
    self._detected_list: List[str] = []
    self._detected_set: set = set()
    if detected_faults is not None:
      self._set_detected(parse_detected_faults_list(detected_faults))

  def _set_detected(self, faults: List[str]) -> None:
    self._detected_list = [_normalize_fault_key(f) for f in faults]
    self._detected_set = set(self._detected_list)

  @classmethod
  def from_record(cls, record: Dict[str, Any], pattern_idx: int = 0) -> "TetraMaxFaultSimulator":
    return cls(detected_faults=record.get("detected_faults"), pattern_idx=pattern_idx)

  def _detected_faults_path(self) -> Path:
    assert self.design_output_dir is not None
    return (
      self.design_output_dir
      / "simulation"
      / "bad"
      / f"machine_detected_faults_{self.pattern_idx}.csv"
    )

  def is_fault_detected(self, fault: str) -> bool:
    return _normalize_fault_key(fault) in self._detected_set

  def run_vector_fault_sim(
    self,
    verilog_text: str,
    input_vector: Union[str, Dict[str, int]],
    *,
    module_name: str = "design",
    pi_order: Optional[List[str]] = None,
    work_dir: Optional[Union[str, Path]] = None,
    tmax_bin: Optional[str] = None,
    timeout_s: Optional[int] = None,
    use_asap7_28: bool = True,
  ) -> List[str]:
    """
    Load ``verilog_text`` in TetraMAX, apply ``input_vector``, run ``run_fault_sim``.

    Returns detected fault strings (``saX net`` with status DS).
    """
    from tetramax_stil import write_vector_stil
    from tmax import build_env, infer_tmax_binary

    if not _TMAX_VECTOR_TCL.is_file():
      raise FileNotFoundError(f"TetraMAX Tcl not found: {_TMAX_VECTOR_TCL}")

    inputs = parse_vector_field(input_vector)
    if pi_order is None:
      raise ValueError("pi_order is required (use OptimizedNetlist.input_nets)")

    cleanup = work_dir is None
    if work_dir is None:
      work_dir = Path(tempfile.mkdtemp(prefix="tmax_fault_sim_"))
    else:
      work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    design_name = module_name or "design"
    design_dir = work_dir / design_name
    design_dir.mkdir(parents=True, exist_ok=True)
    verilog_path = design_dir / f"{design_name}.v"
    verilog_path.write_text(verilog_text, encoding="utf-8")
    stil_path = design_dir / "vector.stil"
    write_vector_stil(stil_path, pi_order, inputs)

    cell_v, liberty_v = asap7_cell_libs(use_asap7_28=use_asap7_28)
    env = build_env(
      str(verilog_path.resolve()),
      work_dir,
      cell_v,
      cell_libs_liberty=liberty_v,
      stil_file=str(stil_path.resolve()),
      pattern_idx=self.pattern_idx,
    )
    bin_name = tmax_bin or infer_tmax_binary()
    cmd = [bin_name, "-shell", "-tcl", str(_TMAX_VECTOR_TCL.resolve())]

    from tetramax_seats import run_tmax_subprocess

    try:
      run_tmax_subprocess(
        cmd,
        env=env,
        cwd=str(design_dir),
        timeout_s=timeout_s,
      )
    except subprocess.CalledProcessError as exc:
      raise RuntimeError(
        f"TetraMAX failed (exit {exc.returncode}): {exc.stderr or exc.stdout}"
      ) from exc
    except TimeoutError as exc:
      raise RuntimeError(str(exc)) from exc
    finally:
      if cleanup:
        shutil.rmtree(work_dir, ignore_errors=True)

    self.design_output_dir = design_dir
    detected = detected_faults_from_csv(self._detected_faults_path())
    self._set_detected(detected)
    return detected


def tetramax_fault_sim(
  input_nets_and_vector: Union[str, Dict[str, int]],
  expected_output_nets_and_vector: Union[str, Dict[str, int]],
  fault: str,
  optimized_netlist: OptimizedNetlist,
  gate_func=None,
  *,
  tetramax_simulator: Optional[TetraMaxFaultSimulator] = None,
  detected_faults: Optional[Union[str, List[str]]] = None,
  module_name: Optional[str] = None,
  run_tetramax: Optional[bool] = None,
  require_tetramax_detection: bool = False,
  return_rewards: bool = False,
) -> Union[pd.DataFrame, Tuple[pd.DataFrame, Dict[str, Any]]]:
  """
  Fault simulation aligned with the language-of-test EDA flow.

  When TetraMAX is enabled, runs ``run_fault_sim`` on the supplied PI vector and
  checks that ``fault`` appears in the detected-fault list.  Good/bad snapshots
  are still computed with :func:`fast_fault_sim` (same schema as the dataset).
  """
  rewards_extra: Dict[str, Any] = {
    "tetramax_available": False,
    "tetramax_detected": None,
  }

  tmax_sim = tetramax_simulator
  if tmax_sim is None and detected_faults is not None:
    tmax_sim = TetraMaxFaultSimulator(detected_faults=detected_faults)

  do_tmax = run_tetramax if run_tetramax is not None else _use_tetramax_backend()
  if do_tmax:
    if tmax_sim is None:
      tmax_sim = TetraMaxFaultSimulator()
    if not tmax_sim._detected_set:
      try:
        from tetramax_seats import detection_cache_key, get_detection_cache

        inputs = (
          input_nets_and_vector
          if isinstance(input_nets_and_vector, dict)
          else parse_vector_field(input_nets_and_vector)
        )
        cache = get_detection_cache()
        ck = detection_cache_key(optimized_netlist.netlist, inputs, fault)
        cached = cache.get(ck)
        if cached is not None:
          tmax_sim._set_detected(cached)
        else:

          def _run():
            return tmax_sim.run_vector_fault_sim(
              optimized_netlist.netlist,
              inputs,
              module_name=module_name or "design",
              pi_order=list(optimized_netlist.input_nets),
            )

          detected = _run()
          cache.set(ck, detected)
      except Exception as exc:
        if fault_sim_backend() == "tetramax":
          err = pd.DataFrame([{"error": f"TetraMAX simulation failed: {exc}"}])
          return (err, rewards_extra) if return_rewards else err
        # hybrid: fall back to Python snapshot-only when TetraMAX is unavailable

  if tmax_sim is not None and tmax_sim._detected_set:
    rewards_extra["tetramax_available"] = True
    rewards_extra["tetramax_detected"] = tmax_sim.is_fault_detected(fault)
    if require_tetramax_detection and not rewards_extra["tetramax_detected"]:
      err = pd.DataFrame(
        [{"error": f"fault not detected by TetraMAX: {fault}"}]
      )
      return (err, rewards_extra) if return_rewards else err

  result = fast_fault_sim(
    input_nets_and_vector,
    expected_output_nets_and_vector,
    fault,
    optimized_netlist=optimized_netlist,
    gate_func=gate_func,
    return_rewards=return_rewards,
  )
  if not return_rewards:
    return result
  simulation, rewards = result
  rewards.update(rewards_extra)
  return simulation, rewards


def resolve_fault_sim_runner():
  """Return ``tetramax_fault_sim`` or ``fast_fault_sim`` per ``FAULT_SIM_BACKEND``."""
  return tetramax_fault_sim if _use_tetramax_backend() else fast_fault_sim
