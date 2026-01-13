#!/bin/bash
#SBATCH --job-name=freeset_syn
#SBATCH --partition=normal
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=2
#SBATCH --cpus-per-task=32
#SBATCH --mem=64G

DATE=$(date +"%Y-%m-%d_%a")
mkdir -p logs
logs_dir="/home/cxv200006/work/transformers_atpg/data_preprocessing/logs"
exec >"${logs_dir}/${DATE}_${SLURM_JOB_ID}.out" 2>"${logs_dir}/${DATE}_${SLURM_JOB_ID}.err"

source /home/cxv200006/work/myenv/bin/activate

# Worst-case timing corner
export LIB_VARIANT=${1:-RVT} # RVT / LVT / SLVT / SRAM
export PVT_CORNER=${2:-TT} # TT / SS / FF

# set dataset and library
export DATASET=${3:-freeset} # freeset / metrex / shailja
export LIBRARY=${4:-asap7sc7p5t_28} # asap7sc7p5t_28

mkdir -p /home/cxv200006/work/transformers_atpg/data/structural.v.${DATASET,,}.${LIBRARY,,}.${LIB_VARIANT,,}.${PVT_CORNER,,}

scontrol show job $SLURM_JOB_ID | grep -E "NumNodes|NumCPUs|NumTasks"
echo "--workers $SLURM_CPUS_PER_TASK --stride $SLURM_NTASKS --procid $SLURM_PROCID"
echo "LIB_VARIANT=${LIB_VARIANT} PVT_CORNER=${PVT_CORNER} DATASET=${DATASET} LIBRARY=${LIBRARY}"

srun --ntasks=${SLURM_NTASKS:-1} --cpus-per-task=${SLURM_CPUS_PER_TASK:-1} \
        bash -c './syn.py --workers "$(($SLURM_CPUS_PER_TASK / 4))" --stride "$SLURM_JOB_NUM_NODES" --procid "$SLURM_PROCID"'

echo "----------------------------------------"
echo "Done"
exit
