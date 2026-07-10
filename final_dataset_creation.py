#!/usr/bin/env python

import pandas as pd
import numpy as np
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
from transformers import AutoTokenizer
import random
from io import StringIO
from functools import partial
from tqdm import tqdm
import argparse
import multiprocessing as mp
import copy
from utils import best_match
from pathlib import Path
from typing import Dict, List, Tuple
from sympy import symbols
from sympy.parsing.sympy_parser import parse_expr
from sympy.core.symbol import Symbol
from huggingface_hub import HfApi
import pyarrow as pa
import pyarrow.parquet as pq
import json
import shutil

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
  parser.add_argument('--eval_ratio', type=float, default=0.1, help="Fraction of data to assign to eval split (default: 0.1)")
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
  
  # Output directory for sharded files (with train/eval subdirectories)
  output_shards_dir = DATA_PATH / DATASET / f"dataset.{suffix}_shards"
  if output_shards_dir.exists():
      shutil.rmtree(output_shards_dir)
  train_dir = output_shards_dir / "train"
  eval_dir  = output_shards_dir / "eval"
  train_dir.mkdir(parents=True, exist_ok=True)
  eval_dir.mkdir(parents=True, exist_ok=True)
  eval_ratio = args.eval_ratio
  print(f"Split ratio: train={1 - eval_ratio:.0%}, eval={eval_ratio:.0%}")
  
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

    # Per-split writers and counters
    writers = {"train": None, "eval": None}
    shard_idxs = {"train": 0, "eval": 0}
    rows_in_shard = {"train": 0, "eval": 0}
    split_dirs = {"train": train_dir, "eval": eval_dir}
    total_rows_written = {"train": 0, "eval": 0}
    module_name_correct = 0
    module_name_fixed   = 0
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

    def _write_split(split_name, table):
      """Write a pyarrow Table to the appropriate split shard."""
      if table.num_rows == 0:
        return
      if writers[split_name] is None:
        shard_path = split_dirs[split_name] / f"data-{shard_idxs[split_name]:05d}.parquet"
        writers[split_name] = pq.ParquetWriter(shard_path, schema)
      writers[split_name].write_table(table)
      rows_in_shard[split_name] += table.num_rows
      total_rows_written[split_name] += table.num_rows

      if rows_in_shard[split_name] >= ROWS_PER_SHARD:
        writers[split_name].close()
        writers[split_name] = None
        rows_in_shard[split_name] = 0
        shard_idxs[split_name] += 1
    
    for i, df_chunk in tqdm(enumerate(iterator), total=total_lines, desc=f"Processing..."):
      if df_chunk is None or df_chunk.empty:
        print(f"Empty chunk... {i}")
        continue
      # Ensure columns are in schema
      for col in schema.names:
        if col not in df_chunk.columns:
          df_chunk[col] = None
          
      # Verify module_name matches the netlist header and fix mismatches
      if 'netlist' in df_chunk.columns and 'module_name' in df_chunk.columns:
        for idx in df_chunk.index:
          netlist_val = df_chunk.at[idx, 'netlist']
          mname_val   = df_chunk.at[idx, 'module_name']
          if pd.notna(netlist_val) and pd.notna(mname_val):
            corrected, changed = verify_module_name(str(netlist_val), str(mname_val))
            if changed:
              df_chunk.at[idx, 'module_name'] = corrected
              module_name_fixed += 1
            else:
              module_name_correct += 1

      # Convert dicts to JSON strings for map columns
      for col in ['input_vector', 'expected_output']:
        if col in df_chunk.columns:
          df_chunk[col] = df_chunk[col].apply(lambda x: json.dumps(x) if x is not None else None)

      # Randomly assign each row to train or eval (equivalent to shuffle + split)
      mask = np.random.random(len(df_chunk)) < eval_ratio
      df_eval  = df_chunk[mask]
      df_train = df_chunk[~mask]

      for split_name, df_split in [("train", df_train), ("eval", df_eval)]:
        if df_split.empty:
          continue
        try:
          table = pa.Table.from_pandas(df_split, schema=schema)
        except Exception as e:
          print(f"Error converting chunk to table ({split_name}): {e}")
          continue
        _write_split(split_name, table)

    # Close any remaining open writers
    for split_name in writers:
      if writers[split_name]:
        writers[split_name].close()

    print(f"Rows written — train: {total_rows_written['train']}, eval: {total_rows_written['eval']}")
    print(f"Module name verification — correct: {module_name_correct}, fixed: {module_name_fixed}")

  finally:
    if pool:
      pool.close()
      pool.join()

  print(f"Dataset written to {output_shards_dir}")
  print(f"  train/ : {shard_idxs['train'] + (1 if rows_in_shard['train'] else 0)} shard(s)")
  print(f"  eval/  : {shard_idxs['eval']  + (1 if rows_in_shard['eval']  else 0)} shard(s)")

  # Upload to Hugging Face
  # The folder structure  train/*.parquet  and  eval/*.parquet  is auto-detected
  # by HuggingFace datasets, so load_dataset(repo, split="train") works out of the box.
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
      
      # Upload each split folder so HF auto-detects the splits
      for split_name in ("train", "eval"):
        split_path = output_shards_dir / split_name
        if any(split_path.iterdir()):
          api.upload_folder(
            folder_path=str(split_path),
            path_in_repo=split_name,
            repo_id=REPO_ID,
            repo_type="dataset",
            commit_message=f"Upload {split_name} split shards"
          )
          print(f"Uploaded {split_name}/ split to {REPO_ID}")
      
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
