#!/usr/bin/env python

import pandas as pd
import numpy as np
import os
import regex as re
from glob import glob
from vars import (
  _user_prompt_dict,
  _training_prompts_faults_list,
  _cot_assistant_response_faults_list,
  _system_prompts, 
  _answer_template,
  chat_template, 
)
from datasets import DatasetDict, Dataset
from transformers import AutoTokenizer
import random
from io import StringIO
from functools import partial
from tqdm import tqdm
import argparse
import multiprocessing as mp
import subprocess
import copy
from utils import best_match
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List
from sympy import symbols, lambdify
from sympy.parsing.sympy_parser import parse_expr
from sympy.core.symbol import Symbol
from huggingface_hub import HfApi
import pyarrow as pa
import pyarrow.parquet as pq
import json
import shutil


@dataclass
class Gate:
  """Represents a gate instance in a structural netlist."""
  cell: str
  name: str
  connections: Dict[str, str]  # port name → net name


@dataclass
class Netlist:
  """A combinational netlist parsed from a Verilog module."""
  inputs: List[str]
  outputs: List[str]
  wires: List[str]
  gates: List[Gate]

  @property
  def all_nets(self) -> List[str]:
    return list(dict.fromkeys(self.inputs + self.outputs + self.wires))


def float_cols_to_int_with_x(df: pd.DataFrame) -> pd.DataFrame:
  """
  """
  df = df.copy()
  float_cols = df.select_dtypes(include=["float", "Float64"]).columns
  for c in float_cols:
    df[c] = df[c].map(lambda v: 'x' if pd.isna(v) else int(v))
  return df


def is_every_net_evaluated(machine: dict) -> bool:
  flag = True
  for n in machine.values():
    if not isinstance(n, int):
      flag = False
      break
  return flag


def parse_range(rng: str | None) -> int:
    """Convert [msb:lsb] into integer width. If None, return 1."""
    if not rng:
        return 1
    msb, lsb = map(int, re.findall(r"\d+", rng))
    return abs(msb - lsb) + 1


def get_net_length(verilog_text: str, keyword: str, decl_re: re.compile, name_re: re.compile) -> dict:
  """
  Get the length of the nets for a given keyword.
  """
  keyword = keyword.lower()
  assert keyword in {"input", "output", "inout", "wire", "reg", "tri"}, f"Keyword must be input/output/inout/wire/reg/tri, got {keyword}"

  nets = dict()
  for m in decl_re.finditer(verilog_text):
    kind = m.group("kind")
    if kind != keyword:
      continue
    packed = m.group("packed")
    rest = m.group("rest")
    bus_width = parse_range(packed)
    for token in rest.split(","):
      nm = name_re.match(token)
      if not nm:
        continue
      unpacked = nm.group("unpacked")
      array_len = parse_range(unpacked)
      nets[token.strip()] = (bus_width, array_len, True if packed else False, True if unpacked else False)

  return nets


def expand_nets(nets: dict) -> list:
  """
  Expand the nets to include the bus width and the array length.
  """
  expanded_nets = []
  for net, (bus_len, array_len, packed, unpacked) in nets.items():
    if bus_len == 1 and array_len == 1 and not packed and not unpacked:
      expanded_nets.append(net)
      continue
    for i in range(bus_len):
      for j in range(max(1, array_len)):
        suffix = ""
        if packed:
          suffix += f"[{i}]"
        if unpacked:
          suffix += f"[{j}]"
        expanded_nets.append(f"{net}{suffix}")
  return expanded_nets


def df_to_compact_markdown(
  df: pd.DataFrame,
  *,
  include_index: bool = True,
  include_index_name: bool = False,   # matches your example (blank top-left cell)
  na_rep: str = "",
  col_sep: str = " / ",               # for MultiIndex columns
  idx_sep: str = " / ",               # for MultiIndex index
) -> str:
  """
  Emits a compact GitHub-flavored markdown table like:
  | | Good Machine | Bad Machine |
  |-|-|-|
  | net1 | 0 | 0 |
  ...
  Constraints:
  - exactly one space padding inside each data/header cell: `| {cell} |`
  - separator row uses only '-' and '|': `|-|-|-|`
  """

  if not isinstance(df, pd.DataFrame):
    df = pd.DataFrame(df)
  def _mi_to_str(x, sep: str) -> str:
    if isinstance(x, tuple):
      parts = ["" if p is None else str(p) for p in x]
      s = sep.join(parts).strip()
      return s
    return "" if x is None else str(x)
  def _escape_cell(s: str) -> str:
    # keep markdown structure stable
    s = s.replace("|", r"\|")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\n", "<br>")
    return s

  # Build headers
  col_labels = [_escape_cell(_mi_to_str(c, col_sep)) for c in df.columns.tolist()]

  # Build index labels
  if include_index:
    if isinstance(df.index, pd.MultiIndex):
      idx_labels = [_escape_cell(_mi_to_str(t, idx_sep)) for t in df.index.tolist()]
    else:
      idx_labels = [_escape_cell(_mi_to_str(i, idx_sep)) for i in df.index.tolist()]
  else:
    idx_labels = []

  # Top-left header cell
  if include_index:
    idx_name = "" if not include_index_name else _escape_cell("" if df.index.name is None else str(df.index.name))
    header_cells = [idx_name] + col_labels
  else:
    header_cells = col_labels

  # Markdown lines
  lines = []
  lines.append("| " + " | ".join(header_cells) + " |")
  lines.append("|" + "|".join("-" for _ in header_cells) + "|")
  # Data rows
  for r, row in enumerate(df.itertuples(index=False, name=None)):
    row_cells = []
    for v in row:
      if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        s = na_rep
      else:
        s = str(v)
      row_cells.append(_escape_cell(s))
    if include_index:
      line_cells = [idx_labels[r]] + row_cells
    else:
      line_cells = row_cells

    lines.append("| " + " | ".join(line_cells) + " |")

  return "\n".join(lines)


