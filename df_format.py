"""
df_format.py
------------

DataFrame formatting utilities for converting pandas DataFrames
to compact JSON or Markdown representations.
"""

import pandas as pd


def df_to_json(df: pd.DataFrame) -> str:
    df.index = df.index.map(str.strip)
    df.columns = df.columns.map(str.strip)
    return df.to_json()


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
