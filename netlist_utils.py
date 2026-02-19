"""
netlist_utils.py
----------------

Verilog netlist parsing and manipulation utilities.

Provides dataclasses for representing structural netlists and helper
functions for parsing port declarations, expanding bus ranges, and
verifying module names.
"""

import regex as re
from dataclasses import dataclass
from typing import Dict, List, Tuple


# ============================================================
# Data structures
# ============================================================

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


# ============================================================
# Verilog declaration helpers
# ============================================================

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


# ============================================================
# Module-name verification
# ============================================================

_MODULE_NAME_RE = re.compile(r'module\s+(\S+)\s*\(')


def verify_module_name(netlist: str, module_name: str) -> Tuple[str, bool]:
  """
  Parse the first line of *netlist* with ``module <name> (`` and compare with
  *module_name*.  Returns ``(correct_name, changed)`` where *changed* is True
  when the stored module_name did not match the netlist.
  """
  m = _MODULE_NAME_RE.search(netlist)
  if m is None:
    # Cannot parse → keep the original name
    return module_name, False
  parsed_name = m.group(1)
  if parsed_name == module_name:
    return module_name, False
  return parsed_name, True
