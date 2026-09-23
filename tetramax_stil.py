"""Single combinational capture in the TetraMAX O-2018 unified STIL dialect."""
from __future__ import annotations
from pathlib import Path
from typing import Mapping, Sequence


def _stil_quote(name: str) -> str:
    if any(c in name for c in "\n\r\x00"):
        raise ValueError("Invalid STIL signal name")
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pi_bit_string(pi_order: Sequence[str], values: Mapping[str, int]) -> str:
    if len(set(pi_order)) != len(pi_order) or set(values) != set(pi_order):
        raise ValueError("Input vector must contain exactly the canonical PIs")
    if any(type(v) is not int or v not in (0, 1) for v in values.values()):
        raise ValueError("Input vector must be binary integers")
    return "".join(str(values[n]) for n in pi_order)


def write_vector_stil(path: Path, pi_order: Sequence[str], values: Mapping[str, int],
                      po_order: Sequence[str], outputs: Mapping[str, int | str] | None = None) -> None:
    """Write an external capture without scan cells or ATPG-generated inputs.

    Initial H output placeholders are independent of model predictions. A good
    simulation supplies actual measures before fault grading; X masks unknown POs.
    """
    bits = pi_bit_string(pi_order, values)
    if not po_order or len(set(po_order)) != len(po_order) or set(pi_order) & set(po_order):
        raise ValueError("Expected distinct canonical PI and PO names")
    if outputs is not None and set(outputs) != set(po_order):
        raise ValueError("Output measures must contain exactly the canonical POs")
    wave = {0: "L", 1: "H", "x": "X", "X": "X", "z": "T", "Z": "T"}
    measures = "H" * len(po_order) if outputs is None else "".join(wave[outputs[n]] for n in po_order)
    signals = "\n".join(f"  {_stil_quote(n)} {direction};"
                        for names, direction in [(pi_order, "In"), (po_order, "Out")] for n in names)
    groups = "\n".join(f'  "{group}" = \'{" + ".join(_stil_quote(n) for n in names)}\';'
                       for group, names in [("_pi", pi_order), ("_po", po_order)])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'''STIL 1.0 {{ Design 2005; }}
Signals {{
{signals}
}}
SignalGroups {{
{groups}
}}
Timing {{ WaveformTable "w" {{ Period '100ns'; Waveforms {{
  "_pi" {{ 0 {{ '0ns' D; }} }}
  "_pi" {{ 1 {{ '0ns' U; }} }}
  "_pi" {{ N {{ '0ns' N; }} }}
  "_pi" {{ Z {{ '0ns' Z; }} }}
  "_po" {{ X {{ '0ns' X; }} }}
  "_po" {{ H {{ '0ns' X; '40ns' H; }} }}
  "_po" {{ L {{ '0ns' X; '40ns' L; }} }}
  "_po" {{ T {{ '0ns' X; '40ns' T; }} }}
}} }} }}
ScanStructures {{ }}
PatternBurst "burst" {{ PatList {{ "pattern" {{ }} }} }}
PatternExec {{ PatternBurst "burst"; }}
Procedures {{ "capture" {{
  W "w";
  C {{ "_po"={"X" * len(po_order)}; }}
  "forcePI": V {{ "_pi"={"#" * len(pi_order)}; }}
  "measurePO": V {{ "_po"={"#" * len(po_order)}; }}
}} }}
MacroDefs {{ }}
Pattern "pattern" {{
  W "w";
  C {{ "_pi"={"0" * len(pi_order)}; "_po"={"X" * len(po_order)}; }}
  "pattern 0": Call "capture" {{ "_pi"={bits}; "_po"={measures}; }}
}}
''', encoding="utf-8")
