#!/usr/bin/env python3
"""
Script to rename .lib files to match the library name specified inside each file.
The library name is found in the 'library (name) {' declaration.
"""

import os
import re
from pathlib import Path

def extract_library_name(file_path):
    """Extract the library name from a .lib file."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                # Look for the library declaration: library (name) {
                match = re.search(r'library\s*\(\s*([^)]+)\s*\)', line)
                if match:
                    return match.group(1).strip()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return None
    return None

def rename_lib_files(directory):
    """Rename all .lib files in the directory to match their library names."""
    directory = Path(directory)
    if not directory.exists():
        print(f"Directory does not exist: {directory}")
        return
    
    # Find all .lib files (excluding .7z files)
    lib_files = list(directory.glob("*.lib"))
    # Filter out .7z files
    lib_files = [f for f in lib_files if not f.name.endswith('.7z')]
    
    print(f"Found {len(lib_files)} .lib files to process")
    
    renamed_count = 0
    skipped_count = 0
    error_count = 0
    
    for lib_file in lib_files:
        library_name = extract_library_name(lib_file)
        
        if not library_name:
            print(f"WARNING: Could not extract library name from {lib_file.name}")
            error_count += 1
            continue
        
        # Create the new filename
        new_name = f"{library_name}.lib"
        new_path = lib_file.parent / new_name
        
        # Skip if the file already has the correct name
        if lib_file.name == new_name:
            print(f"SKIP: {lib_file.name} already has correct name")
            skipped_count += 1
            continue
        
        # Check if target file already exists
        if new_path.exists() and new_path != lib_file:
            print(f"WARNING: Target file already exists: {new_name} (skipping {lib_file.name})")
            error_count += 1
            continue
        
        # Rename the file
        try:
            lib_file.rename(new_path)
            print(f"RENAMED: {lib_file.name} -> {new_name}")
            renamed_count += 1
        except Exception as e:
            print(f"ERROR: Failed to rename {lib_file.name}: {e}")
            error_count += 1
    
    print(f"\nSummary:")
    print(f"  Renamed: {renamed_count}")
    print(f"  Skipped (already correct): {skipped_count}")
    print(f"  Errors: {error_count}")

if __name__ == "__main__":
    lib_dir = "/home/cxv200006/work/transformers_atpg/data_preprocessing/lib/asap7sc7p5t_28/LIB/CCS"
    rename_lib_files(lib_dir)

