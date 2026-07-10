#!/home/cxv200006/work/myenv/bin/python
import os
import subprocess
import signal
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed, wait, FIRST_COMPLETED
from pathlib import Path
import regex as re
from typing import Dict, Any, List, Optional, Set
import shutil
import json
import sys
import argparse

# Project paths anchored to this file so Slurm cwd / submit directory does not break
# lib/, scripts/, or ../data resolution.
_SYN_SCRIPT_DIR = Path(__file__).resolve().parent

# Set by SIGTERM/SIGINT (e.g. Slurm walltime sends SIGTERM before SIGKILL).
_shutdown_requested = threading.Event()


def _request_shutdown(signum: int, _frame) -> None:
    name = getattr(signal, "Signals", None)
    label = name(signum).name if name is not None else str(signum)
    print(f"\n⚠️  Received {label} – stopping new work (Slurm time limit or manual interrupt) …", flush=True)
    _shutdown_requested.set()


def _isolate_huggingface_cache_for_slurm() -> None:
    """Use a per-task Hugging Face cache under $SLURM_TMPDIR (or /tmp).

    Many parallel ``srun`` tasks calling ``load_dataset`` on the same
    ``~/.cache/huggingface`` hit ``filelock`` + ``O_EXCL`` races and raise
    ``FileExistsError`` on the ``*.lock`` path.  Setting ``HF_HOME`` once per
    Slurm task avoids that.  Child worker processes inherit this env.

    Override: set ``HF_HOME`` yourself, or ``SYN_HF_SHARED_CACHE=1`` to keep
    the default cache (not recommended for multi-task ``srun``).
    """
    if os.environ.get("SYN_HF_SHARED_CACHE", "").strip() in ("1", "true", "yes"):
        return
    if os.environ.get("HF_HOME"):
        return
    job_id = os.environ.get("SLURM_JOB_ID")
    if not job_id:
        return
    proc = os.environ.get("SLURM_PROCID", "0")
    base = os.environ.get("SLURM_TMPDIR") or os.environ.get("TMPDIR") or "/tmp"
    user = os.environ.get("USER", "user")
    hf_home = str(Path(base) / user / "hf_syn_slurm" / f"{job_id}_{proc}")
    Path(hf_home).mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = hf_home
    print(f"🗄  HF_HOME={hf_home} (per Slurm task; avoids datasets lock contention on shared home cache)")


_isolate_huggingface_cache_for_slurm()

from datasets import load_dataset
from utils import best_match

# --------------------------------------------------------------------------------------
# Configuration – adjust these defaults to match your local Synopsys / technology setup
# --------------------------------------------------------------------------------------

# Path to the TCL script that drives Synopsys Design Compiler (next to this file)
TCL_SCRIPT_PATH = _SYN_SCRIPT_DIR / "scripts" / "syn.tcl"
FORMALITY_SCRIPT_PATH = _SYN_SCRIPT_DIR / "scripts" / "formality.tcl"

DC_TIMEOUT_SECONDS = 600       # 10 min per synthesis run
FM_TIMEOUT_SECONDS = 120       # 2 min per Formality verification
YOSYS_TIMEOUT_SECONDS = 60     # 1 min per Yosys precheck

# ------------------------------------------------------------------
# Build list of technology libraries automatically from env settings
# ------------------------------------------------------------------
LIBRARY = os.environ.get("LIBRARY", "asap7sc7p5t_28")
LIB_DIR = (_SYN_SCRIPT_DIR / "lib" / LIBRARY / "DB").resolve()
CATEGORIES = ["AO", "OA", "INVBUF", "SEQ", "SIMPLE"]
VARIANT   = os.environ.get("LIB_VARIANT", "RVT")   # LVT / RVT / SLVT / SRAM
PVT       = os.environ.get("PVT_CORNER", "TT")     # TT / SS / FF

_DB_LIST = []
for cat in CATEGORIES:
    # Gather all .db files that contain the triplet somewhere
    cat_files = list(LIB_DIR.glob(f"*{cat}_{VARIANT}_{PVT}*.db"))
    if not cat_files:
        continue
    pick = best_match(cat_files, cat, VARIANT, PVT)
    if pick:
        _DB_LIST.append(pick)

DEFAULT_DBS = " ".join(_DB_LIST)

# -----------------------------------------------------------------------------
# Command-line interface – lets Slurm job-array instances control their slice
# -----------------------------------------------------------------------------

