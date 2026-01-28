#!/bin/bash
#SBATCH --job-name=freeset_syn
#SBATCH --partition=normal
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=2
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G

DATE=$(date +"%Y-%m-%d_%a")
mkdir -p logs
logs_dir="/home/cxv200006/work/transformers_atpg/data_preprocessing/logs"
exec >"${logs_dir}/${DATE}_${SLURM_JOB_ID}.out" 2>"${logs_dir}/${DATE}_${SLURM_JOB_ID}.err"

source /home/cxv200006/work/myenv/bin/activate

# Timing corner
LIB_VARIANT="${1:-${LIB_VARIANT:-RVT}}"  # RVT / LVT / SLVT / SRAM
PVT_CORNER="${2:-${PVT_CORNER:-TT}}"  # TT / SS / FF
DATASET="${3:-${DATASET:-freeset}}"  # freeset / metrex / shailja
LIBRARY="${4:-${LIBRARY:-asap7sc7p5t_28}}"  # asap7sc7p5t_28
export LIB_VARIANT PVT_CORNER DATASET LIBRARY


mkdir -p /home/cxv200006/work/transformers_atpg/data/${DATASET}/structural.v.${DATASET,,}.${LIBRARY,,}.${LIB_VARIANT,,}.${PVT_CORNER,,}

scontrol show job $SLURM_JOB_ID | grep -E "NumNodes|NumCPUs|NumTasks"
echo "--workers $SLURM_CPUS_PER_TASK --stride $SLURM_NTASKS --procid $SLURM_PROCID"
echo "LIB_VARIANT=${LIB_VARIANT} PVT_CORNER=${PVT_CORNER} DATASET=${DATASET} LIBRARY=${LIBRARY}"

srun --ntasks=${SLURM_NTASKS:-1} --cpus-per-task=${SLURM_CPUS_PER_TASK:-1} python syn.py --workers "$((SLURM_CPUS_PER_TASK / 4))" --stride "$SLURM_JOB_NUM_NODES" --procid "$SLURM_PROCID"

echo "----------------------------------------"
echo "Done"
exit
