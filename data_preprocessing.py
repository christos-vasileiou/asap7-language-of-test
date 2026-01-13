#!/usr/bin/env python
from tqdm.auto import tqdm
import pandas as pd
import re
import argparse
import os
import multiprocessing as mp
from functools import partial
from pathlib import Path

import re

def parse_range(rng: str | None) -> int:
    """Convert [msb:lsb] into integer width. If None, return 1."""
    if not rng:
        return 1
    msb, lsb = map(int, re.findall(r"\d+", rng))
    return abs(msb - lsb) + 1

def count_bits_for_keyword(verilog_text: str, keyword: str, decl_re: re.compile, name_re: re.compile) -> int:
    """
    Count total bits declared for a specific keyword
    (input/output/inout/wire/reg).

    Example:
        count_bits_for_keyword(text, "input", decl_re, name_re)
    """
    keyword = keyword.lower()
    assert keyword in {"input", "output", "inout", "wire", "reg"}, f"Keyword must be input/output/inout/wire/reg, got {keyword}"

    total_bits = 0
    for m in decl_re.finditer(verilog_text):
      kind = m.group("kind")
      if kind != keyword:
          continue
      packed   = m.group("packed")
      rest     = m.group("rest")
      bus_width = parse_range(packed)
      # Parse each declarator: a, b[0:15], memA, memB[0:255], ...
      for token in rest.split(","):
        nm = name_re.match(token)
        if not nm:
          continue
        unpacked = nm.group("unpacked")
        array_len = parse_range(unpacked)
        total_bits += bus_width * array_len
    return total_bits


def bad_machine_simulation_fault_processing(df):
  groupedby_df = df.sort_values(1).groupby(1).agg(list).apply(lambda x: ', '.join([f"{fault} {net}" for fault, net in zip(x[0], x[2])]), axis=1)
  groupedby_df.index.name = None
  return groupedby_df

def get_fault_sim(bad_machine_files, module_name):
  fault_simulation = []
  for txt_file in bad_machine_files[module_name]['txt']:
    with open(txt_file, 'r') as fr:
      lines = fr.readlines()
      stats = lines[5].split()
      fault_simulation.append({'report': f"{lines[0].strip()} Simulated {stats[0]} pattern, detecting {stats[1]} out of {stats[2]} faults, achieving {stats[3]} test coverage"})
  return pd.DataFrame(fault_simulation)

def list_files(directory):
  for entry in os.scandir(directory):
    if entry.is_file():
      yield Path(entry.path)  # Yield each file path
    elif entry.is_dir():
      yield from list_files(entry.path)  # Recursively yield from subdirectories

def process_verilog_file(verilog_file, output_folder_file_path, regex_patterns):
  regex_pattern_extraction, regex_remove_multi_wspaces, regex_instances, decl_re, name_re = regex_patterns

  # Get module name from the netlist
  module_name = verilog_file.stem
  module_dir = output_folder_file_path / module_name
  
  # Get verilog netlist content
  with open(verilog_file, 'r') as f:
    verilog_content = f.readlines()

  netlist_lines = []
  netlist_only_gates_lines = []
  for line in verilog_content:
    # Filter out comments, empty lines, and keywords (`input`, `output` and `wire`)
    if (
      line.isspace() 
      or '//' in line):
      continue
    line_strip = line.strip()
    netlist_lines.append(line_strip)
    if not (line_strip.startswith('wire') 
        or line_strip.startswith('input') 
        or line_strip.startswith('output')
        or line_strip.startswith('reg')):
      netlist_only_gates_lines.append(line_strip)

  # --- ATPG Summary ---
  atpg_path = module_dir / 'atpg.txt'
  atpg = ''
  if atpg_path.exists():
    with open(atpg_path, 'r') as file_data:
      atpg = []
      flag = False
      for line in file_data:
        line = regex_remove_multi_wspaces.sub(' ', line.strip())
        if 'Summary Report' in line:
          flag=True
        elif flag == True and '---' not in line:
          atpg.append(line.strip())
      atpg = '\n'.join(atpg)

  # --- Faults ---
  faults_path = module_dir / 'faults.txt'
  faults = ''
  if faults_path.exists():
    try:
      tmp_df = pd.read_csv(faults_path, sep='\s+', names=['fault_type', 'status', 'net', 'bit'], header=None)
      tmp_df['net'] = tmp_df['net'] + tmp_df['bit'].fillna('').astype(str)
      faults_df = tmp_df[['fault_type', 'status', 'net']]
      faults = faults_df.astype(str).apply(' '.join, axis=1).str.cat(sep='\n')
    except:
      pass

  # --- Patterns ---
  patterns_path = module_dir / 'patterns.txt'
  patterns = ''
  if patterns_path.exists():
    with open(patterns_path, 'r') as f:
      file_content = f.read()

    try:
      _patterns = []
      matches = regex_pattern_extraction.findall(file_content)
      for pi_block, po_block in matches:
        pi = ''.join(pi_block.split())
        po = ''.join(po_block.split())
        _patterns.append([pi, po])

      if _patterns:
        patterns = pd.DataFrame.from_records(_patterns).to_string()
    except:
      pass

  # Collect Data 
  result = {
    'module_name': module_name, 
    'netlist_only_gates': '\n'.join(netlist_only_gates_lines), 
    'patterns': patterns,
    'faults': faults, 
    'atpg': atpg, 
    'netlist': '\n'.join(netlist_lines), 
    'num_instances': len([line for line in netlist_lines if 'module' not in line and regex_instances.match(line)]),
    'num_wires': count_bits_for_keyword('\n'.join(netlist_lines), "wire", decl_re, name_re),
    'num_inputs': count_bits_for_keyword('\n'.join(netlist_lines), "input", decl_re, name_re),
    'num_outputs': count_bits_for_keyword('\n'.join(netlist_lines), "output", decl_re, name_re),
  }
  return result