parser = argparse.ArgumentParser(
    description="Run Synopsys Design Compiler on a slice of the FreeSet dataset",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)

parser.add_argument(
    "--workers",
    type=int,
    default=os.cpu_count(),
    help="Number of parallel processes to launch on this node",
)

parser.add_argument(
    "--stride",
    type=int,
    nargs="?",
    help="Total number of job-array tasks (the stride for dataset slicing)",
)

parser.add_argument(
    "--procid",
    type=int,
    nargs="?",
    help="Index of this job within the stride (0-based)",
)

args = parser.parse_args()

# Maximum parallel synthesis jobs to spawn on this node
MAX_WORKERS = int(args.workers) or (os.cpu_count() or 1)

# Which dataset indices belong to *this* Slurm task (--stride / --procid default for local runs)
_stride = 1 if args.stride is None else int(args.stride)
STRIDE = max(_stride, 1)
_procid = 0 if args.procid is None else int(args.procid)
PROCID = _procid % STRIDE

# --------------------------------------------------------------------------------------
# Dataset loading (streaming mode keeps memory footprint low)
# --------------------------------------------------------------------------------------

print("📦 Loading FreeSet dataset … (streaming mode)")
dataset_dict = {"freeset": "SETH-TAMU/FreeSet-V1.0-LabUse",
                "metrex": "scale-lab/MetRex",
                "shailja": "shailja/Verilog_GitHub"}

DATASET   = os.environ.get("DATASET", "freeset")   # freeset / metrex / shailja
ds = load_dataset(dataset_dict[DATASET], split="train", streaming=True)

# Directory where we cache the generated RTL source files and synthesis outputs.
# OUTPUT_ROOT is per syn.py *rank* (e.g. one Slurm task per node → work_0 … work_{STRIDE-1}).
# All ProcessPoolExecutor workers in that process share this same OUTPUT_ROOT.
OUTPUT_PATH = (_SYN_SCRIPT_DIR.parent / "data" / DATASET).resolve()
OUTPUT_ROOT = OUTPUT_PATH / f"work_{PROCID}"
OUTPUT_ROOT.mkdir(exist_ok=True, parents=True)

print(f"Rank config → STRIDE={STRIDE} PROCID={PROCID} OUTPUT_ROOT={OUTPUT_ROOT}")

# --------------------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------------------

NOT_SYNTHESIZABLE_VERILOG_CODE_PATTERN = r"\$(display|monitor|strobe|write|fdisplay|fwrite|fmonitor|fopen|fclose|fgetc|fgets|fscanf|sscanf|readmemb|readmemh|stop|finish|time|stime|realtime|random|urandom|urandom_range|dumpfile|dumpvars|dumpon|dumpoff|dumplimit|setup|hold|width|period|recovery|removal|skew|timeskew|assert|fatal|error|warning|info|itor|rtoi|bitstoreal|realtobits|cast|typename|countdrivers|coverage_control|sample|async|synchronous) \
                     |\b(initial|forever|wait|fork|join|disable|force|release|specify|endspecify|timeunit|timeprecision|realtime|event|assert|assume|cover|property|sequence|randcase|randsequence|randomize|constraint|program|endprogram)\b"

def _dump_rtl_files(verilog_code: str, example_id: int) -> Path:
    """Write the Verilog/SystemVerilog/VHDL contained in *example* to disk.
    Returns the directory path that now contains the RTL files.
    """
    example_dir = OUTPUT_ROOT / f"example_{example_id:06d}"
    example_dir.mkdir(parents=True, exist_ok=True)
    (example_dir / "design.v").write_text(verilog_code)
    return example_dir


def find_top_module_name(verilog_code: str) -> str:
    """
    Recognize argument input and outputs and return the top module name.
    Accepts the example dict, checks for 'verilog', 'code', or 'text' fields.
    """
    if not verilog_code:
        return None
    # This regex matches the names of Verilog modules, including those with optional parameter blocks:
    # - r"module\s+(\w+)" matches the keyword 'module', followed by whitespace, then captures the module name.
    # - "\s*" allows optional whitespace after the name.
    # - "(?:#\s*\((?:[^()]|\([^()]*\))*\))?" optionally matches the parameter declaration (#(...)), handling nested parens.
    # - "\s*\(" matches optional whitespace, then the opening parenthesis for the port list.
    modules = re.findall(
        r"module\s+(\w+)\s*(?:#\s*\((?:[^()]|\([^()]*\))*\))?\s*\(",
        verilog_code,
    )
    r_strings = [f"{module}\s+(\w+)\s*(?:#\s*\((?:[^()]|\([^()]*\))*\))?\s*\(" for module in modules]
    includes = [len(re.findall(r, verilog_code)) for r in r_strings]
    if 0 not in includes:
        return None
    return modules[includes.index(0)]


