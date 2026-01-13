#!/usr/bin/env python
import os
import sys
import argparse
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import List
from pathlib import Path
from utils import best_match


def build_env(verilog_file: str, output_dir: str, LIBS: str) -> dict:
    """Prepare environment variables for the TetraMAX TCL script (single file)."""
    env = os.environ.copy()
    env["VERILOG_FILE"] = verilog_file
    env["OUTPUT_DIR"] = output_dir
    env["LIBS"] = LIBS
    return env


def infer_tmax_binary() -> str:
    """Return the TetraMAX binary to use, preferring an explicit path or env var."""
    env_bin = os.environ.get("TMAX_BIN")
    if env_bin:
        return env_bin
    return "tmax"


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run Synopsys TetraMAX ATPG on a single Verilog netlist via tmax.tcl",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    default_tcl = Path(__file__).parent / "scripts" / "tmax.tcl"

    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where per-design ATPG outputs will be written",
    )
    parser.add_argument(
        "--verilog-files",
        required=True,
        help="Path to Verilog files",
    )
    parser.add_argument(
        "--tcl-script",
        default=str(default_tcl.resolve()),
        help="Path to tmax.tcl",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=os.cpu_count() or 1,
        help="Number of parallel TetraMAX invocations to allow",
    )

    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    tcl_script = Path(args.tcl_script)
    verilog_root = Path(args.verilog_files).resolve()
    verilog_files = sorted(verilog_root.glob("*.v"))

    # Validate inputs
    if not tcl_script.exists():
        print(f"Error: TCL script not found: {tcl_script}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)

    # Build list of technology libraries automatically from env settings
    LIBRARY = os.environ.get("LIBRARY", "asap7sc7p5t_28")
    LIB_DIR = Path(f"lib/{LIBRARY}/Verilog").resolve()
    CATEGORIES = ["AO", "OA", "INVBUF", "SEQ", "SIMPLE"]
    VARIANT    = os.environ.get("LIB_VARIANT", "RVT")   # LVT / RVT / SLVT / SRAM
    PVT        = os.environ.get("PVT_CORNER", "TT")      # TT / SS / FF

    _LIB_LIST = []
    for cat in CATEGORIES:
        # Gather all .v files that contain the triplet somewhere
        cat_files = list(LIB_DIR.glob(f"*{cat}_{VARIANT}_{PVT}*.v"))
        if not cat_files:
            continue
        pick = best_match(cat_files, cat, VARIANT, PVT)
        if pick:
            _LIB_LIST.append(pick)

    LIBS = " ".join(_LIB_LIST)

    if not verilog_files:
        print(f"No Verilog files found in {verilog_root}", file=sys.stderr)
        return 1

    max_workers = max(1, min(args.jobs, len(verilog_files)))

    tmax_bin = infer_tmax_binary()
    if shutil.which(tmax_bin) is None:
        print(
            f"Warning: Could not locate '{tmax_bin}' in PATH. Continuing to attempt execution...",
            file=sys.stderr,
        )

    tcl_path = str(tcl_script.resolve())

    def run_tmax_for_file(verilog_file: Path) -> None:
        # Build environment
        env = build_env(str(verilog_file), str(output_dir), LIBS)

        # Construct command
        # Using -shell and -tcl to run the script non-interactively. The script ends with 'quit'.
        cmd = [tmax_bin, "-shell", "-tcl", tcl_path]
        print(
            "Launching TetraMAX with:\n",
            " ".join(cmd),
            "\n",
            "VERILOG_FILE=",
            env["VERILOG_FILE"],
            "\n",
            "OUTPUT_DIR =",
            env["OUTPUT_DIR"],
        )

        # Run TetraMAX
        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            print(f"TetraMAX failed with return code {exc.returncode}", file=sys.stderr)
        except FileNotFoundError:
            print(f"Error: TetraMAX binary not found: {tmax_bin}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for _ in executor.map(run_tmax_for_file, verilog_files):
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

