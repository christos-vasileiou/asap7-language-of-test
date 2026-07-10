#!/bin/bash
#SBATCH --job-name=freeset_syn
#SBATCH --partition=normal
#SBATCH --nodes=8 
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=64
#SBATCH --mem=128G
#SBATCH --exclusive
# Default wall time 48h; Slurm sends SIGTERM before the limit, then SIGKILL after KillWait.
#SBATCH --time=48:00:00
# Optional early signal (syntax varies by site / Slurm version), e.g. 300s before end:
#SBATCH --signal=B:USR1@300

DATE=$(date +"%Y-%m-%d_%a")

# Under sbatch, Slurm runs a *copy* of this script from /var/spool/slurmd/job*/
# so ${BASH_SOURCE[0]} is NOT your git checkout — using it as SCRIPT_DIR makes
# python "$SCRIPT_DIR/syn.py" fail with "can't open .../jobNNN/syn.py".
_SYN_DEFAULT="/home/cxv200006/work/transformers_atpg/data_preprocessing"
if [[ -n "${SYN_PIPELINE_ROOT:-}" ]]; then
  SCRIPT_DIR="$SYN_PIPELINE_ROOT"
elif [[ -n "${SLURM_JOB_ID:-}" ]]; then
  if [[ -n "${SLURM_SUBMIT_DIR:-}" && -f "${SLURM_SUBMIT_DIR}/syn.py" ]]; then
    SCRIPT_DIR="$(cd "$SLURM_SUBMIT_DIR" && pwd)"
  else
    SCRIPT_DIR="$_SYN_DEFAULT"
  fi
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
cd "$SCRIPT_DIR" || { echo "Cannot cd to $SCRIPT_DIR"; exit 1; }
[[ -f "$SCRIPT_DIR/syn.py" ]] || {
  echo "syn.py not found under SCRIPT_DIR=$SCRIPT_DIR"
  echo "Fix: cd to data_preprocessing before sbatch, or export SYN_PIPELINE_ROOT=/path/to/data_preprocessing"
  exit 1
}

logs_dir="${SCRIPT_DIR}/logs"
mkdir -p "$logs_dir"
exec >"${logs_dir}/${DATE}_${SLURM_JOB_ID}.out" 2>"${logs_dir}/${DATE}_${SLURM_JOB_ID}.err"

source /home/cxv200006/work/myenv/bin/activate

# Timing corner
LIB_VARIANT="${1:-${LIB_VARIANT:-RVT}}"  # RVT / LVT / SLVT / SRAM
PVT_CORNER="${2:-${PVT_CORNER:-TT}}"  # TT / SS / FF
DATASET="${3:-${DATASET:-freeset}}"  # freeset / metrex / shailja
LIBRARY="${4:-${LIBRARY:-asap7sc7p5t_28}}"  # asap7sc7p5t_28
export LIB_VARIANT PVT_CORNER DATASET LIBRARY
# syn.py sets HF_HOME per SLURM task under $SLURM_TMPDIR to avoid Hugging Face datasets FileLock races on ~/.cache

mkdir -p /home/cxv200006/work/transformers_atpg/data/${DATASET}/structural.v.${DATASET,,}.${LIBRARY,,}.${LIB_VARIANT,,}.${PVT_CORNER,,}

# One syn.py per allocated node: each rank gets its own work_{PROCID} directory.
# Intra-node parallelism is only via syn.py --workers (ProcessPoolExecutor), which
# all share that node's OUTPUT_ROOT. Stride must equal the number of nodes so
# dataset indices shard as idx % NUM_NODES == SLURM_PROCID.
# Do not multiply by SLURM_NTASKS_PER_NODE here — extra sbatch tasks per node would
# not get separate srun ranks with this layout, and stride would no longer match ranks.
NUM_NODES=${SLURM_JOB_NUM_NODES:-1}
if (( NUM_NODES < 1 )); then
  NUM_NODES=1
fi
scontrol show job $SLURM_JOB_ID | grep -E "NumNodes|NumCPUs|NumTasks"
echo "Planned per rank: python syn.py --workers $((SLURM_CPUS_PER_TASK / 4)) --stride ${NUM_NODES} --procid <SLURM_PROCID>"
echo "LIB_VARIANT=${LIB_VARIANT} PVT_CORNER=${PVT_CORNER} DATASET=${DATASET} LIBRARY=${LIBRARY}"
echo "SLURM_JOB_NUM_NODES=${SLURM_JOB_NUM_NODES} SLURM_NTASKS_PER_NODE=${SLURM_NTASKS_PER_NODE} SLURM_NTASKS=${SLURM_NTASKS:-<unset>}"
echo "NUM_NODES=${NUM_NODES} (one syn.py rank per node; --stride matches node count)"

# CRITICAL: do not pass --procid "$SLURM_PROCID" directly to srun. The batch script
# runs on the controller/submit host where SLURM_PROCID is almost always 0, so the shell
# expands it once and EVERY rank gets --procid 0 → only work_0. Wrap in bash -c so
# each task expands $SLURM_PROCID on the compute node (0, 1, … NUM_NODES-1).
# Use absolute path to syn.py: compute nodes often cannot chdir to the job spool
# (see slurmstepd "couldn't chdir to /var/spool/slurmd/job…" → cwd /tmp), which
# makes "python syn.py" look for /tmp/syn.py and fail.
srun --ntasks="${NUM_NODES}" --ntasks-per-node=1 --cpus-per-task=${SLURM_CPUS_PER_TASK:-1} \
  bash -c 'echo "syn.py rank SLURM_PROCID=${SLURM_PROCID} node=${SLURMD_NODENAME} stride='"${NUM_NODES}"'" && exec python "'"${SCRIPT_DIR}"'/syn.py" --workers '"$((SLURM_CPUS_PER_TASK / 4))"' --stride '"${NUM_NODES}"' --procid "${SLURM_PROCID}"'

echo "----------------------------------------"
echo "Done"
exit