def _prepare_env(rtl_path: Path, top_name: Optional[str] = None, 
                 synthesized_file: Optional[Path] = None, json_file: Optional[Path] = None) -> Dict[str, str]:
    """Build the environment for Design Compiler or Formality by extending the current env.
    Args:
        rtl_path: Path to the RTL directory (used for reports/results directories and RTL file location)
        top_name: Optional top-level module name (sets DESIGN variable)
        synthesized_file: Optional path to synthesized Verilog file (for Formality - sets VERILOG_FILE)
        json_file: Optional path to JSON file (for Formality - sets JSON_FILE)
    The DESIGN variable is only set when *top_name* is provided. This makes
    it optional – the TCL script can now auto-detect the top-level module when
    the variable is absent.
    The RTL file is always assumed to be at rtl_path/design.v, so RTL_PATH
    is used by both syn.tcl (to glob for files) and formality.tcl (to construct
    the RTL file path).
    When verilog_file is provided, Formality-specific variable VERILOG_FILE is set.
    """
    env = os.environ.copy()
    env.setdefault("DBS", DEFAULT_DBS)
    env.setdefault("REPORTS_DIR", str(rtl_path / "reports"))
    env.setdefault("RESULTS_DIR", str(rtl_path / "results"))
    # Always provide the RTL path & basic synthesis settings
    updates = {
        # Timing DB list already present in env (set above) – do not overwrite here.
        "RTL_PATH": str(rtl_path.resolve()),
        # Default to plain Verilog unless the caller overrides via env var
        "SYN_LANGUAGE": env.get("SYN_LANGUAGE", "verilog"),
    }
    # Only set DESIGN when we have an explicit top name – this is now
    # optional, and the TCL script will pick a reasonable default otherwise.
    if top_name:
        updates["DESIGN"] = top_name
    # Formality-specific variable (when synthesized_file is provided)
    if synthesized_file is not None:
        updates["SYNTHESIZED_FILE"] = str(synthesized_file.resolve())
    if json_file is not None:
        updates["JSON_FILE"] = str(json_file.resolve())
    env.update(updates)
    return env


def _get_yosys_cmd() -> Optional[List[str]]:
    """Return a command list to invoke Yosys or yowasp-yosys, or None if neither is available."""
    yosys_path = shutil.which("yosys")
    if yosys_path:
        return [yosys_path]
    yowasp_path = shutil.which("yowasp-yosys")
    if yowasp_path:
        return [yowasp_path]
    try:
        import yowasp_yosys  # type: ignore
        return [sys.executable, "-m", "yowasp_yosys"]
    except Exception:
        return None


def _yosys_precheck(rtl_path: Path, top_name: Optional[str]) -> Optional[bool]:
    """Static synthesizability precheck using Yosys.
    Returns True on pass, False on fail, None if Yosys is unavailable.
    """
    yosys_cmd = _get_yosys_cmd()
    if yosys_cmd is None:
        return None
    design_file = rtl_path / "design.v"
    log_file = rtl_path / "yosys_check.log"
    if top_name:
        script = (
            f"read_verilog -sv {design_file}; "
            f"hierarchy -check -top {top_name}; proc; memory; opt; check -assert"
        )
    else:
        script = (
            f"read_verilog -sv {design_file}; "
            f"hierarchy -check -auto-top; proc; memory; opt; check -assert"
        )
    cmd: List[str] = [*yosys_cmd, "-Q", "-l", str(log_file), "-p", script]
    try:
        subprocess.run(cmd, check=True, timeout=YOSYS_TIMEOUT_SECONDS)
        return True
    except subprocess.TimeoutExpired:
        print(f"⚠️  Yosys precheck timed out after {YOSYS_TIMEOUT_SECONDS}s")
        return False
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        return None


