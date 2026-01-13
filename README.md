---
pretty_name: ASAP7 Language of Test - Dataset Generation Pipeline
tags:
  - verilog
  - vlsi
  - asap7
  - structural-netlist
  - test-generation
  - atpg
  - fault-simulation
  - llm-training
  - synthesis
task_categories:
  - text-generation
license: apache-2.0
---

# ASAP7 Language of Test - Dataset Generation Pipeline

This repository contains the complete pipeline for generating the **ASAP7 Language of Test** dataset. The pipeline transforms **RTL Verilog designs** into **gate-level structural netlists**, generates **ATPG test patterns**, performs **fault simulation**, and creates **LLM training data** for fault-aware test generation.

The core hypothesis is that the relationship between **netlist structure**, **fault locations**, and **distinguishing test stimuli** forms a learnable "language of test" accessible to modern AI models.

---

# 1. Pipeline Overview

The pipeline consists of **three major stages**:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        STAGE 1: RTL → Structural Netlist                    │
│  run_syn.sh → syn.py → syn.tcl → formality.tcl                              │
│  [RTL Verilog] ──► [Synthesis] ──► [Equivalence Check] ──► [Gate Netlist]   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STAGE 2: Test Pattern & Fault Generation                 │
│  tmax.py → tmax.tcl                                                         │
│  [Gate Netlist] ──► [ATPG] ──► [Fault Sim] ──► [Patterns + Faults]          │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    STAGE 3: Dataset Preprocessing & Creation                │
│  run_fault_net_data_preprocessor.sh → fault_net_preprocessing.py            │
│                                     → data_preprocessing.py                 │
│  run_final_dataset_creation.sh → final_dataset_creation.py                  │
│  [Raw Data] ──► [Preprocessing] ──► [Fault Sim] ──► [LLM Training Data]     │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

# 2. RTL Source Datasets

The pipeline supports multiple RTL source datasets:

| Dataset | Source | Description |
|---------|--------|-------------|
| **FreeSet** | [SETH-TAMU/FreeSet-V1.0-LabUse](https://huggingface.co/datasets/SETH-TAMU/FreeSet-V1.0-LabUse) | Copyright-safe RTL Verilog modules (arXiv:2505.06096) |
| **MetRex** | [scale-lab/MetRex](https://huggingface.co/datasets/scale-lab/MetRex) | Additional RTL designs |
| **Shailja** | [shailja/Verilog_GitHub](https://huggingface.co/datasets/shailja/Verilog_GitHub) | GitHub-sourced Verilog |

---

# 3. Stage 1: RTL to Structural Netlist Conversion

## 3.1 Purpose
Transform behavioral RTL Verilog into gate-level structural netlists using industry-standard synthesis tools and the ASAP7 7nm PDK.

## 3.2 Tools Required
- **Synopsys Design Compiler** (`dc_shell`) - Logic synthesis
- **Synopsys Formality** (`fm_shell`) - Equivalence checking
- **Yosys** (optional) - Pre-synthesis validation

## 3.3 Files

| File | Description |
|------|-------------|
| `run_syn.sh` | SLURM batch script for parallel synthesis |
| `syn.py` | Python driver managing dataset loading and parallel job dispatch |
| `scripts/syn.tcl` | TCL script for Design Compiler synthesis |
| `scripts/formality.tcl` | TCL script for equivalence verification |

## 3.4 Process Flow

```
1. Load RTL designs from HuggingFace dataset (streaming mode)
2. For each design:
   a. Dump RTL to disk
   b. Identify top-level module automatically
   c. [Optional] Run Yosys pre-synthesis check
   d. Execute Design Compiler:
      - Read RTL
      - Link to ASAP7 libraries
      - Uniquify submodules
      - Flatten hierarchy (ungroup -all -flatten)
      - Compile with compile_ultra
      - Export structural Verilog + design statistics
   e. Run Formality equivalence check:
      - Compare RTL (reference) vs netlist (implementation)
      - Clean up on verification failure
3. Output: structural netlists + JSON metadata
```

## 3.5 Synthesis Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `LIBRARY` | `asap7sc7p5t_28` | ASAP7 standard cell library |
| `LIB_VARIANT` | `RVT` | Threshold voltage variant (LVT/RVT/SLVT) |
| `PVT_CORNER` | `TT` | Process-Voltage-Temperature corner (TT/SS/FF) |
| `DATASET` | `freeset` | Source RTL dataset |

## 3.6 Outputs

For each successfully synthesized design:
```
example_XXXXXX/
├── design.v                      # Original RTL
├── results/
│   ├── <module_name>.v           # Structural netlist
│   └── <module_name>_info.json   # Design statistics
├── reports/                      # Synthesis reports
├── dc_shell_run.log              # Design Compiler log
├── formality.log                 # Equivalence check log
└── yosys_check.log               # Pre-synthesis validation
```

## 3.7 Usage

```bash
# Interactive
export LIB_VARIANT=RVT PVT_CORNER=TT DATASET=freeset LIBRARY=asap7sc7p5t_28
python syn.py --workers 8 --stride 1 --procid 0

# SLURM batch
sbatch run_syn.sh RVT TT freeset asap7sc7p5t_28
```

---

# 4. Stage 2: Test Pattern and Fault Generation

## 4.1 Purpose
Generate ATPG test patterns for stuck-at faults and perform fault simulation to identify which faults each pattern detects.

## 4.2 Tools Required
- **Synopsys TetraMAX** (`tmax`) - ATPG and fault simulation

## 4.3 Files

| File | Description |
|------|-------------|
| `tmax.py` | Python driver for parallel TetraMAX execution |
| `scripts/tmax.tcl` | TCL script for ATPG and fault simulation |

## 4.4 Process Flow

```
1. Load structural netlists from Stage 1
2. For each netlist:
   a. Read ASAP7 standard cell libraries
   b. Read structural Verilog netlist
   c. Build simulation model
   d. Run Design Rule Check (DRC)
   e. Add all stuck-at faults (sa0/sa1)
   f. Run ATPG with -ndetects 1 (single detection per fault)
   g. Report patterns and faults
   h. For each pattern:
      - Run good machine simulation (fault-free)
      - Run bad machine simulation (with faults)
      - Record detected faults per pattern
3. Output: Test patterns, fault lists, simulation results
```

## 4.5 Fault Model

| Type | Description |
|------|-------------|
| **sa0** | Stuck-at-0: Net permanently tied to logic 0 |
| **sa1** | Stuck-at-1: Net permanently tied to logic 1 |

## 4.6 Outputs

For each design:
```
<module_name>/
├── drc.txt                           # Design Rule Check report
├── atpg.txt                          # ATPG summary
├── patterns.txt                      # Human-readable test patterns
├── faults.txt                        # All faults list
├── simulation.stil                   # STIL format patterns
├── simulation.v                      # Verilog testbench
└── simulation/
    ├── good/
    │   └── machine_<N>.txt           # Good machine outputs per pattern
    └── bad/
        ├── machine_faults_sim_<N>.txt    # Bad machine simulation
        └── machine_detected_faults_<N>.csv # Detected faults per pattern
```

## 4.7 Usage

```bash
# Set environment variables
export LIBRARY=asap7sc7p5t_28 LIB_VARIANT=RVT PVT_CORNER=TT

# Run TetraMAX
python tmax.py --output-dir ../data/freeset/out.freeset.asap7sc7p5t_28.rvt.tt \
               --verilog-files ../data/freeset/structural.v.freeset.asap7sc7p5t_28.rvt.tt \
               --jobs 8
```

---

# 5. Stage 3: Data Preprocessing and Final Dataset Creation

## 5.1 Purpose
Aggregate raw data, preprocess fault information, perform symbolic fault simulation, and generate LLM training data with chain-of-thought reasoning.

## 5.2 Sub-stages

### Sub-stage 3a: Fault/Net Preprocessing
Map TetraMAX instance pin names to actual net names and aggregate data.

### Sub-stage 3b: Final Dataset Creation
Perform symbolic fault simulation and generate training prompts.

## 5.3 Files

| File | Description |
|------|-------------|
| `run_fault_net_data_preprocessor.sh` | SLURM script for preprocessing |
| `fault_net_preprocessing.py` | Maps instance pins to net names |
| `data_preprocessing.py` | Aggregates data into CSV |
| `run_final_dataset_creation.sh` | SLURM script for final dataset |
| `final_dataset_creation.py` | Generates LLM training data |
| `vars.py` | Prompt templates and configurations |

## 5.4 Fault/Net Preprocessing (3a)

### Process Flow
```
1. For each structural netlist:
   a. Parse gate instances and pin connections
   b. Build mapping: instance/pin → net name
   c. Apply mapping to faults.txt
   d. Apply mapping to per-pattern fault files
2. Aggregate into CSV:
   - Module name
   - Full netlist
   - Gates-only netlist
   - Test patterns
   - Fault lists
   - ATPG summary
   - Statistics (instances, wires, inputs, outputs)
```

### Usage
```bash
# Rust mode (faster, supports sharding)
sbatch --array=0-3 run_fault_net_data_preprocessor.sh rust fault_net RVT TT freeset

# Python mode
sbatch --array=0 run_fault_net_data_preprocessor.sh python all RVT TT freeset
```

## 5.5 Final Dataset Creation (3b)

### Process Flow
```
1. Parse ASAP7 .lib files to extract gate functions
2. For each design in the aggregated CSV:
   a. Parse structural netlist into optimized representation
   b. For each test pattern:
      i.   Read detected faults for this pattern
      ii.  For each detected fault:
           - Perform symbolic fault simulation
           - Compute good machine values (fault-free)
           - Compute bad machine values (with fault)
           - Track fault propagation path
           - Generate snapshot table
      iii. Select random prompt templates
      iv.  Format training sample with:
           - System prompt
           - User prompt (netlist + fault)
           - Chain-of-thought reasoning
           - Answer (input vector, expected output, detected faults)
3. Output sharded Parquet files
4. Upload to Hugging Face Hub
```

### Symbolic Fault Simulation

The pipeline implements a fast symbolic fault simulation engine:

1. **Parse netlist** into instructions (assigns, gates)
2. **Initialize machines**:
   - Good machine: input values only
   - Bad machine: input values + faulty net stuck at 0/1
3. **Propagate values** through netlist:
   - Evaluate gate functions using sympy lambdify
   - Track fault propagation path (nets where good ≠ bad)
4. **Generate snapshot** showing all net values

### Usage
```bash
# Export gate function configuration
python final_dataset_creation.py --export_config sim_config.json

# Full dataset creation
sbatch run_final_dataset_creation.sh
```

---

# 6. Output Dataset Schema

Each row in the final dataset represents a **single fault detection scenario**:

| Column | Type | Description |
|--------|------|-------------|
| `system_content` | str | System prompt defining LLM behavior |
| `user_content` | str | User prompt with netlist and target fault |
| `reasoning_content` | str | Chain-of-thought reasoning steps |
| `answer_content` | str | Ground truth answer |
| `module_name` | str | Design module name |
| `netlist` | str | Full structural Verilog netlist |
| `fault` | str | Target fault (e.g., "sa0 net_name") |
| `input_vector` | JSON str | Input test vector as dict |
| `expected_output` | JSON str | Expected output as dict |
| `snapshot` | str | Markdown table of good/bad machine values |
| `detected_faults` | str | All faults detected by this pattern |

---

# 7. Example Training Sample

## User Prompt
```
Please formulate a test vector for the circuit "clk_edge_detector" to address the "sa0 clk" based on this netlist:
```verilog
module clk_edge_detector ( clk, temp_clk, posedge_detect, negedge_detect,
dualedge_detect );
input clk, temp_clk;
output posedge_detect, negedge_detect, dualedge_detect;
wire   n1;
INVxp33_ASAP7_75t_R U1 ( .A(temp_clk), .Y(n1) );
AND2x2_ASAP7_75t_R U2 ( .A(n1), .B(clk), .Y(posedge_detect) );
NOR2xp33_ASAP7_75t_R U3 ( .A(clk), .B(n1), .Y(negedge_detect) );
OR2x2_ASAP7_75t_R U5 ( .A(negedge_detect), .B(posedge_detect), .Y(
dualedge_detect) );
endmodule
```

## Chain-of-Thought Response
```
CHAIN_OF_THOUGHT:
1. **Netlist Examination**: Inspect the netlist to identify components and connections relevant to the sa0 clk.
2. **Fault Analysis**: Determine how the sa0 clk affects signal flow and circuit functionality.
3. **Simulate the Fault**: Perform a fault simulation to observe how the sa0 clk impacts the circuit's behavior.
4. **Input Vector Formulation**: Create an input vector that induces the sa0 clk by interacting with the affected components.
5. **Expected Output Specification**: Establish the expected output behavior when the sa0 clk is active.
6. **Simulation Execution**: Perform a simulation with the input vector to capture the circuit's behavior, resulting in a snapshot.
SNAPSHOT:
|  | Good Machine | Bad Machine |
|-|-|-|
| clk | 1 | 0 | ← Fault injected
| temp_clk | 0 | 0 |
| posedge_detect | 1 | 0 | ← Fault propagation 
| negedge_detect | 0 | 0 |
| dualedge_detect | 1 | 0 | ← Observable difference 
| n1 | 1 | 1 |
Is the simulation snapshot correct? If not, I will have to perform steps 1-6 again.
7. **Fault Detection Confirmation**: Compare the simulation snapshot with the expected output to verify the detection of the sa0 clk.

INPUT_VECTOR: "clk: 1, temp_clk: 0"
EXPECTED_OUTPUT: "posedge_detect: 1, negedge_detect: 0, dualedge_detect: 1"
DETECTED_FAULTS: "sa0 clk, sa0 posedge_detect, sa0 dualedge_detect"
```


---

# 8. Directory Structure

```
data_preprocessing/
├── run_syn.sh                        # Stage 1: Synthesis batch script
├── syn.py                            # Stage 1: Synthesis driver
├── tmax.py                           # Stage 2: TetraMAX driver
├── run_fault_net_data_preprocessor.sh # Stage 3a: Preprocessing batch
├── fault_net_preprocessing.py        # Stage 3a: Fault/net mapping
├── data_preprocessing.py             # Stage 3a: Data aggregation
├── run_final_dataset_creation.sh     # Stage 3b: Final dataset batch
├── final_dataset_creation.py         # Stage 3b: Training data generation
├── vars.py                           # Prompt templates
├── utils.py                          # Utility functions
├── scripts/
│   ├── syn.tcl                       # Design Compiler TCL
│   ├── formality.tcl                 # Formality TCL
│   └── tmax.tcl                      # TetraMAX TCL
├── lib/
│   └── asap7sc7p5t_28/
│       ├── DB/                       # Timing libraries (.db)
│       ├── LIB/CCS/                  # Liberty libraries (.lib)
│       └── Verilog/                  # Behavioral models (.v)
└── logs/                             # Job logs
```

---

# 9. Dependencies

## Python Packages
```
datasets
transformers
pandas
numpy
regex
sympy
tqdm
pyarrow
huggingface_hub
```

## EDA Tools
- Synopsys Design Compiler (dc_shell)
- Synopsys Formality (fm_shell)
- Synopsys TetraMAX (tmax)
- Yosys (optional, for pre-synthesis checks)

## Technology Library
- ASAP7 7nm PDK (Arizona State University)

---

# 10. Quick Start

```bash
# 1. Set environment
export DATASET=freeset
export LIBRARY=asap7sc7p5t_28
export LIB_VARIANT=RVT
export PVT_CORNER=TT

# 2. Run synthesis (Stage 1)
sbatch run_syn.sh $LIB_VARIANT $PVT_CORNER $DATASET $LIBRARY

# 3. Run ATPG (Stage 2)
python tmax.py \
  --output-dir ../data/$DATASET/out.$DATASET.$LIBRARY.$LIB_VARIANT.$PVT_CORNER \
  --verilog-files ../data/$DATASET/structural.v.$DATASET.$LIBRARY.$LIB_VARIANT.$PVT_CORNER

# 4. Run preprocessing (Stage 3a)
sbatch run_fault_net_data_preprocessor.sh python all $LIB_VARIANT $PVT_CORNER $DATASET $LIBRARY

# 5. Generate final dataset (Stage 3b)
sbatch run_final_dataset_creation.sh
```

---

# 11. Citation

If you use this pipeline or the resulting dataset, please cite:

```bibtex
@dataset{asap7_language_of_test_2025,
  title        = {ASAP7 Language of Test Dataset},
  author       = {Christos Vasileiou},
  year         = {2025},
  publisher    = {Hugging Face Datasets},
  url          = {https://huggingface.co/datasets/chrivasileiou/asap7-language-of-test}
}
```

```bibtex
@inproceedings{vasileiou2025llamas,
  title     = {Teaching Llamas to Test: A Language-Based Approach},
  author    = {Vasileiou, Christos and Makris, Yiorgos},
  booktitle = {IEEE International Test Conference},
  year      = {2025}
}
```

FreeSet source dataset:
```bibtex
@article{bush2025freeset,
  title  = {Free and Fair Hardware},
  author = {Bush, S. and others},
  journal= {arXiv:2505.06096},
  year   = {2025}
}
```

---

# 12. License

This pipeline and resulting datasets are released under the **Apache 2.0** license.

---

# 13. Contact

For questions or issues, please open a GitHub issue or contact the maintainers.
