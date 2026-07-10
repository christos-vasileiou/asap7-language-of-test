#!/bin/bash
#SBATCH --job-name=copy_verified_files        # Job name
#SBATCH --output=logs/copy_verified_files_%j.out   # Standard output file (%j will be replaced with job ID)
#SBATCH --error=logs/copy_verified_files_%j.err    # Standard error file
#SBATCH --nodes=1                       # Request 1 node
#SBATCH --ntasks=1                      # Run a single task
#SBATCH --cpus-per-task=64              # Request 64 CPUs per task
#SBATCH --mem=128G                       # Request 128GB of memory
#SBATCH --partition=normal

#
# Script to copy verified verilog and json files with unique index prefix
# 
# Environment variables (with defaults):
#   LIB_VARIANT  - Library variant: RVT / LVT / SLVT / SRAM (default: RVT)
#   PVT_CORNER   - PVT corner: TT / SS / FF (default: TT)
#   DATASET      - Dataset name: freeset / metrex / shailja (default: freeset)
#   LIBRARY      - Library name (default: asap7sc7p5t_28)
#   NUM_WORKERS  - Number of parallel workers (default: nproc)
#
# Usage: 
#   ./run_copy_verified_files.sh                                  # Use env vars or defaults
#   ./run_copy_verified_files.sh RVT TT freeset asap7sc7p5t_28 8  # Override with args
#   LIB_VARIANT=LVT ./run_copy_verified_files.sh                  # Use env var

set -euo pipefail

# Resolve script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Configuration with argument > env var > default fallback
LIB_VARIANT="${1:-${LIB_VARIANT:-RVT}}"
PVT_CORNER="${2:-${PVT_CORNER:-TT}}"
DATASET="${3:-${DATASET:-freeset}}"
LIBRARY="${4:-${LIBRARY:-asap7sc7p5t_28}}"
NUM_WORKERS="${5:-${NUM_WORKERS:-$(nproc)}}"

export LIB_VARIANT PVT_CORNER DATASET LIBRARY NUM_WORKERS

# Construct paths (lowercase for consistency)
INPUT_BASE_DIR="${SCRIPT_DIR}/../data/${DATASET,,}"
OUTPUT_DIR="${SCRIPT_DIR}/../data/${DATASET,,}/structural.v.${DATASET,,}.${LIBRARY,,}.${LIB_VARIANT,,}.${PVT_CORNER,,}"

echo "=============================================="
echo "Configuration:"
echo "  LIB_VARIANT: ${LIB_VARIANT}"
echo "  PVT_CORNER:  ${PVT_CORNER}"
echo "  DATASET:     ${DATASET}"
echo "  LIBRARY:     ${LIBRARY}"
echo "  NUM_WORKERS: ${NUM_WORKERS}"
echo ""
echo "Paths:"
echo "  Input:  ${INPUT_BASE_DIR}"
echo "  Output: ${OUTPUT_DIR}"
echo "=============================================="

# Run the Python script
exec python3 "${SCRIPT_DIR}/copy_verified_files.py" "${INPUT_BASE_DIR}" "${OUTPUT_DIR}" "${NUM_WORKERS}"