# Global cache for compiled gate functions
_compiled_gate_cache = {}


def get_compiled_func(gate_type, port, gate_func):
  key = (gate_type, port)
  if key in _compiled_gate_cache:
    return _compiled_gate_cache[key]
  
  entry = gate_func[gate_type][port]
  expr = entry['function']
  sym_names = sorted(entry['symbols'].keys())
  syms = [symbols(n) for n in sym_names]
  # Compile lambda
  # Use 'numpy' for potential vectorization support, though we use scalars here mostly.
  try:
    fn = lambdify(syms, expr, modules='numpy')
  except Exception as e:
    print(f"Error compiling lambda for {gate_type} {port}: {e}")
    fn = None
      
  _compiled_gate_cache[key] = (fn, sym_names)
  return fn, sym_names


class OptimizedNetlist:
  def __init__(self, verilog_text, gate_func, decl_re, name_re):
    self.instructions = []
    
    # Cache net lists
    # self.wire_nets = expand_nets(get_net_length(verilog_text, "wire", decl_re, name_re))
    self.input_nets = expand_nets(get_net_length(verilog_text, "input", decl_re, name_re))
    self.output_nets = expand_nets(get_net_length(verilog_text, "output", decl_re, name_re))
    
    self.parse(verilog_text, gate_func)

  def parse(self, verilog_text, gate_func):
    GATE_INGREDIENT = r"((?:\\[^\s]+|\w+))\s+((?:\\[^\s]+|\w+))\s*\(\s*([\s\S]+?)\s*\)\s*;"
    GATE_CONNECTIONS = r"\.((?:\\[^\s]+|\w+))\s*\(\s*([^)]+?)\s*\)"
    ASSIGN_STATEMENT = r"assign\s+(?P<net>(?:\\[^\s]+|[\w\[\]\*]+))\s*=\s*(?P<val>(?:\\[^\s]+|[\w\[\]\*]+))\s*;"
    LOGIC_VALUE = r"\*Logic(?P<val>[01]+)\*\s*"

    gate_ingredients = re.compile(GATE_INGREDIENT)
    gate_connections = re.compile(GATE_CONNECTIONS)
    assign_re = re.compile(ASSIGN_STATEMENT)
    logic_value = re.compile(LOGIC_VALUE)
    
    # Parse assigns
    for match in assign_re.finditer(verilog_text):
      assign_net, assign_value = match.groups()
      m = logic_value.search(assign_value)
      if m:
        assign_value = m.group('val')
      if assign_value.isdigit():
        val = int(assign_value)
        self.instructions.append(('assign_const', assign_net, val))
      else:
        self.instructions.append(('assign_net', assign_net, assign_value))

    # Parse gates
    for match in gate_ingredients.finditer(verilog_text):
      gate_type, instance, connections_str = match.groups()
      if gate_type.startswith('module'):
        continue
      
      if gate_type not in gate_func:
        # Fallback or skip? Original code would likely fail later or skip
        # Assuming mostly valid standard cells
        continue

      connections = dict(gate_connections.findall(connections_str))
      
      # Clean logic values in connections
      for p, n in connections.items():
        m = logic_value.search(n)
        if m:
          connections[p] = m.group('val')

      gate_output_ports = set(gate_func[gate_type].keys())
      instance_ports = set(connections.keys())
      
      out_ports = instance_ports.intersection(gate_output_ports)
      in_ports = instance_ports.difference(gate_output_ports)
      
      # We store instruction for each output port
      for out_p in out_ports:
        out_net = connections[out_p]
        input_map = {p: connections.get(p) for p in in_ports} # map port name to net name
        self.instructions.append(('gate', gate_type, instance, out_p, out_net, input_map))

def convert_string_to_dict(x, sep=':'):
  return {net.strip(): int(value.strip()) for net_value in x.split(',') for net, value in [net_value.split(sep)]}

