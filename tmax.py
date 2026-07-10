"""
Drive Synopsys TetraMAX on design netlists with explicit separation of:

  * Design Verilog: ``--verilog-files`` (directory of DUT .v netlists).
  * Cell library Verilog: ``CELL_LIBS_VERILOG`` (structural stdcell .v for read_netlist),
    or auto-resolved ASAP7 paths if unset.
  * Liberty timing libs: ``CELL_LIBS_LIBERTY`` (space-separated .lib); not read by
    TetraMAX but passed through in the environment for Tcl (sequential cell-name scan
    prefers *SEQ* here) and for other tools in your flow.

Legacy: ``LIBS`` is still set to the same string as ``CELL_LIBS_VERILOG`` for older wrappers.
"""

import os
import sys
import argparse
import shutil
import subprocess
from pathlib import Path


def split_env_path_list(s: str) -> list[str]:
    return [p for p in (x.strip() for x in s.split()) if p]


def _lib_paths_asap7_liberty(
    lib_dir: Path, categories: list[str], variant: str, pvt: str
) -> list[Path]:
    """Glob Liberty CCS files: asap7sc7p5t_{CAT}_{VARIANT}_{PVT}_*.lib"""
    paths: list[Path] = []
    for cat in categories:
        pattern = f"asap7sc7p5t_{cat}_{variant}_{pvt}_*.lib"
        matches = sorted(lib_dir.glob(pattern))
        if not matches:
            raise FileNotFoundError(
                f"No Liberty library matching {pattern!r} under {lib_dir}"
            )
        paths.append(matches[-1] if len(matches) > 1 else matches[0])
    return paths


def resolve_liberty_paths(data_preprocessing_dir: Path, use_asap7_28: bool) -> list[str]:
    """
    Return absolute paths to ASAP7 Liberty (.lib) libraries for SEQ cell-name scan.

    Used by TetraMAX Tcl (``CELL_LIBS_LIBERTY``); not passed to ``read_netlist``.
    """
    categories = ["AO", "OA", "INVBUF", "SEQ", "SIMPLE"]
    variant = os.environ.get("LIB_VARIANT", "RVT")
    pvt = os.environ.get("PVT_CORNER", "TT")

    if use_asap7_28:
        lib_dir = (data_preprocessing_dir / "lib" / "asap7sc7p5t_28" / "LIB" / "CCS").resolve()
        paths = _lib_paths_asap7_liberty(lib_dir, categories, variant, pvt)
    else:
        lib_dir = (data_preprocessing_dir / "lib" / "asap7sc7p5t_24" / "LIB" / "CCS").resolve()
        paths = _lib_paths_asap7_liberty(lib_dir, categories, variant, pvt)

    missing = [p for p in paths if not p.is_file()]
    if missing:
        names = ", ".join(p.name for p in missing)
        raise FileNotFoundError(f"Missing Liberty file(s): {names}")

    return [str(p.resolve()) for p in paths]


def cell_liberty_paths_from_env_or_kit(
    data_preprocessing_dir: Path,
    use_asap7_28: bool,
) -> list[str]:
    """Liberty paths from ``CELL_LIBS_LIBERTY`` or bundled ASAP7 kit."""
    raw = os.environ.get("CELL_LIBS_LIBERTY", "").strip()
    if raw:
        paths = split_env_path_list(raw)
        missing = [p for p in paths if not Path(p).is_file()]
        if missing:
            raise FileNotFoundError(
                f"CELL_LIBS_LIBERTY entries not found: {missing!r}"
            )
        return [str(Path(p).resolve()) for p in paths]
    return resolve_liberty_paths(data_preprocessing_dir, use_asap7_28)


def build_env(
    verilog_file: str,
    output_dir: Path,
    cell_libs_verilog: str,
    *,
    cell_libs_liberty: str = "",
    stil_file: str = "",
    pattern_idx: int = 0,
) -> dict:
    """Build env for TetraMAX Tcl drivers. Copies the parent environment."""
    env = os.environ.copy()
    env["VERILOG_FILE"] = verilog_file
    env["OUTPUT_DIR"] = str(output_dir.resolve())
    env["CELL_LIBS_VERILOG"] = cell_libs_verilog
    env["LIBS"] = cell_libs_verilog
    if cell_libs_liberty:
        env["CELL_LIBS_LIBERTY"] = cell_libs_liberty
    if stil_file:
        env["STIL_FILE"] = stil_file
    env["PATTERN_IDX"] = str(int(pattern_idx))
    return env


def infer_tmax_binary() -> str:
    """Return the TetraMAX binary to use, preferring an explicit path or env var."""
    env_bin = os.environ.get("TMAX_BIN")
    if env_bin:
        return env_bin
    return "tmax"


def _lib_paths_asap7_24(lib_dir: Path, categories: list[str], variant: str, pvt: str) -> list[Path]:
    """Fixed filenames: asap7sc7p5t_24_{CAT}_{VARIANT}_{PVT}.v"""
    paths: list[Path] = []
    for cat in categories:
        paths.append(lib_dir / f"asap7sc7p5t_24_{cat}_{variant}_{pvt}.v")
    return paths


def _lib_paths_asap7_28(lib_dir: Path, categories: list[str], variant: str, pvt: str) -> list[Path]:
    """Glob asap7sc7p5t_{CAT}_{VARIANT}_{PVT}_*.v (revision date differs, e.g. SEQ vs AO)."""
    paths: list[Path] = []
    for cat in categories:
        pattern = f"asap7sc7p5t_{cat}_{variant}_{pvt}_*.v"
        matches = sorted(lib_dir.glob(pattern))
        if not matches:
            raise FileNotFoundError(
                f"No structural Verilog library matching {pattern!r} under {lib_dir}"
            )
        if len(matches) > 1:
            chosen = matches[-1]
            print(
                f"Warning: multiple matches for {pattern}; using {chosen.name}",
                file=sys.stderr,
            )
        else:
            chosen = matches[0]
        paths.append(chosen)
    return paths


