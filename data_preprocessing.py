from tqdm.auto import tqdm
from random import randint
import pandas as pd
import regex as re
import argparse
import os

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
      yield entry.path  # Yield each file path
    elif entry.is_dir():
      yield from list_files(entry.path)  # Recursively yield from subdirectories

parser = argparse.ArgumentParser(description='Random Circuit Generator')
parser.add_argument('-cf', '--circuit_folder', type=str) # default='circuits_random_pis')
parser.add_argument('-of', '--output_folder', type=str)  # default='output_random_pis')
parser.add_argument('-odf', '--out_data_file', type=str)     # default='atpg_data.csv')
parser.add_argument('-ovf', '--out_vocab_file', type=str)    # default='vocab.csv')
args = parser.parse_args()

circuit_folder_file_path = args.circuit_folder
output_folder_file_path  = args.output_folder

print(circuit_folder_file_path)
print(output_folder_file_path)

regex_tokenize_ones_zeros = re.compile(r'[0|1]')
regex_remove_multi_wspaces = re.compile(r'\s+')
regex_instances = re.compile(r'\w+\s+_\d+_\s*\(')
regex_wires = re.compile(r'wire\s+((?:_\d+_,\s*)*_\d+_);')
regex_inputs = re.compile(r'input\s+((?:_\d+_,\s*)*_\d+_);')
regex_outputs = re.compile(r'output\s+((?:_\d+_,\s*)*_\d+_);')

data = []
vocab = set()
atpg_extracted_files = 3 # files end with .txt (atpg.txt, patterns.txt, faults.txt)

print("Loading Verilog & ATPG files...")
# Loading Verilog & ATPG files
verilog_files = sorted([ f for f in list_files(circuit_folder_file_path)])
output_files = sorted([ f for f in list_files(output_folder_file_path) if not f.split('/')[-1].startswith('machine') and f.split('/')[-1].endswith('.txt') ])

coverage_types = ["fault"] # ["test", "fault"]
goal = {"fault": 8} # {"test": 7, "fault": 8} # the number shows the line that needs to be cropped.
messages = []

# Process verilog files, ATPG-extracted files, and good/bad machine simulation files
num_verilog_files = len(verilog_files)
for i in tqdm(range(num_verilog_files), desc="Processing..."):
  # Get verilog file
  verilog_file = verilog_files[i]
  # Get module name from the netlist
  module_name = verilog_file.split('/')[-1].split('.')[0]
  # Get verilog netlist content
  verilog_content = open(verilog_file, 'r').read().split('\n')
  # Remove comments and empty lines from the netlist
  netlist = []
  # Remove comments, empty and keywords (`input`, `output` and `wire`) lines
  netlist_only_gates = []
  for line in verilog_content:
    if '/*' not in line and '*' not in line and not line.isspace():
      line_strip = line.strip()
      netlist.append(line_strip)
      if not line_strip.startswith('wire') and not line_strip.startswith('input') and not line_strip.startswith('output'):
        netlist_only_gates.append(line_strip)
  netlist = '\n'.join(netlist)
  netlist_only_gates = '\n'.join(netlist_only_gates)
  vocab = vocab.union(set([word for word in netlist_only_gates.replace(';', ' ;').replace(',', ' ,').split() if not word.startswith('circuit')]))
  
  # ATPG
  with open(os.path.join(output_folder_file_path, module_name, 'atpg.txt'), 'r') as file_data:
    atpg = []
    flag = False
    for line in file_data:
      line = regex_remove_multi_wspaces.sub(' ', line.strip())
      if 'Summary Report' in line:
        flag=True
      elif flag == True and '---' not in line:
        atpg.append(line.strip())
    atpg = '\n'.join(atpg)

  # Faults
  faults_df = pd.read_csv(os.path.join(output_folder_file_path, module_name, 'faults.txt'), sep='\s+', header=None)
  faults = faults_df.astype(str).apply(' '.join, axis=1).str.cat(sep='\n')

  # Patterns
  with open(os.path.join(output_folder_file_path, module_name, 'patterns.txt'), 'r') as file_data:
    patterns = [ f"{' '.join(regex_tokenize_ones_zeros.findall(line.split('=')[-1].strip()))}".strip() for line in file_data if '_pis' in line or '_pos' in line ]
    _patterns = []
    for i, (test_vector, expected_output) in enumerate(zip(patterns[0::2], patterns[1::2])):
      _patterns.append({'Input Test Vector': test_vector, 'Expected Output': expected_output})
    patterns = pd.DataFrame(_patterns)  

  # The next 3 lines will be usefull to be calculated but cannot be stored in csv as nested DataFrames
  # bad_machine_faults = pd.concat([bad_machine_simulation_fault_processing(pd.read_csv(csv_file, sep=r'\s+', header=None)) for csv_file in bad_machine_files[module_name]['csv']], axis=1).T
  # fault_simulation = get_fault_sim(bad_machine_files, module_name)
  # bad_machine = pd.DataFrame.from_dict({'simulation': [fault_simulation], 'type': [bad_machine_faults]})

  # Collect Data 
  data.append({'module_name': module_name, 'netlist_only_gates': netlist_only_gates, 'patterns': patterns, 'faults': faults, 'atpg': atpg, 'netlist': netlist, 'num_instances': len(regex_instances.findall(netlist_only_gates)), 'num_wires': sum([len(wire.split(',')) for wire in regex_wires.findall(netlist)]), 'num_inputs': sum([len(inputs.split(',')) for inputs in regex_inputs.findall(netlist)]), 'num_outputs': sum([len(outputs.split(',')) for outputs in regex_outputs.findall(netlist)])})


data = pd.DataFrame(data)
data.to_csv(args.out_data_file, sep=',', index=False)

if args.out_vocab_file:
  vocab = pd.DataFrame(vocab)
  vocab.to_csv(args.out_vocab_file, sep=',', index=False)
