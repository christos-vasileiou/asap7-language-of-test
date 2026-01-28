#!/bin/bash
#SBATCH --job-name=fault-net-data-pre
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=64
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --array=0-3
#SBATCH --output=logs/slurm-fault-net-data-pre_%j_%a.out
#SBATCH --error=logs/slurm-fault-net-data-pre_%j_%a.err

set -euo pipefail

# Usage: $0 <mode> [phase] [LIB_VARIANT] [PVT_CORNER] [DATASET] [LIBRARY] [starting_point]
# mode: "rust" or "python"
# phase: "fault_net" (only fault net preprocessing), "data" (only data preprocessing), or "all" (default)
#
# IMPORTANT:
#   - Rust fault_net_preprocessor supports sharding (SLURM array)
#   - Python fault_net_preprocessing.py does NOT support sharding (uses internal multiprocessing)
#   - Both data_preprocessor scripts do NOT support sharding and should run only ONCE
#
# Recommended usage:
#   - For rust mode with array: Run "fault_net" phase first, then "data" phase separately (single job)
#   - For python mode: Set array size to 1 (or just run with --array=0)

print_usage() {
  echo "Usage: $0 <mode> [phase] [LIB_VARIANT] [PVT_CORNER] [DATASET] [LIBRARY] [starting_point]"
  echo "  mode: 'rust' or 'python'"
  echo "  phase: 'fault_net', 'data', or 'all' (default: 'all')"
  echo "  LIB_VARIANT: library variant (default: RVT)"
  echo "  PVT_CORNER: PVT corner (default: TT)"
  echo "  DATASET: dataset name (default: freeset)"
  echo "  LIBRARY: library name (default: asap7sc7p5t_28)"
  echo "  starting_point: starting index (default: 0)"
  echo ""
  echo "Examples:"
  echo "  sbatch --array=0-3 $0 rust fault_net    # Run fault net sharded across 4 jobs"
  echo "  sbatch --array=0   $0 rust data         # Run data preprocessing (single job)"
  echo "  sbatch --array=0   $0 python all        # Run all python preprocessing (single job)"
  exit 1
}

