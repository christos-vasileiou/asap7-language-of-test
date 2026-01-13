#!/usr/bin/env python
from functools import partial
from tqdm import tqdm
from pathlib import Path
import multiprocessing as mp
import regex as re
import pandas as pd
import argparse
import shutil

def preprocess_data(nets_mapping, data):
  for key, value in nets_mapping.items():
    data = data.replace(key, value)
  # Parse manually to handle net names with spaces (e.g., "\div_6/u_div/BInv [24]")
  rows = []
  for line in data.strip().split('\n'):
    parts = line.split(None, 2)  # Split on whitespace, max 2 splits → 3 fields
    if len(parts) == 3:
      rows.append(parts)
  df = pd.DataFrame(rows)
  df = df.drop_duplicates()
  return df

def process_faults(verilog_file, tetramax_folder, nets_re):
  nets_mapping = {}
  with open(verilog_file, 'r') as f:
    netlist = f.read()
    for m in nets_re.finditer(netlist):
      cell = m.group('cell')
      inst = m.group('inst')
      pins = dict(re.findall(r"\.(\w+)\s*\(\s*([^()]*?)\s*\)", m.group("pins")))
      nets_mapping.update({f"{inst}/{k}": v for k, v in pins.items()})
  
  module_name = verilog_file.stem
  work_dir = tetramax_folder / module_name
  
  # Check if work_dir exists and has the expected number of items
  if not work_dir.exists():
    return None
  
  if not work_dir.is_dir():
    return None
  
  if len(list(work_dir.iterdir())) != 6: # 6 items: drc.txt, atpg.txt, patterns.txt, faults.txt, simulation.stil, simulation
    shutil.rmtree(work_dir)
    return None
  
  # Process faults.txt
  faults_file = work_dir / 'faults.txt'
  if faults_file.exists():
    with open(faults_file, 'r+') as fr:
      data = fr.read()
      df = preprocess_data(nets_mapping, data)
      fr.seek(0)
      fr.write(' '+df.astype(str).apply('   '.join, axis=1).str.cat(sep='\n '))
      fr.truncate()

  # Process bad machine faults
  bad_dir = work_dir / 'simulation/bad'
  if bad_dir.exists() and bad_dir.is_dir():
    for bad_machine_faults in sorted(bad_dir.glob('*.csv')):
      with open(bad_machine_faults, 'r+') as fr:
        data = fr.read()
        df = preprocess_data(nets_mapping, data)
        fr.seek(0)
        fr.write(' '+df.astype(str).apply('   '.join, axis=1).str.cat(sep='\n '))
        fr.truncate()

NETS_RE = r"""
^ \s*
(?P<cell>\w+)              # cell type, e.g., MAJIxp5_ASAP7_75t_R
\s+
(?P<inst>\w+)              # instance name, e.g., U9
\s* \(
\s*
(?P<pins>                  # entire named-pin list
    \.\w+\s*\(\s*[^()]*\s*\)            # .PIN(expr)
    (?: \s*,\s* \.\w+\s*\(\s*[^()]*\s*\) )*   # , .PIN(expr) ...
)
\s* \)
\s* ;
\s*$"""

if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Random Circuit Generator')
  parser.add_argument('-cf', '--circuit_folder', type=str)
  parser.add_argument('-of', '--output_folder', type=str)
  parser.add_argument('-sp', '--starting_point', type=int, default=0)
  args = parser.parse_args()

  circuits_folder = Path(args.circuit_folder)
  tetramax_folder = Path(args.output_folder)
  start_idx = args.starting_point

  nets_re = re.compile(NETS_RE, re.VERBOSE | re.MULTILINE)
  verilog_files = sorted([file for file in circuits_folder.glob('*.v')])[start_idx:]

  process_func = partial(process_faults, tetramax_folder=tetramax_folder, nets_re=nets_re)
  
  cpu_pool = mp.cpu_count()
  with mp.Pool(processes=cpu_pool) as pool:
    list(tqdm(pool.imap(process_func, verilog_files), total=len(verilog_files), desc="Processing..."))