def _cleanup_failed_synthesis(rtl_dir: Path, top_name: str):
    """Delete synthesized outputs when synthesis/verification fails or netlist must not be kept.
    Removes the entire results/ tree (all artifacts for that example) and reports/.
    Keeps original RTL (design.v) and log files next to the example dir (e.g. dc_shell_run.log).
    """
    results_dir = rtl_dir / "results"
    reports_dir = rtl_dir / "reports"
    deleted_items = []
    if results_dir.exists():
        try:
            shutil.rmtree(results_dir)
            deleted_items.append("results/")
        except Exception as e:
            print(f"⚠️  Could not delete {results_dir}: {e}")
    # Remove reports directory
    if reports_dir.exists():
        try:
            shutil.rmtree(reports_dir)
            deleted_items.append("reports/")
        except Exception as e:
            print(f"⚠️  Could not delete {reports_dir}: {e}")
    if deleted_items:
        print(f"🗑️  Cleaned up {len(deleted_items)} item(s): {', '.join(deleted_items)}")


def _should_keep_synthesized_netlist(info: Dict[str, Any]) -> bool:
    """Return whether to retain this example's synthesis outputs.

    When this is False, callers run ``_cleanup_failed_synthesis``, which removes the
    whole ``results/`` directory for that example (not only the structural netlist),
    plus ``reports/``.
    """
    if info.get("verification_result") != "succeed":
        return False
    cells = info.get("total_cell_count")
    try:
        n = int(cells) if cells is not None else 0
    except (TypeError, ValueError):
        return False
    return n > 0


def _example_results_are_all_kept(example_dir: Path) -> bool:
    """True if every *.v under results/ has a matching *_info.json that passes the keep policy."""
    results_dir = example_dir / "results"
    if not results_dir.is_dir():
        return True
    for v_file in results_dir.glob("*.v"):
        jpath = results_dir / f"{v_file.stem}_info.json"
        if not jpath.exists():
            return False
        try:
            with open(jpath, encoding="utf-8") as fr:
                info = json.load(fr)
        except Exception:
            return False
        if not _should_keep_synthesized_netlist(info):
            return False
    return True


def _cleanup_stale_synthesis_under_work_root(output_root: Path) -> None:
    """Remove synthesis artifacts that are not fully verified (e.g. after Slurm SIGTERM).

    Only touches *this* rank's work directory (OUTPUT_ROOT == .../work_PROCID).
    """
    for example_dir in sorted(output_root.glob("example_*")):
        if not example_dir.is_dir():
            continue
        results_dir = example_dir / "results"
        if not results_dir.is_dir() or not any(results_dir.glob("*.v")):
            continue
        if _example_results_are_all_kept(example_dir):
            continue
        print(f"⏱️  Final cleanup: removing non-kept outputs under {example_dir.name}", flush=True)
        _cleanup_failed_synthesis(example_dir, "")


def _run_formality(rtl_dir: Path, top_name: str, example_id: int,) -> Optional[bool]:
    """Run Formality verification to compare RTL with synthesized netlist.
    
    Returns:
        True: Verification passed
        False: Verification failed
        None: Verification skipped (tool unavailable, files missing, etc.)
    """
    fm_path = shutil.which("fm_shell")
    if not fm_path:
        print(f"⚠️  Formality (fm_shell) not found – skipping verification")
        return None
    
    rtl_file = rtl_dir / "design.v"
    synthesized_file = rtl_dir / "results" / f"{top_name}.v"
    json_file = rtl_dir / "results" / f"{top_name}_info.json"

    # Check that both files exist
    if not rtl_file.exists():
        print(f"⚠️  RTL file not found: {rtl_file} – skipping verification")
        return None
    if not synthesized_file.exists():
        print(f"⚠️  Synthesized file not found: {synthesized_file} – skipping verification")
        return None
    
    # Prepare environment for Formality using _prepare_env
    # RTL file path is constructed in formality.tcl from RTL_PATH, so we only need to pass verilog_file
    fm_env = _prepare_env(rtl_dir, top_name, synthesized_file=synthesized_file, json_file=json_file)

    log_file = rtl_dir / "formality.log"

    cmd: List[str] = [
        fm_path,
        "-work_path",
        str(rtl_dir),
        "-file",
        str(FORMALITY_SCRIPT_PATH.resolve()),
    ]
    
    try:
        print(f"🔍 Running Formality verification …")
        log_content = subprocess.run(cmd, check=True, env=fm_env, capture_output=True, timeout=FM_TIMEOUT_SECONDS)
        # Check if verification passed by examining the log
        # Formality returns 0 on success, but we should also check the log for verification status
        if not log_file.exists():
            log_file.write_text(log_content.stdout.decode("utf-8"))
        is_failed = log_content.returncode != 0 or "Verification FAILED" in log_content.stdout.decode("utf-8")
        info: Dict[str, Any] = {}
        if json_file.exists():
            with open(json_file, "r") as fr:
                info = json.load(fr)
        else:
            info = {"design_name": top_name}
        info["verification_result"] = "succeed" if not is_failed else "failed"
        with open(json_file, "w") as fw:
            json.dump(info, fw, indent=2)

        if _should_keep_synthesized_netlist(info):
            print(f"✅ [#{example_id}] Formality verification PASSED – keeping generated files")
            return True

        if is_failed:
            print(f"❌ [#{example_id}] Formality verification FAILED – cleaning up generated files")
        else:
            tc = info.get("total_cell_count")
            print(
                f"❌ [#{example_id}] Formality passed but netlist rejected "
                f"(need total_cell_count > 0 and verification_result == \"succeed\"; "
                f"got total_cell_count={tc!r}) – cleaning up generated files"
            )
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False
    except subprocess.TimeoutExpired:
        print(f"❌ Formality timed out after {FM_TIMEOUT_SECONDS}s – cleaning up generated files")
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False
    except subprocess.CalledProcessError as exc:
        print(f"❌ Formality verification failed (return-code {exc.returncode}) – cleaning up generated files")
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False
    except Exception as exc:
        print(f"❌ Formality unexpected error: {exc} – cleaning up generated files")
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False