if __name__ == '__main__':
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
      (?P<name>[A-Za-z_]\w*)                       # signal name
      \s*
      (?P<unpacked>\[\s*\d+\s*:\s*\d+\s*\])?       # optional unpacked dimension
      \s*$
  """, re.VERBOSE)

  parser = argparse.ArgumentParser(description='Random Circuit Generator')
  parser.add_argument('-cf', '--circuit_folder', type=Path)
  parser.add_argument('-of', '--output_folder', type=Path)
  parser.add_argument('-odf', '--out_data_file', type=Path)
  parser.add_argument('-cp', '--cpu_pool', type=int, default=mp.cpu_count())
  args = parser.parse_args()

  circuit_folder_file_path = args.circuit_folder
  output_folder_file_path  = args.output_folder
  # Create Pool of CPU cores, each function task per process
  cpu_pool = args.cpu_pool

  regex_pattern_extraction = re.compile(r"force_all_pis\s*=\s*([01\s]+)Time\s+\d+:\s+measure_all_pos\s*=\s*([01\s]+)")
  regex_remove_multi_wspaces = re.compile(r'\s+')
  regex_instances = re.compile(r'\w+\s+\w+\s*\(')
  regex_wires = re.compile(r'wire\s+((?:\w+,\s*)*\w+);')
  regex_inputs = re.compile(r'input\s+((?:\w+,\s*)*\w+);')
  regex_outputs = re.compile(r'output\s+((?:\w+,\s*)*\w+);')
  regex_patterns = (regex_pattern_extraction, regex_remove_multi_wspaces, regex_instances, decl_re, name_re)

  print("Loading verilog files...")
  verilog_files = sorted(list_files(circuit_folder_file_path))

  # Create a partial function to pass fixed arguments
  process_func = partial(process_verilog_file, output_folder_file_path=output_folder_file_path, regex_patterns=regex_patterns)
  # Process verilog files, ATPG-extracted files, and good/bad machine simulation files
  with mp.Pool(processes=cpu_pool) as pool:
    results = list(tqdm(pool.imap(process_func, verilog_files), total=len(verilog_files), desc="Processing..."))
  
  # Combine results
  data = []
  for result in results:
    data.append({
      'module_name': result['module_name'],
      'netlist_only_gates': result['netlist_only_gates'],
      'patterns': result['patterns'],
      'faults': result['faults'],
      'atpg': result['atpg'],
      'netlist': result['netlist'],
      'num_instances': result['num_instances'],
      'num_wires': result['num_wires'],
      'num_inputs': result['num_inputs'],
      'num_outputs': result['num_outputs'],
    })

  data = pd.DataFrame(data)
  data.to_csv(args.out_data_file, sep=',', index=False)
