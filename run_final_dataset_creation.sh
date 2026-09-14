#!/bin/bash
#SBATCH --job-name=final_dataset_creation
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=64
#SBATCH --mem=192G
#SBATCH --output=logs/final_dataset_creation_%j.log

set -euo pipefail

# Define environment variables
export DATA_PATH="/home/cxv200006/work/transformers_atpg/data"
LIB_VARIANT="${1:-${LIB_VARIANT:-RVT}}"  # RVT / LVT / SLVT / SRAM
PVT_CORNER="${2:-${PVT_CORNER:-TT}}"  # TT / SS / FF
DATASET="${3:-${DATASET:-freeset}}"  # freeset / metrex / shailja
LIBRARY="${4:-${LIBRARY:-asap7sc7p5t_28}}"  # asap7sc7p5t_28
export LIB_VARIANT PVT_CORNER DATASET LIBRARY

# Build locally without replacing old shards or publishing to the Hub.
# Set OUTPUT_DIR to a new directory for each build. If supplied, SIM_CONFIG must
# match the selected library/variant; otherwise extract functions from Liberty.
suffix="${DATASET,,}.${LIBRARY,,}.${LIB_VARIANT,,}.${PVT_CORNER,,}"
OUTPUT_DIR="${OUTPUT_DIR:-$DATA_PATH/$DATASET/dataset.$suffix.stil_repaired_v1}"
config_args=()
if [[ -n "${SIM_CONFIG:-}" ]]; then
    config_args+=(--sim_config "$SIM_CONFIG")
fi
python final_dataset_creation.py \
    "${config_args[@]}" \
    --output_dir "$OUTPUT_DIR" \
    --workers "${SLURM_CPUS_PER_TASK:-4}" \
    --validation_circuits "${VALIDATION_CIRCUITS:-8}" \
    --seed "${SPLIT_SEED:-20260910}"
