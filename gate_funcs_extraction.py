from pathlib import Path
import os
import regex as re
from sympy import symbols
from sympy.parsing.sympy_parser import parse_expr
from sympy.core.symbol import Symbol

# LIBRARY: asap7sc7p5t_28
LIBRARY = os.environ["LIBRARY"]
# PVT_CORNER: TT / SS / FF
PVT_CORNER = os.environ["PVT_CORNER"]
# LIB_VARIANT: RVT / LVT / SLVT / SRAM
LIB_VARIANT = os.environ["LIB_VARIANT"]

LIB_DIR = Path(f"lib/{LIBRARY}/LIB/CCS/").resolve()
CATEGORIES = ["AO", "OA", "INVBUF", "SEQ", "SIMPLE"]

# Helper function
def best_match(files, cat, VARIANT, PVT):
  # Compile a category-specific regex once
  pat = re.compile(
      rf"{re.escape(cat)}_{re.escape(VARIANT)}_{re.escape(PVT)}(?:_(ccs(?![an])|ccsa|ccsn))?",
      re.IGNORECASE
  )

  # Score: lower is better
  def score(suffix):
      # suffix is 'ccs', 'ccsa', 'ccsn', or None (base only)
      if suffix == "ccs":     return (0, )
      if suffix is None:      return (1, )
      if suffix in ("ccsa","ccsn"):
          # Prefer ccsa over ccsn only if you need a tie-break
          return (2, 0 if suffix == "ccsa" else 1)
      return (3, )

  candidates = []
  for f in files:
      m = pat.search(f.name)
      if m:
          sfx = m.group(1)
          # Normalize 'ccs' captured inside '(ccs(?![an])|...)'
          if sfx and sfx.startswith("ccs") and sfx not in ("ccs", "ccsa", "ccsn"):
              sfx = "ccs"
          candidates.append((score(sfx), f))

  if not candidates:
      return None
  return str(min(candidates, key=lambda t: t[0])[1])
  
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

# Regex to capture symbols inside the function
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