def fast_fault_sim(input_nets_and_vector, expected_output_nets_and_vector, fault, optimized_netlist, gate_func, return_rewards = False):
  """
  Performs fault simulation comparing good machine vs bad machine (with stuck-at fault).
  
  Also computes:
  - fault_propagation_path: nets where the fault effect propagates forward toward outputs
  - backtrack_fault_path: inputs that control the fault propagation (sensitizing inputs)
  """
  FAULT_VALUE = r"sa(\d)\s*(.*)"
  fault_value_re = re.compile(FAULT_VALUE)
  
  try:
    faulty_value_str, faulty_net = fault_value_re.findall(fault)[0]
    faulty_value = int(faulty_value_str)
  except:
    import traceback; traceback.print_exc()
    return pd.DataFrame() # Or error
  
  # Fault propagation path from the injection point to the outputs
  fault_propagation_path = [faulty_net] # injection point
  
  if isinstance(input_nets_and_vector, str):
    input_nets_and_vector = convert_string_to_dict(input_nets_and_vector, sep=':' if ':' in input_nets_and_vector else '=')
  if isinstance(expected_output_nets_and_vector, str):
    expected_output_nets_and_vector = convert_string_to_dict(expected_output_nets_and_vector, sep=':' if ':' in expected_output_nets_and_vector else '=')
  _inputs = list(input_nets_and_vector.keys())
  _outputs = list(expected_output_nets_and_vector.keys())
  
  # Calculate rewards if input and output vectors have the correct length
  if return_rewards:
    rewards = {}
    # Check if the input vector has the correct length and if the nets names are correct
    rewards['input_nets_match'] = _inputs == optimized_netlist.input_nets
    # Check if the output vector has the correct length and if the nets names are correct
    rewards['output_nets_match'] = _outputs == optimized_netlist.output_nets
  
  good_machine = input_nets_and_vector.copy()
  good_machine.update(expected_output_nets_and_vector)
  
  bad_machine = input_nets_and_vector.copy()
  bad_machine.update({faulty_net: faulty_value})
  
  # Dependency graph: maps each net to the set of nets it directly depends on
  # Used for backtracing from fault propagation path to controlling inputs
  net_dependencies = {}
  
  # Helper to resolve value
  def resolve(machine, val, max_depth=20):
    if isinstance(val, int): return val
    visited = 0
    curr = val
    while not isinstance(curr, int):
      if isinstance(curr, str) and curr.isdigit():
        return int(curr)
      if curr not in machine:
        return curr # Return name if missing (not computed yet)
      nex = machine[curr]
      if nex == curr: break # Self-loop?
      curr = nex
      visited += 1
      if visited > max_depth: break 
    return curr
  
  # Pass 1: Evaluate instructions and build dependency graph
  for instr in optimized_netlist.instructions:
    typ = instr[0]
    
    if typ == 'assign_const':
      _, net, val = instr
      if net not in good_machine: good_machine[net] = val
      
      # No dependencies for constant assignments
      net_dependencies[net] = set()
      
      # Bad Machine
      # If net is fault path (i.e. it IS the faulty net), we don't overwrite it with the assign
      if net in fault_propagation_path:
        pass
      else:
        if net not in bad_machine: bad_machine[net] = val
        
    elif typ == 'assign_net':
      _, net, src = instr
      
      # Track dependency: net depends on src
      if not (isinstance(src, str) and src.isdigit()):
        net_dependencies[net] = {src}
      else:
        net_dependencies[net] = set()
      
      # Good Machine Update
      src_val_gm = resolve(good_machine, src)
      good_machine[net] = src_val_gm
      
      # Bad Machine Update
      if net in fault_propagation_path:
        continue # Already stuck
      
      src_val_bm = resolve(bad_machine, src)
      bad_machine[net] = src_val_bm
      
      # Fault Propagation
      if src in fault_propagation_path:
        fault_propagation_path.append(net)
      
    elif typ == 'gate':
      _, gate_type, instance, out_port, out_net, input_map = instr
      
      fn, sym_names = get_compiled_func(gate_type, out_port, gate_func)
      if not fn: continue
      
      # Track dependencies: out_net depends on all input nets of this gate
      gate_input_nets = set()
      for sym in sym_names:
        p_net = input_map.get(sym)
        if p_net is not None and not (isinstance(p_net, str) and p_net.isdigit()):
          gate_input_nets.add(p_net)
      net_dependencies[out_net] = gate_input_nets
      
      def eval_gate_inputs(machine):
        args = []
        for sym in sym_names:
          p_net = input_map.get(sym)
          if p_net is None: return None # Should not happen
          val = resolve(machine, p_net)
          
          if not isinstance(val, int):
            if isinstance(val, str) and val.isdigit():
              val = int(val)
            else:
              return None # Can't evaluate yet
          args.append(val)
        return args
      
      # Good Machine
      args_gm = eval_gate_inputs(good_machine)
      if args_gm:
        out_val = int(bool(fn(*args_gm)))
        good_machine[out_net] = out_val
      
      # Bad Machine
      if out_net not in fault_propagation_path:
        args_bm = eval_gate_inputs(bad_machine)
        if args_bm:
          out_val_bm = int(bool(fn(*args_bm)))
          bad_machine[out_net] = out_val_bm
          
          # Check propagation
          if args_gm and good_machine.get(out_net) != out_val_bm:
            fault_propagation_path.append(out_net)
  
  # Final Cleanup Loops (resolve any remaining aliases)
  def cleanup(machine):
    keys = list(machine.keys())
    # We iterate a few times to settle aliases
    for _ in range(5): # Cap iterations
      changed = False
      for net in keys:
        val = machine[net]
        if not isinstance(val, int):
          res = resolve(machine, val)
          if res != val:
            machine[net] = res
            changed = True
      if not changed: break
  
  cleanup(good_machine)
  cleanup(bad_machine)
  
  # Filter keys
  good_keys_to_remove = [net for net in good_machine.keys() if isinstance(net, int) or (isinstance(net, str) and net.isdigit())]
  for net in good_keys_to_remove: good_machine.pop(net)
  
  bad_keys_to_remove = [net for net in bad_machine.keys() if isinstance(net, int) or (isinstance(net, str) and net.isdigit())]
  for net in bad_keys_to_remove: bad_machine.pop(net)
  
  # ============================================================
  # Backtracing: Find the inputs that control fault propagation
  # ============================================================
  # We trace backwards from nets in the fault_propagation_path to find
  # all the primary inputs that affect whether the fault propagates.
  # These are the "sensitizing inputs" that must have specific values
  # for the fault effect to be observable at the outputs.
  
  def compute_backtrack_path(propagation_path, net_deps, primary_inputs):
    """
    Trace backwards from the fault propagation path to find controlling inputs.
    
    For each net on the fault propagation path, we recursively find all nets it depends on,
    continuing until we reach primary inputs. The result is the set of primary inputs
    that control the fault propagation (sensitizing inputs).
    
    Args:
      propagation_path: List of nets where fault propagates (forward path)
      net_deps: Dictionary mapping net -> set of nets it depends on
      primary_inputs: Set of primary input net names
    
    Returns:
      List of primary inputs that control the fault propagation
    """
    backtrack_inputs = set()
    visited = set()
    
    # Start from all nets on the propagation path
    to_visit = set(propagation_path)
    
    while to_visit:
      net = to_visit.pop()
      if net in visited:
        continue
      visited.add(net)
      
      # If this is a primary input, add to backtrack result
      if net in primary_inputs:
        backtrack_inputs.add(net)
      # Otherwise, add its dependencies to visit
      elif net in net_deps:
        to_visit.update(net_deps[net])
        backtrack_inputs.update(net_deps[net])
    return list(backtrack_inputs)
  
  _inputs_set = set(_inputs)
  backtrack_fault_path = compute_backtrack_path(fault_propagation_path, net_dependencies, _inputs_set)
  
  simulation = pd.concat([pd.Series(good_machine), pd.Series(bad_machine)], axis=1)
  simulation[2] = simulation.index.isin(_inputs)
  simulation[3] = simulation.index.isin(_outputs)
  simulation[4] = simulation.index.isin(fault_propagation_path)
  simulation[5] = simulation.index.isin(backtrack_fault_path)

  simulation = float_cols_to_int_with_x(simulation)
  simulation.columns = ["Good Machine", "Bad Machine", "PIs", "POs", "Fault Propagation Path", "Backtrack Sensitizing Inputs"]
  
  priority = {
    (True,  False): 0,
    (False, False): 1,
    (False, True):  2
  }
  simulation["Priority"] = simulation[["PIs", "POs"]].apply(tuple, axis=1).map(priority)
  simulation = simulation.sort_values("Priority").drop(columns=["Priority"])
  
  if return_rewards:
    return simulation, rewards
  return simulation