def _has_existing_result(example_id: int) -> bool:
    """Return True if any synthesized .v exists under results/ for this example id.

    Checks across the current task's output root since worker execution is scoped
    per PROCID. The pre-scan covers all work_* roots.
    """
    example_dir = OUTPUT_ROOT / f"example_{example_id:06d}"
    results_dir = example_dir / "results"
    try:
        return any(results_dir.glob("*.v"))
    except Exception:
        return False


def _run_dc_shell(rtl_dir: Path, top_name: str, example_id: int) -> bool:
    """Run Design Compiler. On any failure, remove synthesized netlists under results/.

    Returns:
        True if DC completed successfully; False if precheck failed, DC failed, or timed out.
    """
    env = _prepare_env(rtl_dir, top_name)

    # Pre-synthesis synthesizability check
    yosys_ok = _yosys_precheck(rtl_dir, top_name)
    if yosys_ok is True:
        print(f"ℹ️  [#{example_id}] Yosys precheck passed. Proceeding with synthesis")
    elif yosys_ok is False:
        print(f"ℹ️  [#{example_id}] Yosys precheck failed. Skipping synthesis – cleaning up generated files")
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False
    elif yosys_ok is None:
        print(f"ℹ️  [#{example_id}] Yosys unavailable... Proceeding with synthesis")

    log_file = rtl_dir / "dc_shell_run.log"

    try:
        cmd: List[str] = [
            "dc_shell",
            "-no_gui",
            "-output_log_file",
            str(log_file),
            "-f",
            str(TCL_SCRIPT_PATH.resolve()),
        ]

        print(f"\n🚀 [#{example_id}] Running Design Compiler …")
        print("🔧 Command:", " ".join(cmd))

        subprocess.run(cmd, check=True, env=env, timeout=DC_TIMEOUT_SECONDS)

        print(f"✅ [#{example_id}] Synthesis complete → results in {rtl_dir}")
        return True

    except subprocess.TimeoutExpired:
        print(f"❌ [#{example_id}] Design Compiler timed out after {DC_TIMEOUT_SECONDS}s. Check {rtl_dir} for logs.")
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False
    except subprocess.CalledProcessError as exc:
        print(f"❌ [#{example_id}] Design Compiler failed (return-code {exc.returncode}). Check {rtl_dir} for logs.")
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False
    except Exception as exc:
        print(f"❌ [#{example_id}] Unexpected error: {exc}")
        _cleanup_failed_synthesis(rtl_dir, top_name)
        return False


