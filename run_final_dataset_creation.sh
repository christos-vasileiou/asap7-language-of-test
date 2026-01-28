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

# Optional: Hugging Face upload settings
export HF_USERNAME="chrivasileiou"
export DATASET_HF_REPO_NAME="asap7-language-of-test"

# Run the script
python final_dataset_creation.py --export_config sim_config.json 2>&1