def process(x, base_prompt_dict, optimized_netlist, module_name, tetramax_folder, gate_func):
  """
  Process a single pattern row to generate test vector data for detected faults.
  
  NOTE: This function creates a FRESH dict for each result to avoid race conditions
  when using multiprocessing. The base_prompt_dict is only used as a template and
  is deep-copied for each output record.
  """
  vector_idx, input_vector, expected_output = x.name, x[0], x[1]
  input_vector = ','.join(input_vector).split(',')
  expected_output = ','.join(expected_output).split(',')
  
  input_nets = optimized_netlist.input_nets
  output_nets = optimized_netlist.output_nets
  
  input_nets_and_vector = {net: int(value) for net, value in zip(input_nets, input_vector)}
  expected_output_nets_and_vector = {net: int(value) for net, value in zip(output_nets, expected_output)}

  detected_file_path = tetramax_folder / module_name / "simulation/bad" / f"machine_detected_faults_{vector_idx}.csv"
  
  if not detected_file_path.exists():
    return [] # Changed from None to [] to behave well with explode/dropna
  detected_faults_per_vector_df = pd.read_csv(detected_file_path, sep=r'\s+', header=None).sort_values(1)
  # keep only the detected faults.
  detected_faults = detected_faults_per_vector_df[detected_faults_per_vector_df.loc[:, 1] == "DS"][[0, 2]]
  
  contents = []
  for _, _fault in detected_faults.iterrows():
    fault = _fault.str.cat(sep=' ')
    faulty_net = _fault.iloc[1]
    try:
      snapshot = fast_fault_sim(input_nets_and_vector, expected_output_nets_and_vector, fault, optimized_netlist=optimized_netlist, gate_func=gate_func)
    except:
      import traceback; traceback.print_exc()
      continue # Skip if sim fails

    # TetraMax and ATPG report faults that happen before the faulty net. 
    # Extract nets on the fault propagation path
    detected_faults_in_fault_path = snapshot[snapshot["Fault Propagation Path"] == True]['Bad Machine'].reset_index()
    detected_faults_in_fault_path = detected_faults_in_fault_path[["Bad Machine", "index"]]
    detected_faults_in_fault_path_str = 'sa'+detected_faults_in_fault_path.astype(str).apply(' '.join, axis=1).str.cat(sep=', sa')
    
    # Extract the backtrack sensitizing inputs that control fault propagation
    sensitizing_inputs = snapshot[snapshot["Backtrack Sensitizing Inputs"] == True].index.tolist()
    
    # ============================================================
    # Find gate types by parsing optimized_netlist.instructions
    # ============================================================
    # Get the set of nets on each path
    fault_propagation_nets = set(detected_faults_in_fault_path["index"].tolist())
    sensitizing_input_nets = set(sensitizing_inputs)
    
    # Find gates whose OUTPUT is on the fault propagation path
    # These gates are responsible for propagating the fault effect forward
    fault_propagation_gates = []
    fault_propagation_nets = detected_faults_in_fault_path['index'].tolist()
    for instr in optimized_netlist.instructions:
      if instr[0] == 'gate':
        _, gate_type, instance, out_port, out_net, input_map = instr
        if out_net in fault_propagation_nets:
          fault_propagation_gates.append(instance)
    
    # Find gates whose INPUTS include sensitizing inputs
    # These gates are driven by the sensitizing inputs (backward dependency)
    backtrack_gates = []
    for instr in reversed(optimized_netlist.instructions):
      if instr[0] == 'gate':
        _, gate_type, instance, out_port, out_net, input_map = instr
        # Check if any input net of this gate is a sensitizing input
        gate_input_nets = set(input_map.values())
        if gate_input_nets & sensitizing_input_nets:  # intersection
          backtrack_gates.append(instance)
    
    # Format as comma-separated strings
    fault_propagation_gates_str = ', '.join(fault_propagation_gates) if fault_propagation_gates else ''
    fault_propagation_nets_str = ', '.join(fault_propagation_nets) if fault_propagation_nets else ''
    backtrack_gates_str = ', '.join(backtrack_gates) if backtrack_gates else ''
    backtrack_nets_str = ', '.join(sensitizing_input_nets) if sensitizing_input_nets else ''

    # Create a FRESH deep copy for each result to avoid shared references
    # This prevents race conditions when workers process data in parallel
    result_dict = copy.deepcopy(base_prompt_dict)
    
    system_prompt = _system_prompts[random.randint(0, len(_system_prompts)-1)]
    result_dict.update({
      'fault': fault, 
      # Deep copy the dicts to ensure no shared references between results
      'input_vector': copy.deepcopy(input_nets_and_vector), 
      'expected_output': copy.deepcopy(expected_output_nets_and_vector), 
      'snapshot': df_to_compact_markdown(snapshot[["Good Machine", "Bad Machine"]]), 
      'detected_faults': detected_faults_in_fault_path_str,
      'fault_propagation_gates': fault_propagation_gates_str,  # Gates whose outputs are on propagation path
      'fault_propagation_nets': fault_propagation_nets_str,  # Nets on the fault propagation path
      'backtrack_gates': backtrack_gates_str,  # Gates whose inputs include sensitizing inputs
      'backtrack_nets': backtrack_nets_str,  # Nets on the backtrack path
    })
    user_prompt = _training_prompts_faults_list[random.randint(0, len(_training_prompts_faults_list)-1)]
    reasoning_content = _cot_assistant_response_faults_list[random.randint(0, len(_cot_assistant_response_faults_list)-1)]
    answer_content = _answer_template[random.randint(0, len(_answer_template)-1)]
    result_dict.update({
      'system_content': system_prompt,
      'user_content': user_prompt,
      'reasoning_content': reasoning_content,
      'answer_content': answer_content,
    })
    contents.append(result_dict)

  return contents


