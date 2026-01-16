#!/bin/bash
#SBATCH --job-name=final_dataset_creation
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=64
#SBATCH --mem=192G
#SBATCH --time=24:00:00
#SBATCH --output=logs/final_dataset_creation_%j.log

set -euo pipefail

# Define environment variables
export DATA_PATH="/home/cxv200006/work/transformers_atpg/data"
export DATASET="freeset"
export LIBRARY="asap7sc7p5t_28"
export LIB_VARIANT="RVT"
export PVT_CORNER="TT"
export MODEL="meta-llama/Llama-3.3-70B-Instruct"

# Optional: Hugging Face upload settings
export HF_USERNAME="chrivasileiou"
export DATASET_HF_REPO_NAME="asap7-language-of-test"

# Run the script
python final_dataset_creation.py --export_config sim_config.json 2>&1
