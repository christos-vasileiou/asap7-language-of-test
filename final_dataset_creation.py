#!/usr/bin/env python

import pandas as pd
import os
import regex as re
from vars import (
  _user_prompt_dict,
  _training_prompts_faults_list,
  _cot_assistant_response_faults_list,
  _system_prompts, 
  _answer_template,
  chat_template, 
)
import random
from io import StringIO
from functools import partial
import argparse
import copy
from utils import best_match
from pathlib import Path
from typing import Dict, List, Tuple
from sympy import symbols
from sympy.parsing.sympy_parser import parse_expr
from sympy.core.symbol import Symbol
import json

# Extracted modules
from netlist_utils import Gate, Netlist, parse_range, get_net_length, expand_nets, verify_module_name
from fault_sim import (
  float_cols_to_int_with_x,
  is_every_net_evaluated,
  convert_string_to_dict,
  _compiled_gate_cache,
  get_compiled_func,
  OptimizedNetlist,
  fast_fault_sim,
)
from df_format import df_to_json, df_to_compact_markdown
from pattern_mapping import MAPPING_VERSION, read_pattern_mapping
from fault_claims import verified_fault_claims


def process(x, base_prompt_dict, optimized_netlist, module_name, tetramax_folder, gate_func, pattern_mapping):
  """
  Process a single pattern row to generate test vector data for detected faults.
  
  NOTE: This function creates a FRESH dict for each result to avoid race conditions
  when using multiprocessing. The base_prompt_dict is only used as a template and
  is deep-copied for each output record.
  """
  vector_idx, input_vector, expected_output = x.name, x[0], x[1]
  input_nets_and_vector, expected_output_nets_and_vector = pattern_mapping.vectors(
    vector_idx, input_vector, expected_output,
    optimized_netlist.input_nets, optimized_netlist.output_nets,
  )

  detected_file_path = tetramax_folder / module_name / "simulation/bad" / f"machine_detected_faults_{vector_idx}.csv"
  
  if not detected_file_path.exists():
    raise FileNotFoundError(detected_file_path)
  detected_faults = []
  for line in detected_file_path.read_text().splitlines():
    if not line.strip():
      continue
    fields = line.split(None, 2)
    if len(fields) != 3 or fields[0] not in ('sa0', 'sa1'):
      raise ValueError(f"Malformed fault record in {detected_file_path}: {line}")
    if fields[1] == 'DS':
      detected_faults.append((fields[0], fields[2].strip()))
  
  contents = []
  for stuck_at, faulty_net in dict.fromkeys(detected_faults):
    fault = f'{stuck_at} {faulty_net}'
    snapshot = fast_fault_sim(input_nets_and_vector, expected_output_nets_and_vector, fault, optimized_netlist=optimized_netlist, gate_func=gate_func)
    outputs = snapshot.reindex(optimized_netlist.output_nets)
    if not outputs[['Good Machine', 'Bad Machine']].isin([0, 1]).all().all():
      raise ValueError(f"{module_name} pattern {vector_idx} {fault}: unresolved primary output")
    good = {net: int(value) for net, value in outputs['Good Machine'].items()}
    if good != expected_output_nets_and_vector:
      raise ValueError(f"{module_name} pattern {vector_idx}: STIL outputs disagree with custom simulation")
    if not (outputs['Good Machine'] != outputs['Bad Machine']).any():
      raise ValueError(f"{module_name} pattern {vector_idx} {fault}: reported DS fault is not detected by custom simulation")

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
    for gate_type, instance, out_port, out_net, input_map in optimized_netlist.gate_metadata:
      if out_net in fault_propagation_nets:
        fault_propagation_gates.append(instance)
    
    # Find gates whose INPUTS include sensitizing inputs
    # These gates are driven by the sensitizing inputs (backward dependency)
    backtrack_gates = []
    for gate_type, instance, out_port, out_net, input_map in reversed(optimized_netlist.gate_metadata):
      gate_input_nets = set(input_map.values())
      if gate_input_nets & sensitizing_input_nets:
        backtrack_gates.append(instance)
    
    # Format as comma-separated strings
    fault_propagation_gates_str = ', '.join(fault_propagation_gates) if fault_propagation_gates else ''
    fault_propagation_nets_str = ', '.join(fault_propagation_nets) if fault_propagation_nets else ''
    backtrack_gates_str = ', '.join(backtrack_gates) if backtrack_gates else ''
    backtrack_nets_str = ', '.join(sorted(sensitizing_input_nets)) if sensitizing_input_nets else ''

    # Create a FRESH deep copy for each result to avoid shared references
    # This prevents race conditions when workers process data in parallel
    result_dict = copy.deepcopy(base_prompt_dict)
    
    system_prompt = _system_prompts[random.randint(0, len(_system_prompts)-1)]
    result_dict.update({
      'fault': fault, 
      'pattern_index': int(vector_idx),
      # Deep copy the dicts to ensure no shared references between results
      'input_vector': copy.deepcopy(input_nets_and_vector), 
      'expected_output': copy.deepcopy(expected_output_nets_and_vector), 
      'snapshot': df_to_json(snapshot[["Good Machine", "Bad Machine"]]),
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

  for result, verified_claim in zip(contents, verified_fault_claims(contents), strict=True):
    result['detected_faults'] = verified_claim
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
  if not isinstance(row['patterns'], str) or not row['patterns'].strip():
    raise ValueError(f"{row['module_name']}: missing pattern table")
  patterns = pd.read_csv(StringIO(row['patterns']), sep=r"\s+", dtype=str)
  patterns.columns = patterns.columns.astype(int)
  patterns.index = patterns.index.astype(int)
  pattern_mapping = read_pattern_mapping(
    (Path(tetramax_folder) / row['module_name'] / 'simulation.stil').read_text()
  )
  if set(patterns.index) != set(pattern_mapping.patterns) or not patterns.index.is_unique:
    raise ValueError(f"{row['module_name']}: CSV/STIL pattern indices differ")
  if list(patterns.columns) != [0, 1]:
    raise ValueError(f"{row['module_name']}: expected PI and PO columns")
  
  # Pre-parse netlist ONCE per row
  optimized_netlist = OptimizedNetlist(row['netlist'], gate_func, decl_re, name_re)
  
  # Add row-level data to the base template
  base_prompt_dict.update({
    'module_name': '_'.join(row['module_name'].split('_')[1:]), 
    'netlist': row['netlist'],
    'source_module_name': row['module_name'],
    'mapping_version': MAPPING_VERSION,
  })
  
  _process = partial(
    process, 
    base_prompt_dict=base_prompt_dict,  # Renamed for clarity - this is a template, not shared state
    optimized_netlist=optimized_netlist, 
    module_name=row['module_name'], 
    tetramax_folder=Path(tetramax_folder),
    gate_func=gate_func,
    pattern_mapping=pattern_mapping,
  )
  
  # Use explode to flatten list of lists, then reset index
  result_series = patterns.apply(_process, axis=1).explode().reset_index(drop=True)
  
  # Drop empty/NaN rows (from process returning [] or failed matches)
  result_series = result_series.dropna()
  
  if result_series.empty:
    return pd.DataFrame()
  
  # Convert Series of dicts to DataFrame
  return pd.DataFrame(result_series.tolist())


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
      raise SystemExit(1)

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
  parser.add_argument('-lm', '--load_model', type=str, default=os.environ.get('MODEL'))
  parser.add_argument('--export_config', type=str, help="Export simulation config to JSON file", default=None)
  parser.add_argument('--sim_config', type=str, help="Use an existing simulator JSON instead of Liberty extraction")
  parser.add_argument('--output_dir', type=str, required=True, help="NEW local build directory; existing directories are refused")
  parser.add_argument('--validation_circuits', type=int, default=8, help="Number of whole circuit groups reserved before SFT")
  parser.add_argument('--seed', type=int, default=20260910)
  parser.add_argument('--workers', type=int, default=4)
  parser.add_argument('--quarantine_invalid_circuits', action='store_true', help="Exclude and report an entire circuit if any label fails validation")
  args = parser.parse_args()
  if args.workers < 1:
    parser.error('--workers must be positive')

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
  
  if args.sim_config:
    from fault_sim import _normalize_gate_func
    gate_func = _normalize_gate_func(args.sim_config)
  if not gate_func:
    raise ValueError('No gate functions loaded; provide --sim_config or valid Liberty files')

  if args.export_config:
    import json
    # Prepare serializable config
    # Convert gate_func to just strings
    serializable_gate_func = {}
    for cell, pins in gate_func.items():
      serializable_gate_func[cell] = {}
      for pin, data in pins.items():
        serializable_gate_func[cell][pin] = str(data['function']) if isinstance(data, dict) else str(data)
    
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
  
  from repaired_dataset import build_dataset
  build_dataset(args, gate_func, decl_re, name_re)
  print("Build complete. No data uploaded; review the manifest before publishing.")