def process_per_row(row, tetramax_folder, gate_func, decl_re: re.compile, name_re: re.compile):
  """
  Process a single row from the dataset CSV.
  
  This function is designed to be called from multiprocessing workers.
  It creates a base template dict that is deep-copied for each output record
  to avoid any shared mutable state between results.
  """
  # Create a fresh base template dict for this row
  # This will be deep-copied for each individual result in process()
  base_prompt_dict = copy.deepcopy(_user_prompt_dict)
  
  # Get 'Pattern' from the row
  patterns = pd.read_csv(StringIO(row['patterns']), sep="\s+", dtype=str)
  patterns.columns = patterns.columns.astype(int)
  
  # Pre-parse netlist ONCE per row
  optimized_netlist = OptimizedNetlist(row['netlist'], gate_func, decl_re, name_re)
  
  # Add row-level data to the base template
  base_prompt_dict.update({
    'module_name': '_'.join(row['module_name'].split('_')[1:]), 
    'netlist': row['netlist']
  })
  
  _process = partial(
    process, 
    base_prompt_dict=base_prompt_dict,  # Renamed for clarity - this is a template, not shared state
    optimized_netlist=optimized_netlist, 
    module_name=row['module_name'], 
    tetramax_folder=tetramax_folder, 
    gate_func=gate_func
  )
  
  # Use explode to flatten list of lists, then reset index
  result_series = patterns.apply(_process, axis=1).explode().reset_index(drop=True)
  
  # Drop empty/NaN rows (from process returning [] or failed matches)
  result_series = result_series.dropna()
  
  if result_series.empty:
    return pd.DataFrame()
  
  # Convert Series of dicts to DataFrame
  return pd.DataFrame(result_series.tolist())