if [[ $# -lt 1 ]]; then
  print_usage
fi

MODE=$1
PHASE=${2:-all}
LIB_VARIANT="${1:-${LIB_VARIANT:-RVT}}"  # RVT / LVT / SLVT / SRAM
PVT_CORNER="${2:-${PVT_CORNER:-TT}}"  # TT / SS / FF
DATASET="${3:-${DATASET:-freeset}}"  # freeset / metrex / shailja
LIBRARY="${4:-${LIBRARY:-asap7sc7p5t_28}}"  # asap7sc7p5t_28
export LIB_VARIANT PVT_CORNER DATASET LIBRARY

STARTING_POINT=${7:-0}

# Validate mode
if [[ "${MODE}" != "rust" && "${MODE}" != "python" ]]; then
  echo "Error: Invalid mode '${MODE}'. Use 'rust' or 'python'."
  exit 1
fi

# Validate phase
if [[ "${PHASE}" != "fault_net" && "${PHASE}" != "data" && "${PHASE}" != "all" ]]; then
  echo "Error: Invalid phase '${PHASE}'. Use 'fault_net', 'data', or 'all'."
  exit 1
fi

# Construct suffix with lowercase values
export suffix="${DATASET,,}.${LIBRARY,,}.${LIB_VARIANT,,}.${PVT_CORNER,,}"

# Set circuit and output folders based on suffix
SCRIPT_DIR="/work/cxv200006/transformers_atpg/data_preprocessing"
CIRCUIT_FOLDER="${SCRIPT_DIR}/../data/${DATASET}/structural.v.${suffix}"
OUTPUT_FOLDER="${SCRIPT_DIR}/../data/${DATASET}/out.${suffix}"
OUT_CSV_FILE="${SCRIPT_DIR}/../data/${DATASET}/dataset.${suffix}.csv"

# Cargo manifest paths for rust mode
CARGO_MANIFEST_FAULT_NET="${SCRIPT_DIR}/fault_net_preprocessor/Cargo.toml"
CARGO_MANIFEST_DATA_PRE="${SCRIPT_DIR}/data_preprocessor/Cargo.toml"

# SLURM array variables
SHARD_COUNT=${SLURM_ARRAY_TASK_COUNT:-1}
SHARD_INDEX=${SLURM_ARRAY_TASK_ID:-0}

echo "============================================"
echo "MODE: ${MODE}"
echo "PHASE: ${PHASE}"
echo "LIB_VARIANT: ${LIB_VARIANT}"
echo "PVT_CORNER: ${PVT_CORNER}"
echo "DATASET: ${DATASET}"
echo "LIBRARY: ${LIBRARY}"
echo "SUFFIX: ${suffix}"
echo "CIRCUIT_FOLDER: ${CIRCUIT_FOLDER}"
echo "OUTPUT_FOLDER: ${OUTPUT_FOLDER}"
echo "OUT_CSV_FILE: ${OUT_CSV_FILE}"
echo "STARTING_POINT: ${STARTING_POINT}"
echo "SHARD_COUNT: ${SHARD_COUNT}"
echo "SHARD_INDEX: ${SHARD_INDEX}"
echo "============================================"

source /work/cxv200006/myenv/bin/activate

# Helper function to run fault_net preprocessing
run_fault_net() {
  if [[ "${MODE}" == "rust" ]]; then
    echo "[${SHARD_INDEX}/${SHARD_COUNT}] Running Rust fault_net_preprocessor..."
    srun cargo run --release --manifest-path "${CARGO_MANIFEST_FAULT_NET}" -- \
      --circuit_folder "${CIRCUIT_FOLDER}" \
      --output_folder "${OUTPUT_FOLDER}" \
      --starting_point "${STARTING_POINT}" \
      --shard_count "${SHARD_COUNT}" \
      --shard_index "${SHARD_INDEX}"
  elif [[ "${MODE}" == "python" ]]; then
    # Python does NOT support sharding - warn if running multiple shards
    if [[ "${SHARD_COUNT}" -gt 1 ]]; then
      if [[ "${SHARD_INDEX}" -ne 0 ]]; then
        echo "WARNING: Python fault_net_preprocessing.py does not support sharding."
        echo "         Skipping shard ${SHARD_INDEX} to avoid duplicate work."
        echo "         Only shard 0 will run the Python script."
        return 0
      else
        echo "WARNING: Running with SHARD_COUNT=${SHARD_COUNT} but Python does not support sharding."
        echo "         Consider using --array=0 for Python mode."
      fi
    fi
    echo "[${SHARD_INDEX}] Running Python fault_net_preprocessing.py..."
    srun python "${SCRIPT_DIR}/fault_net_preprocessing.py" \
      -cf "${CIRCUIT_FOLDER}" \
      -of "${OUTPUT_FOLDER}" \
      -sp "${STARTING_POINT}"
  fi
}

# Helper function to run data preprocessing
run_data_preprocessing() {
  # Data preprocessing should only run on shard 0 to avoid race conditions
  if [[ "${SHARD_COUNT}" -gt 1 && "${SHARD_INDEX}" -ne 0 ]]; then
    echo "INFO: Data preprocessing runs only on shard 0. Skipping shard ${SHARD_INDEX}."
    return 0
  fi

  if [[ "${SHARD_COUNT}" -gt 1 && "${PHASE}" == "all" ]]; then
    echo "WARNING: Running data preprocessing on shard 0 while other shards may still be processing."
    echo "         For correct results, ensure all fault_net jobs complete before running data preprocessing."
    echo "         Consider running 'fault_net' and 'data' phases separately."
  fi

  if [[ "${MODE}" == "rust" ]]; then
    echo "[${SHARD_INDEX}] Running Rust data_preprocessor..."
    srun cargo run --release --manifest-path "${CARGO_MANIFEST_DATA_PRE}" -- \
      --circuit_folder "${CIRCUIT_FOLDER}" \
      --output_folder "${OUTPUT_FOLDER}" \
      --out_data_file "${OUT_CSV_FILE}"
  elif [[ "${MODE}" == "python" ]]; then
    echo "[${SHARD_INDEX}] Running Python data_preprocessing.py..."
    srun python "${SCRIPT_DIR}/data_preprocessing.py" \
      -cf "${CIRCUIT_FOLDER}" \
      -of "${OUTPUT_FOLDER}" \
      -odf "${OUT_CSV_FILE}"
  fi
}

# Execute based on phase
case "${PHASE}" in
  fault_net)
    run_fault_net
    ;;
  data)
    run_data_preprocessing
    ;;
  all)
    run_fault_net
    run_data_preprocessing
    ;;
esac

echo "============================================"
echo "PROCESS: shard ${SHARD_INDEX} of ${SHARD_COUNT} completed (phase: ${PHASE})"
echo "============================================"
