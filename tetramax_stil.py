"""Minimal STIL writers for TetraMAX vector fault simulation."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence


def _stil_quote(name: str) -> str:
  """Quote a Verilog net/port name for STIL."""
  escaped = name.replace("\\", "\\\\").replace('"', '\\"')
  return f'"{escaped}"'


def pi_bit_string(pi_order: Sequence[str], values: Dict[str, int]) -> str:
  """Concatenate 0/1 bits in ``pi_order`` (MSB-first within the group string)."""
  missing = [n for n in pi_order if n not in values]
  if missing:
    raise KeyError(f"input_vector missing PI(s): {missing[:5]}")
  return "".join(str(int(values[n])) for n in pi_order)


def write_vector_stil(
  path: Path,
  pi_order: Sequence[str],
  values: Dict[str, int],
) -> None:
  """
  Write a single-pattern combinational STIL file for ``set_patterns -external``.

  Uses Verilog PI names in the ``Signals`` block (TetraMAX maps them on DRC).
  """
  bits = pi_bit_string(pi_order, values)
  sig_lines = "\n".join(f"   {_stil_quote(n)} In;" for n in pi_order)
  pi_group = " + ".join(_stil_quote(n) for n in pi_order)
  text = f"""STIL 1.0 {{ Design 2005; }}
Header {{
   Title "transformers_atpg vector fault sim";
}}
Signals {{
{sig_lines}
}}
SignalGroups {{
   "_pi" = '{pi_group}';
}}
Timing {{
   WaveformTable "_default_WFT_" {{
      Period '100ns';
      Waveforms {{
         "_pi" {{ 0 {{ '0ns' D; }} }}
         "_pi" {{ 1 {{ '0ns' U; }} }}
      }}
   }}
}}
PatternBurst "_burst_" {{
   PatList {{ "_pattern_" {{ }} }}
}}
PatternExec {{
   PatternBurst "_burst_";
}}
Pattern "_pattern_" {{
   W "_default_WFT_";
   "pattern 0": V {{ "_pi"={bits}; }}
}}
"""
  path = Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(text, encoding="utf-8")