def _worker_init(seed_offset):
  """
  Initialize worker process with a unique random seed.
  
  When using fork-based multiprocessing, all workers inherit the parent's 
  random state, causing them to generate correlated "random" sequences.
  This initializer reseeds each worker with a unique seed based on:
  - The current process ID (unique per worker)
  - A seed offset provided by the parent (for reproducibility control)
  
  This prevents race conditions where multiple workers might generate
  identical "random" selections for system prompts, user prompts, etc.
  """
  import os
  # Create a unique seed for this worker using PID and optional offset
  worker_seed = os.getpid() + seed_offset
  random.seed(worker_seed)
  # Also seed numpy if it's being used
  try:
    np.random.seed(worker_seed)
  except:
    pass


if __name__ == '__main__':
  try:
      DATA_PATH = Path(os.environ["DATA_PATH"]).resolve()
      DATASET = os.environ["DATASET"]
      LIBRARY = os.environ["LIBRARY"]
      LIB_VARIANT = os.environ["LIB_VARIANT"]
      PVT_CORNER = os.environ["PVT_CORNER"]
      HF_USERNAME = os.environ.get("HF_USERNAME")
      REPO_NAME = os.environ.get("DATASET_HF_REPO_NAME")
  except KeyError:
      print("Environment variables not set. Exiting.")
      exit(0)

  suffix = f"{DATASET.lower()}.{LIBRARY.lower()}.{LIB_VARIANT.lower()}.{PVT_CORNER.lower()}"
  LIB_DIR = Path(f"lib/{LIBRARY}/LIB/CCS/").resolve()
  CATEGORIES = ["AO", "OA", "INVBUF", "SEQ", "SIMPLE"]
  print(f"Suffix: {suffix}, LIB_DIR: {LIB_DIR}, CATEGORIES: {CATEGORIES}")

  def _default_csv_dataset():
    return DATA_PATH / DATASET / f"dataset.{suffix}.csv"

  def _default_tetramax_folder():
    return DATA_PATH / DATASET / f"out.{suffix}"

  def _default_load_model():
    return os.environ["MODEL"]

  parser = argparse.ArgumentParser(description="Final Dataset Composition")
  parser.add_argument('-csv', '--csv_dataset', type=str, default=_default_csv_dataset())
  parser.add_argument('-tf', '--tetramax_folder', type=str, default=_default_tetramax_folder())
  parser.add_argument('-lm', '--load_model', type=str, default=_default_load_model())
  parser.add_argument('--export_config', type=str, help="Export simulation config to JSON file", default=None)
  args = parser.parse_args()

  # Regex to match a cell and its block recursively using regex module
  CELL_RE = re.compile(
      r'cell\s*\(\s*(?P<cell>[\w]+)\s*\)\s*(?P<block>\{(?:[^{}]++|(?&block))*\})',
      re.DOTALL
  )

  # Regex to match a pin and its block recursively
  PIN_RE = re.compile(
      r'pin\s*\(\s*(?P<pin>[\w]+)\s*\)\s*(?P<block>\{(?:[^{}]++|(?&block))*\})',
      re.DOTALL
  )

  # Regex to find direction and function inside a pin block
  DIRECTION_RE = re.compile(r'direction\s*:\s*(?P<direction>\w+)\s*;')
  FUNCTION_RE = re.compile(r'function\s*:\s*"(?P<function>[^"]+)"\s*;')

  # Regex to capture full declarations
  decl_re = re.compile(r"""
      ^\s*
      (?P<kind>input|output|inout|wire|reg)\b      # keyword
      \s*
      (?P<packed>\[\s*\d+\s*:\s*\d+\s*\])?         # optional packed bus, e.g. [15:0]
      \s*
      (?P<rest>[^;]+)                              # list of signals
      ;
  """, re.VERBOSE | re.MULTILINE)

  # Regex to capture each signal in the list
  name_re = re.compile(r"""
      ^\s*
      (?P<name>(?:\\[^\s]+)|(?:[A-Za-z_]\w*))      # signal name
      \s*
      (?P<unpacked>\[\s*\d+\s*:\s*\d+\s*\])?       # optional unpacked dimension
      \s*$
  """, re.VERBOSE)

  SYMBOLS_RE = re.compile(r'\w+')
  
  gate_func = {}
  for cat in CATEGORIES:
    # Gather all .lib files that contain the triplet somewhere
    lib_files = list(LIB_DIR.glob(f"*{cat}_{LIB_VARIANT}_{PVT_CORNER}*.lib"))
    if not lib_files:
        continue
    pick = best_match(lib_files, cat, LIB_VARIANT, PVT_CORNER)
    if pick:
      with open(pick, 'r') as f:
        data = f.read()
        # Iterate over all cells in the library
        for match in CELL_RE.finditer(data):
          cell_name = match.group('cell')
          cell_content = match.group('block')
          outputs = {}
          
          # Iterate over all pins in the cell
          for pin_match in PIN_RE.finditer(cell_content):
            pin_name = pin_match.group('pin')
            pin_content = pin_match.group('block')
            
            # Check direction
            dir_m = DIRECTION_RE.search(pin_content)
            if not dir_m or dir_m.group('direction') != 'output':
                continue
            
            # Check function
            func_m = FUNCTION_RE.search(pin_content)
            if not func_m:
                continue
            
            fn = func_m.group('function').replace('!', '~').replace('*', '&').replace('+', '|')
            names = sorted(set(SYMBOLS_RE.findall(func_m.group('function'))))
            syms = symbols(' '.join(names))
            if isinstance(syms, Symbol):
              syms = [syms]
            
            outputs[pin_name] = {
              'function': parse_expr(fn, local_dict=dict(zip(names, syms)), evaluate=False),
              'symbols': dict.fromkeys(names),
              'expr_str': fn  # Store string for C++ export
            }
            
          if outputs:
            gate_func[cell_name] = outputs
  
  if args.export_config:
    import json
    # Prepare serializable config
    # Convert gate_func to just strings
    serializable_gate_func = {}
    for cell, pins in gate_func.items():
      serializable_gate_func[cell] = {}
      for pin, data in pins.items():
        serializable_gate_func[cell][pin] = data['expr_str']
    
    config_dump = {
        "gate_funcs": serializable_gate_func,
        "system_prompts": _system_prompts,
        "user_prompts": _training_prompts_faults_list,
        "assistant_prompts": _cot_assistant_response_faults_list,
    }
    with open(args.export_config, 'w') as f:
      json.dump(config_dump, f, indent=2)
    print(f"Configuration exported to {args.export_config}")
  
  # Pre-compile all gate functions BEFORE creating the multiprocessing pool
  # This populates _compiled_gate_cache so workers inherit the pre-compiled lambdas
  # rather than each worker redundantly compiling the same functions
  print("Pre-compiling gate functions for multiprocessing efficiency...")
  for gate_type, pins in gate_func.items():
    for port in pins.keys():
      get_compiled_func(gate_type, port, gate_func)
  print(f"Pre-compiled {len(_compiled_gate_cache)} gate functions")
  
  dataset = args.csv_dataset
  tetramax_folder = args.tetramax_folder
  model = args.load_model
  print(f"Dataset: {dataset}, Tetramax folder: {tetramax_folder}, Model: {model}")
  
  # Use streaming to avoid memory issues with large datasets
  # df = pd.read_csv(dataset)
  # df.sort_values("num_instances", inplace=True) ... (Skipping in-memory sort)
  
  tokenizer = AutoTokenizer.from_pretrained(model)
  cpu_pool = os.cpu_count() or 1
  
  _process_per_row = partial(process_per_row, tetramax_folder=tetramax_folder, gate_func=gate_func, decl_re=decl_re, name_re=name_re)
  
  # Output directory for sharded files
  output_shards_dir = DATA_PATH / DATASET / f"dataset.{suffix}_shards"
  if output_shards_dir.exists():
      shutil.rmtree(output_shards_dir)
  output_shards_dir.mkdir(parents=True, exist_ok=True)
  
  # Calculate total lines for tqdm estimation
  try:
    print("Counting total rows to process...")
    total_lines = 0
    with pd.read_csv(dataset, chunksize=10000) as reader:
      for chunk in reader:
        chunk = chunk[(chunk.num_instances > 0) & (chunk.num_instances < 100)].dropna()
        total_lines += len(chunk)
    print(f"Total rows to process: {total_lines}")
  except Exception as e:
    print(f"Error counting rows: {e}")
    total_lines = None
  
  def data_generator():
    chunksize = 100
    with pd.read_csv(dataset, chunksize=chunksize) as reader:
      for chunk in reader:
        chunk = chunk[(chunk.num_instances > 0) & (chunk.num_instances < 100)].dropna()
        for record in chunk.to_dict(orient='records'):
          yield record

  pool = None
  
  # Seed offset for worker initialization - use current time for randomness
  # or set to a fixed value for reproducibility
  import time
  seed_offset = int(time.time())

  try:
    if cpu_pool > 1:
      # Create pool with worker initializer to reseed random number generators
      # This prevents race conditions where workers generate correlated random sequences
      pool = mp.Pool(
        processes=cpu_pool,
        initializer=_worker_init,
        initargs=(seed_offset,)
      )
      iterator = pool.imap(_process_per_row, data_generator())
    else:
      iterator = map(_process_per_row, data_generator())

    writer = None
    shard_idx = 0
    rows_in_current_shard = 0
    ROWS_PER_SHARD = 1000

    # Define explicit schema to handle nested structures (dicts -> Map) consistently
    # Changed Map to String (JSON) for better compatibility and memory usage
    schema = pa.schema([
        ('fault', pa.string()),
        ('system_content', pa.string()),
        ('user_content', pa.string()),
        ('reasoning_content', pa.string()),
        ('answer_content', pa.string()),
        ('module_name', pa.string()),
        ('netlist', pa.string()),
        ('input_vector', pa.string()), # Converted to JSON string
        ('expected_output', pa.string()), # Converted to JSON string
        ('snapshot', pa.string()),
        ('detected_faults', pa.string()),
        ('fault_propagation_gates', pa.string()),  # Gates whose outputs are on fault propagation path
        ('fault_propagation_nets', pa.string()),  # Nets on the fault propagation path
        ('backtrack_gates', pa.string()),  # Gates whose inputs include sensitizing inputs
        ('backtrack_nets', pa.string()),  # Backtrack: inputs controlling and non-controlling nets
    ])
    
    for i, df_chunk in tqdm(enumerate(iterator), total=total_lines, desc=f"Processing..."):
      if df_chunk is None or df_chunk.empty:
        print(f"Empty chunk... {i}")
        continue
      # Ensure columns are in schema
      for col in schema.names:
        if col not in df_chunk.columns:
          df_chunk[col] = None
          
      # Convert dicts to JSON strings for map columns
      for col in ['input_vector', 'expected_output']:
        if col in df_chunk.columns:
          df_chunk[col] = df_chunk[col].apply(lambda x: json.dumps(x) if x is not None else None)
          
      try:
        table = pa.Table.from_pandas(df_chunk, schema=schema)
      except Exception as e:
        print(f"Error converting chunk to table: {e}")
        continue
      
      if writer is None:
        shard_path = output_shards_dir / f"data-{shard_idx:05d}.parquet"
        writer = pq.ParquetWriter(shard_path, schema)
      writer.write_table(table)
      rows_in_current_shard += len(df_chunk)
      
      if rows_in_current_shard >= ROWS_PER_SHARD:
        writer.close()
        writer = None
        rows_in_current_shard = 0
        shard_idx += 1

    if writer:
      writer.close()

  finally:
    if pool:
      pool.close()
      pool.join()

  print(f"Dataset written to {output_shards_dir}")

  # Upload to Hugging Face
  if HF_USERNAME and REPO_NAME:
    api = HfApi()
    REPO_ID = f"{HF_USERNAME}/{REPO_NAME}"
    print(f"Uploading to Hugging Face Hub: {REPO_ID}")
    
    try:
      api.create_repo(
        repo_id=REPO_ID, 
        repo_type="dataset",
        private=True,
        exist_ok=True
      )
      
      api.upload_folder(
        folder_path=str(output_shards_dir),
        repo_id=REPO_ID,
        repo_type="dataset",
        commit_message="Upload sharded dataset with JSON-serialized maps"
      )
      print(f"Successfully uploaded shards to {REPO_ID}")
      
      # Also try to upload README if exists
      readme_path = DATA_PATH / f"README.{suffix}.md"
      if readme_path.exists():
        api.upload_file(
          path_or_fileobj=str(readme_path),
          path_in_repo="README.md",
          repo_id=REPO_ID,
          repo_type="dataset",
        )
        print(f"Uploaded README.md")

    except Exception as e:
      print(f"Error uploading to Hugging Face: {e}")
  else:
    print("Skipping upload: HF_USERNAME or DATASET_HF_REPO_NAME not set.")
