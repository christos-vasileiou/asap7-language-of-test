#!/usr/bin/env python3
"""
Deprecated wrapper — use uniquify_netlists.py.

Preserves the historical CLI::

    copy_verified_files.py [input_base_dir] [output_dir] [num_workers]

``input_base_dir`` must be the dataset directory (…/data/<DATASET>/).
``output_dir`` must be named ``structural.<suffix>`` (as produced by the old defaults).
Forwards to uniquify_netlists with ``--require-verified`` (legacy behavior).
"""

from __future__ import annotations

import os
import sys
from multiprocessing import cpu_count
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent


def _main_shim() -> int:
    print(
        "copy_verified_files.py is deprecated — run data_preprocessing/uniquify_netlists.py",
        file=sys.stderr,
    )

    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    import uniquify_netlists  # noqa: E402  # after sys.path

    # Defaults match former get_config() + relative paths from data_preprocessing/
    config_dataset = os.environ.get("DATASET", "freeset")
    config_library = os.environ.get("LIBRARY", "asap7sc7p5t_28")
    config_lv = os.environ.get("LIB_VARIANT", "RVT").lower()
    config_pvt = os.environ.get("PVT_CORNER", "TT").lower()
    default_base = Path(f"../data/{config_dataset}")
    default_out = Path(
        f"../data/{config_dataset}/structural.v.{config_dataset}."
        f"{config_library}.{config_lv}.{config_pvt}"
    )
    default_workers = int(os.environ.get("NUM_WORKERS", cpu_count()))

    argv = sys.argv[1:]
    if len(argv) > 3:
        print("Usage: copy_verified_files.py [input_base_dir] [output_dir] [num_workers]", file=sys.stderr)
        return 2

    input_base = Path(argv[0] if len(argv) > 0 else str(default_base)).resolve()
    output_dir = Path(argv[1] if len(argv) > 1 else str(default_out)).resolve()
    num_workers = int(argv[2]) if len(argv) > 2 else default_workers

    if not input_base.is_dir():
        print(f"Error: input_base_dir is not a directory: {input_base}", file=sys.stderr)
        return 1

    data_root = input_base.parent
    dataset = input_base.name

    if not output_dir.name.startswith("structural."):
        print(
            f"Error: output_dir must be named structural.<suffix>, got: {output_dir.name}",
            file=sys.stderr,
        )
        return 1
    suffix = output_dir.name[len("structural.") :]

    old = sys.argv[:]
    try:
        sys.argv = [
            "uniquify_netlists.py",
            "--dataset",
            dataset,
            "--suffix",
            suffix,
            "--data-root",
            str(data_root),
            "--require-verified",
            "--num-workers",
            str(num_workers),
        ]
        return uniquify_netlists.main()
    finally:
        sys.argv = old


if __name__ == "__main__":
    raise SystemExit(_main_shim())