def resolve_structural_lib_paths(data_preprocessing_dir: Path, use_asap7_28: bool) -> list[str]:
    """
    Return absolute paths to ASAP7 structural cell Verilog libraries for TetraMAX.

    Default kit: lib/asap7sc7p5t_28/verilog. If use_asap7_28 is False: lib/asap7sc7p5t_24/verilog.
    """
    categories = ["AO", "OA", "INVBUF", "SEQ", "SIMPLE"]
    variant = os.environ.get("LIB_VARIANT", "RVT")
    pvt = os.environ.get("PVT_CORNER", "TT")

    if use_asap7_28:
        lib_dir = (data_preprocessing_dir / "lib" / "asap7sc7p5t_28" / "verilog").resolve()
        paths = _lib_paths_asap7_28(lib_dir, categories, variant, pvt)
    else:
        lib_dir = (data_preprocessing_dir / "lib" / "asap7sc7p5t_24" / "verilog").resolve()
        paths = _lib_paths_asap7_24(lib_dir, categories, variant, pvt)

    missing = [p for p in paths if not p.is_file()]
    if missing:
        names = ", ".join(p.name for p in missing)
        raise FileNotFoundError(f"Missing library file(s): {names}")

    return [str(p.resolve()) for p in paths]


def cell_verilog_paths_from_env_or_kit(
    data_preprocessing_dir: Path,
    use_asap7_28: bool,
) -> list[str]:
    """
    Structural cell-library .v paths for read_netlist.

    If ``CELL_LIBS_VERILOG`` is set in the environment, use it (space-separated paths).
    Otherwise resolve the bundled ASAP7 kit (28-track by default, or 24 with CLI flag).
    """
    raw = os.environ.get("CELL_LIBS_VERILOG", "").strip()
    if raw:
        paths = split_env_path_list(raw)
        missing = [p for p in paths if not Path(p).is_file()]
        if missing:
            raise FileNotFoundError(
                f"CELL_LIBS_VERILOG entries not found or not files: {missing!r}"
            )
        non_v = [p for p in paths if not p.lower().endswith(".v")]
        if non_v:
            print(
                "Warning: CELL_LIBS_VERILOG is for structural cell Verilog (.v); "
                f"non-.v entries: {non_v}",
                file=sys.stderr,
            )
        return [str(Path(p).resolve()) for p in paths]
    return resolve_structural_lib_paths(data_preprocessing_dir, use_asap7_28)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run Synopsys TetraMAX ATPG on a single Verilog netlist via tmax.tcl",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    default_tcl = Path(__file__).parent / "scripts" / "tmax.tcl"
    data_preprocessing_dir = Path(__file__).parent.resolve()

    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where per-design ATPG outputs will be written",
    )
    parser.add_argument(
        "--verilog-files",
        required=True,
        help="Directory of design gate-level Verilog netlists (*.v), not PDK cell libs",
    )
    parser.add_argument(
        "--tcl-script",
        default=str(default_tcl.resolve()),
        help="Path to run_tmax.tcl",
    )
    parser.add_argument(
        "--asap7-24-verilog",
        action="store_true",
        help=(
            "When CELL_LIBS_VERILOG is unset, use legacy 24-track libs under "
            "lib/asap7sc7p5t_24/verilog/. Default auto-kit: lib/asap7sc7p5t_28/verilog."
        ),
    )

    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    tcl_script = Path(args.tcl_script)
    verilog_files = Path(args.verilog_files).resolve().glob("*.v")

    if not tcl_script.exists():
        print(f"Error: TCL script not found: {tcl_script}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        lib_list = cell_verilog_paths_from_env_or_kit(
            data_preprocessing_dir,
            use_asap7_28=not args.asap7_24_verilog,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    cell_v = " ".join(lib_list)
    liberty_raw = os.environ.get("CELL_LIBS_LIBERTY", "").strip()

    for verilog_file in verilog_files:
        env = build_env(str(verilog_file), output_dir, cell_v)

        tmax_bin = infer_tmax_binary()
        if shutil.which(tmax_bin) is None:
            print(
                f"Warning: Could not locate '{tmax_bin}' in PATH. Continuing to attempt execution...",
                file=sys.stderr,
            )

        cmd = [tmax_bin, "-shell", "-tcl", str(tcl_script.resolve())]
        if os.environ.get("CELL_LIBS_VERILOG", "").strip():
            kit_note = "CELL_LIBS_VERILOG (from environment)"
        else:
            kit_note = "asap7sc7p5t_24" if args.asap7_24_verilog else "asap7sc7p5t_28 (default kit)"
        print(
            "Launching TetraMAX with:\n",
            " ".join(cmd),
            "\n",
            "VERILOG_FILE (design)=",
            env["VERILOG_FILE"],
            "\n",
            "OUTPUT_DIR =",
            env["OUTPUT_DIR"],
            "\n",
            f"cell_library_verilog ({kit_note})=",
            cell_v,
        )
        if liberty_raw:
            print("CELL_LIBS_LIBERTY=", liberty_raw)

        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            print(f"TetraMAX failed with return code {exc.returncode}", file=sys.stderr)
            return exc.returncode or 1
        except FileNotFoundError:
            print(f"Error: TetraMAX binary not found: {tmax_bin}", file=sys.stderr)
            return 127

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
