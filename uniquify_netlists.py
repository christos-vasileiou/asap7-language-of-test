#!/usr/bin/env python3
"""
Uniquify structural netlists from synthesis outputs and prune invalid examples.

Scans:  data/<DATASET>/work_*/example_*/results
Output: data/<DATASET>/structural.<suffix>/

For each example_*:
  - No ``results/``, or no usable netlist pair → delete the entire example_* tree.
  - Usable pair: ``{design}.v`` + ``{design}_info.json``, JSON parses,
    ``total_cell_count`` > 0. Optional ``--require-verified`` also requires
    ``verification_result == "succeed"``.

Uniquification: SHA-256 of (verilog bytes + ``sequential``|``combinational``). Filenames:
  ``<sha256>_<sequential|combinational>.v`` and ``<sha256>_<sequential|combinational>_info.json``.
  ``has_sequential_logic is True`` → ``sequential``; otherwise ``combinational``.

This replaces the legacy ``copy_verified_files.py`` (index-based names, no dedup,
no sequential/combinational, only deleted examples missing ``results/``, not broken ones).

Environment (optional):
  DATASET, STRUCTURAL_SUFFIX — same as ``--dataset`` / ``--suffix`` defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


def _repo_data_default() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _iter_example_dirs(dataset_dir: Path) -> Iterator[Path]:
    """Yield example_* directories under work_* (os.scandir for large trees)."""
    try:
        with os.scandir(dataset_dir) as work_it:
            work_entries = [e for e in work_it if e.is_dir() and e.name.startswith("work_")]
    except OSError:
        return
    for work_entry in sorted(work_entries, key=lambda e: e.name):
        try:
            with os.scandir(work_entry.path) as ex_it:
                for example_entry in sorted(
                    (e for e in ex_it if e.is_dir() and e.name.startswith("example_")),
                    key=lambda e: e.name,
                ):
                    yield Path(example_entry.path)
        except OSError:
            continue


def _list_info_jsons(results_dir: Path) -> List[Path]:
    out: List[Path] = []
    try:
        with os.scandir(results_dir) as it:
            for f in it:
                if f.is_file() and f.name.endswith("_info.json"):
                    out.append(Path(f.path))
    except OSError:
        pass
    return sorted(out)


def _parse_total_cell_count(info: Dict[str, Any]) -> Optional[int]:
    raw = info.get("total_cell_count")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_sequential(info: Dict[str, Any]) -> bool:
    return info.get("has_sequential_logic") is True


def _pair_kind_suffix(info: Dict[str, Any]) -> str:
    return "sequential" if _is_sequential(info) else "combinational"


def _content_fingerprint(verilog_path: Path, kind: str) -> str:
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    h.update(b"\0")
    try:
        with verilog_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        h.update(b"<missing>")
    return h.hexdigest()


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


@dataclass
class ValidPair:
    example_dir: Path
    verilog_path: Path
    json_path: Path
    kind: str
    fingerprint: str


def _collect_valid_pairs_for_example(
    example_dir: Path,
    require_verified: bool,
) -> List[ValidPair]:
    results_dir = example_dir / "results"
    if not results_dir.is_dir():
        return []

    pairs: List[ValidPair] = []
    for json_path in _list_info_jsons(results_dir):
        design = json_path.name[: -len("_info.json")]
        if not design:
            continue
        v_path = results_dir / f"{design}.v"
        if not v_path.is_file():
            continue
        info = _load_json(json_path)
        if info is None:
            continue
        if require_verified and info.get("verification_result") != "succeed":
            continue
        tc = _parse_total_cell_count(info)
        if tc is None or tc <= 0:
            continue
        kind = _pair_kind_suffix(info)
        fp = _content_fingerprint(v_path, kind)
        pairs.append(
            ValidPair(
                example_dir=example_dir,
                verilog_path=v_path,
                json_path=json_path,
                kind=kind,
                fingerprint=fp,
            )
        )
    return pairs


def _example_should_be_deleted(example_dir: Path, valid_pairs: List[ValidPair]) -> bool:
    results_dir = example_dir / "results"
    if not results_dir.is_dir():
        return True
    return not valid_pairs


def _copy_one_pair(args: Tuple[Path, Path, Path]) -> Tuple[bool, str]:
    """Copy verilog + json to dest paths. Used by worker pool."""
    src_v, src_j, dest_v = args
    dest_j = dest_v.with_name(dest_v.name.replace(".v", "_info.json"))
    try:
        shutil.copy2(src_v, dest_v)
        shutil.copy2(src_j, dest_j)
        return True, ""
    except OSError as e:
        return False, str(e)


def _delete_one(path: Path) -> Tuple[bool, str]:
    try:
        shutil.rmtree(path)
        return True, ""
    except OSError as e:
        return False, str(e)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Uniquify structural netlists under structural.<suffix>/ and remove invalid examples.",
    )
    parser.add_argument(
        "--dataset",
        default=os.environ.get("DATASET", "freeset"),
        help="Dataset name (subdirectory of --data-root). Default: env DATASET or freeset.",
    )
    parser.add_argument(
        "--suffix",
        default=os.environ.get("STRUCTURAL_SUFFIX"),
        help="Output dir: <dataset>/structural.<suffix>/. Default: env STRUCTURAL_SUFFIX or 'default'.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=_repo_data_default(),
        help="Parent of per-dataset folders (default: repo data/).",
    )
    parser.add_argument(
        "--require-verified",
        action="store_true",
        help='Keep only pairs with verification_result == "succeed" (legacy copy_verified_files behavior).',
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=int(os.environ.get("NUM_WORKERS", cpu_count())),
        help="Parallel workers for copy/delete (default: NUM_WORKERS or CPU count).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions; no copy or delete.",
    )
    args = parser.parse_args()

    suffix = args.suffix or os.environ.get("STRUCTURAL_SUFFIX") or "default"
    dataset_dir = (args.data_root / args.dataset).resolve()
    out_dir = (dataset_dir / f"structural.{suffix}").resolve()

    if not dataset_dir.is_dir():
        print(f"Error: dataset directory does not exist: {dataset_dir}", file=sys.stderr)
        return 1

    to_delete: List[Path] = []
    all_valid: List[ValidPair] = []

    for example_dir in _iter_example_dirs(dataset_dir):
        valid = _collect_valid_pairs_for_example(example_dir, args.require_verified)
        if _example_should_be_deleted(example_dir, valid):
            to_delete.append(example_dir)
        else:
            all_valid.extend(valid)

    seen_fp: Dict[str, ValidPair] = {}
    for p in all_valid:
        if p.fingerprint not in seen_fp:
            seen_fp[p.fingerprint] = p

    unique_pairs = list(seen_fp.values())
    duplicates = len(all_valid) - len(unique_pairs)

    print(f"Dataset:     {dataset_dir}")
    print(f"Output:      {out_dir}")
    print(f"Verified-only filter: {args.require_verified}")
    print(f"Examples to delete (no valid netlist): {len(to_delete)}")
    print(f"Valid netlist pairs found:             {len(all_valid)}")
    print(f"Unique after dedup:                    {len(unique_pairs)} (dropped {duplicates} duplicates)")

    if args.dry_run:
        print("\n[dry-run] Would create output dir and copy unique netlists.")
        for p in unique_pairs[:10]:
            print(f"  copy -> {p.fingerprint}_{p.kind}.v (+ _info.json)")
        if len(unique_pairs) > 10:
            print(f"  ... and {len(unique_pairs) - 10} more")
        print(f"\n[dry-run] Would delete {len(to_delete)} example_* directories.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    copy_jobs: List[Tuple[Path, Path, Path]] = []
    for p in unique_pairs:
        base_name = f"{p.fingerprint}_{p.kind}"
        dest_v = out_dir / f"{base_name}.v"
        copy_jobs.append((p.verilog_path, p.json_path, dest_v))

    nw = max(1, args.num_workers)
    copy_errors: List[str] = []
    if nw == 1 or len(copy_jobs) <= 1:
        for job in copy_jobs:
            ok, err = _copy_one_pair(job)
            if not ok:
                copy_errors.append(err)
    else:
        with ProcessPoolExecutor(max_workers=nw) as ex:
            futs = [ex.submit(_copy_one_pair, job) for job in copy_jobs]
            for fut in as_completed(futs):
                ok, err = fut.result()
                if not ok:
                    copy_errors.append(err)

    if copy_errors:
        print(f"Copy failures: {len(copy_errors)}", file=sys.stderr)
        for msg in copy_errors[:20]:
            print(f"  {msg}", file=sys.stderr)
        return 1

    delete_errors: List[Tuple[Path, str]] = []
    if nw == 1 or len(to_delete) <= 1:
        for path in to_delete:
            ok, err = _delete_one(path)
            if not ok:
                delete_errors.append((path, err))
    else:
        with ProcessPoolExecutor(max_workers=nw) as ex:
            futs = {ex.submit(_delete_one, p): p for p in to_delete}
            for fut in as_completed(futs):
                ok, err = fut.result()
                if not ok:
                    delete_errors.append((futs[fut], err))

    print(f"Copied {len(unique_pairs)} unique pairs to {out_dir}")
    print(f"Deleted {len(to_delete) - len(delete_errors)}/{len(to_delete)} example directories")
    if delete_errors:
        print("Delete failures:", file=sys.stderr)
        for path, err in delete_errors[:20]:
            print(f"  {path}: {err}", file=sys.stderr)
        if len(delete_errors) > 20:
            print(f"  ... and {len(delete_errors) - 20} more", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
