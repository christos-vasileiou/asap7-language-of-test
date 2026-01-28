#!/usr/bin/env python3
"""
Optimized script to copy verified verilog and json files with unique index prefix.
Uses multiprocessing for parallel file operations.

Usage: 
    python copy_verified_files.py [input_base_dir] [output_dir] [num_workers]
    
Environment variables (with defaults):
    LIB_VARIANT  - Library variant: RVT / LVT / SLVT / SRAM (default: RVT)
    PVT_CORNER   - PVT corner: TT / SS / FF (default: TT)
    DATASET      - Dataset name: freeset / metrex / shailja (default: freeset)
    LIBRARY      - Library name (default: asap7sc7p5t_28)
    NUM_WORKERS  - Number of parallel workers (default: cpu_count)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count
from pathlib import Path


def retrieve_result_from_json(json_path: Path) -> dict:
    """Retrieve the result from a JSON file."""
    try:
        with json_path.open('r') as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, IOError, KeyError):
        return {}


def find_json_files_and_dirs_to_delete(base_dir: Path) -> tuple[list[Path], list[Path]]:
    """Find all *_info.json files in results directories and directories without 'results' subfolder using os.scandir (faster than glob for deep trees)."""
    json_files = []
    dirs_to_delete = []

    # Walk through work_* directories
    with os.scandir(base_dir) as work_entries:
        for work_entry in work_entries:
            if not work_entry.is_dir() or not work_entry.name.startswith('work_'):
                continue
            
            # Walk through example_* directories
            try:
                with os.scandir(work_entry.path) as example_entries:
                    for example_entry in example_entries:
                        if not example_entry.is_dir() or not example_entry.name.startswith('example_'):
                            continue
                        
                        results_path = Path(example_entry.path) / 'results'
                        if results_path.is_dir():
                            # Find *_info.json files in results directory
                            try:
                                with os.scandir(results_path) as files:
                                    for f in files:
                                        if f.is_file() and f.name.endswith('_info.json'):
                                            json_files.append(Path(f.path))
                            except (PermissionError, OSError):
                                continue
                        elif not results_path.is_dir():
                            dirs_to_delete.append(Path(example_entry.path))
            except (PermissionError, OSError):
                continue
    
    return json_files, dirs_to_delete


def process_json_file(json_path: Path) -> dict:
    """
    Process a single JSON file and return info for copying.
    Returns dict with status and file paths if successful.
    """
    absolute_json_path = json_path.resolve()
    design_name = absolute_json_path.stem.replace('_info', '')
    verilog_path = absolute_json_path.parent / f'{design_name}.v'
    
    if not verilog_path.is_file():
        return {
            'error': f'Verilog file not found: {verilog_path}'
        }
    
    data = retrieve_result_from_json(absolute_json_path)
    result = {
        'json_path': absolute_json_path,
        'verilog_path': verilog_path,
        'design_name': design_name,
        'error': None
    }
    result.update(data)
    return result


def copy_file_pair(args: tuple) -> dict:
    """Copy a verilog and json file pair with index prefix."""
    idx, json_path, verilog_path, design_name, output_dir = args
    
    try:
        dest_v = output_dir / f'{idx}_{design_name}.v'
        dest_json = output_dir / f'{idx}_{design_name}_info.json'
        
        # Use copy instead of copy2 for better performance (skip metadata)
        shutil.copy(verilog_path, dest_v)
        shutil.copy(json_path, dest_json)
        
        return {'idx': idx, 'success': True, 'error': None}
    except Exception as e:
        return {'idx': idx, 'success': False, 'error': str(e)}


def find_example_dirs_without_results(base_dir: Path) -> list[Path]:
    """Find example_* directories without 'results' subfolder."""
    dirs_to_delete = []
    
    with os.scandir(base_dir) as work_entries:
        for work_entry in work_entries:
            if not work_entry.is_dir() or not work_entry.name.startswith('work_'):
                continue
            
            try:
                with os.scandir(work_entry.path) as example_entries:
                    for example_entry in example_entries:
                        if not example_entry.is_dir() or not example_entry.name.startswith('example_'):
                            continue
                        
                        results_path = Path(example_entry.path) / 'results'
                        if not results_path.is_dir():
                            dirs_to_delete.append(Path(example_entry.path))
            except (PermissionError, OSError):
                continue
    
    return dirs_to_delete


def delete_directory(dir_path: Path) -> dict:
    """Delete a directory and return status."""
    try:
        shutil.rmtree(dir_path)
        return {'path': dir_path, 'success': True, 'error': None}
    except Exception as e:
        return {'path': dir_path, 'success': False, 'error': str(e)}


def get_config() -> dict:
    """Get configuration from environment variables with defaults."""
    return {
        'lib_variant': os.environ.get('LIB_VARIANT', 'RVT').lower(),
        'pvt_corner': os.environ.get('PVT_CORNER', 'TT').lower(),
        'dataset': os.environ.get('DATASET', 'freeset'),
        'library': os.environ.get('LIBRARY', 'asap7sc7p5t_28'),
        'num_workers': int(os.environ.get('NUM_WORKERS', cpu_count())),
    }


def main():
    config = get_config()
    
    # Build default paths from config
    default_base_dir = Path(f"../data/{config['dataset']}")
    default_output_dir = Path(
        f"../data/{config['dataset']}/structural.v.{config['dataset']}."
        f"{config['library']}.{config['lib_variant']}.{config['pvt_corner']}"
    )
    default_workers = config['num_workers']
    
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description='Copy verified verilog and json files with unique index prefix.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (with defaults):
  LIB_VARIANT  - Library variant: RVT / LVT / SLVT / SRAM (default: RVT)
  PVT_CORNER   - PVT corner: TT / SS / FF (default: TT)
  DATASET      - Dataset name: freeset / metrex / shailja (default: freeset)
  LIBRARY      - Library name (default: asap7sc7p5t_28)
  NUM_WORKERS  - Number of parallel workers (default: cpu_count)
        """
    )
    parser.add_argument('input_base_dir', nargs='?', default=str(default_base_dir),
                        help=f'Base directory containing work_* folders (default: {default_base_dir})')
    parser.add_argument('output_dir', nargs='?', default=str(default_output_dir),
                        help=f'Output directory for copied files (default: {default_output_dir})')
    parser.add_argument('num_workers', nargs='?', type=int, default=default_workers,
                        help=f'Number of parallel workers (default: {default_workers})')
    
    args = parser.parse_args()
    
    base_dir = Path(args.input_base_dir)
    output_dir = Path(args.output_dir)
    num_workers = args.num_workers
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Phase 1: Find all JSON files
    print("\n[Phase 1] Scanning for JSON files...")
    start_time = time.perf_counter()
    json_files, dirs_to_delete = find_json_files_and_dirs_to_delete(base_dir)
    scan_time = time.perf_counter() - start_time
    print(f"  Found {len(json_files)} JSON files in {scan_time:.2f}s")
    print(f"  Found {len(dirs_to_delete)} directories without 'results' subfolder")
    
    if not json_files:
        print("No JSON files found. Exiting.")
        return
    
    # Phase 2: Process JSON files in parallel to check verification status
    print(f"\n[Phase 2] Checking verification status ({num_workers} workers)...")
    start_time = time.perf_counter()
    
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        results = list(executor.map(process_json_file, json_files))
    
    process_time = time.perf_counter() - start_time
    
    # Filter verified files
    verified_files = [r for r in results if r['error'] is None and 'verification_result' in r and r['verification_result'] == 'succeed']
    skipped_no_verilog = [r for r in results if r['error'] is not None and r['error'].startswith('Verilog file not found')]
    skipped_not_verified = [r for r in results if r['error'] is None and 'verification_result' in r and r['verification_result'] != 'succeed']
    
    print(f"  Processed {len(results)} files in {process_time:.2f}s")
    print(f"  Verified: {len(verified_files)}")
    print(f"  Skipped (not verified): {len(skipped_not_verified)}")
    print(f"  Skipped (missing verilog): {len(skipped_no_verilog)}")
    
    # Phase 3: Copy verified files in parallel
    print(f"\n[Phase 3] Copying {len(verified_files)} file pairs...")
    start_time = time.perf_counter()
    
    # Prepare copy arguments with unique indices
    copy_args = [
        (idx, r['json_path'], r['verilog_path'], r['design_name'], output_dir)
        for idx, r in enumerate(verified_files)
    ]
    
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        copy_results = list(executor.map(copy_file_pair, copy_args))
    
    copy_time = time.perf_counter() - start_time
    
    successful_copies = sum(1 for r in copy_results if r['success'])
    failed_copies = [r for r in copy_results if not r['success']]
    
    print(f"  Copied {successful_copies} file pairs in {copy_time:.2f}s")
    if failed_copies:
        print(f"  Failed: {len(failed_copies)}")
        for f in failed_copies[:5]:
            print(f"    - Index {f['idx']}: {f['error']}")
    
    if dirs_to_delete:
        print(f"\n[Phase 4] Deleting {len(dirs_to_delete)} directories...")
        start_time = time.perf_counter()
        
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            delete_results = list(executor.map(delete_directory, dirs_to_delete))
        
        delete_time = time.perf_counter() - start_time
        
        successful_deletes = sum(1 for r in delete_results if r['success'])
        failed_deletes = [r for r in delete_results if not r['success']]
        
        print(f"  Deleted {successful_deletes} directories in {delete_time:.2f}s")
        if failed_deletes:
            print(f"  Failed: {len(failed_deletes)}")
            for f in failed_deletes[:5]:
                print(f"    - {f['path']}: {f['error']}")
    else:
        successful_deletes = 0
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Files copied: {successful_copies} pairs")
    print(f"  Files skipped (not verified): {len(skipped_not_verified)}")
    print(f"  Files skipped (missing verilog): {len(skipped_no_verilog)}")
    print(f"  Directories deleted: {successful_deletes}")
    print("=" * 60)
    print("Done!")


if __name__ == '__main__':
    main()