def _run_synthesis_and_verification(example_id: int, example: Dict[str, Any], nonsynth_verilog_code_regex: re.Pattern):
    # Fast-path: if results already exist for this example, skip running DC
    if _has_existing_result(example_id):
        print(f"⏭️  [#{example_id}] Results already exist – skipping synthesis")
        return None

    # Get the Verilog code from the example
    verilog_code = example.get("text") or example.get("verilog") or example.get("code") or example.get("RTL")
    if verilog_code is None:
        print(f"⚠️  [#{example_id}] No recognised RTL fields (text/verilog/code/RTL) – skipping")
        return None
    if nonsynth_verilog_code_regex is not None and nonsynth_verilog_code_regex.search(verilog_code):
        print(f"⚠️  [#{example_id}] Contains non-synthesizable Verilog constructs – skipping")
        return None

    # Dump the RTL files to the output directory
    rtl_dir = _dump_rtl_files(verilog_code, example_id)

    # Determine top-level module name if present in the dataset
    top_name = find_top_module_name(verilog_code)
    if top_name is None:
        return None

    # If the specific expected output exists, skip
    expected_out = rtl_dir / "results" / f"{top_name}.v"
    if expected_out.exists():
        print(f"⏭️  [#{example_id}] Found existing output {expected_out.name} – skipping synthesis")
        return None

    # Run synthesis; do not run Formality if DC did not produce a clean success
    if not _run_dc_shell(rtl_dir, top_name, example_id):
        return None

    # Run Formality verification
    _run_formality(rtl_dir, top_name, example_id)

        
# --------------------------------------------------------------------------------------
# Main parallel loop
# --------------------------------------------------------------------------------------

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _request_shutdown)

    print(f"🛠  Starting parallel synthesis with up to {MAX_WORKERS} workers …")

    # ------------------------------------------------------------------
    # Pre-scan to detect examples already synthesized in any work_* root
    # ------------------------------------------------------------------
    def _scan_completed_examples() -> Set[int]:
        completed: Set[int] = set()
        # Look across all work_* directories to catch previous runs
        for work_dir in OUTPUT_PATH.glob("work_*"):
            if not work_dir.is_dir():
                continue
            for v_file in work_dir.glob("example_*/results/*.v"):
                try:
                    example_dir = v_file.parent.parent  # .../example_xxxxxx/results/*.v
                    if example_dir.name.startswith("example_"):
                        ex_id = int(example_dir.name.split("example_")[1])
                        completed.add(ex_id)
                except Exception:
                    # Be resilient to any odd directory names
                    continue
        if completed:
            print(f"🧮 Detected {len(completed)} completed examples from previous runs")
        return completed

    COMPLETED_EXAMPLE_IDS = _scan_completed_examples()

    nonsynth_verilog_code_regex = re.compile(NOT_SYNTHESIZABLE_VERILOG_CODE_PATTERN) if DATASET != "metrex" else None

    executor = ProcessPoolExecutor(max_workers=MAX_WORKERS)
    pending: set = set()
    shutdown_wait = True
    try:
        for idx, ex in enumerate(ds):
            if _shutdown_requested.is_set():
                shutdown_wait = False
                break
            if idx % STRIDE != PROCID:
                continue
            # Skip examples already completed in any previous run
            if idx in COMPLETED_EXAMPLE_IDS:
                print(f"⏭️  [#{idx}] Already synthesized in previous runs – skipping")
                continue
            # Bounded submission: keep at most 2× workers queued to limit memory
            while len(pending) >= MAX_WORKERS * 2:
                if _shutdown_requested.is_set():
                    shutdown_wait = False
                    break
                done, pending = wait(pending, return_when=FIRST_COMPLETED, timeout=2.0)
                for fut in done:
                    try:
                        fut.result()
                    except Exception as exc:
                        print(f"⚠️  Worker raised: {exc}")
            if not shutdown_wait:
                break
            pending.add(
                executor.submit(_run_synthesis_and_verification, idx, ex, nonsynth_verilog_code_regex)
            )

        if _shutdown_requested.is_set():
            shutdown_wait = False

        if shutdown_wait:
            for fut in as_completed(pending):
                try:
                    fut.result()
                except Exception as exc:
                    print(f"⚠️  Worker raised: {exc}")
        else:
            for fut in pending:
                fut.cancel()
    finally:
        executor.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
        if not shutdown_wait:
            _cleanup_stale_synthesis_under_work_root(OUTPUT_ROOT)

    if shutdown_wait:
        print("🎉 All synthesis jobs dispatched.")
    else:
        print("⚠️  Synthesis stopped early; stale outputs under this work root were cleaned up where possible.")
