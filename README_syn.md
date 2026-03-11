# Synthesis Pipeline Documentation

Three-file pipeline for large-scale hardware synthesis of RTL designs from the FreeSet dataset.

## Architecture

```
run_syn.sbatch → syn.py → scripts/syn.tcl
```

- **`run_syn.sbatch`** - Slurm job launcher (4 nodes, 16 CPUs/task, 64GB memory)
- **`syn.py`** - Main orchestration script for parallel processing
- **`scripts/syn.tcl`** - Design Compiler synthesis execution

## Workflow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           SLURM CLUSTER JOB LAUNCHER                            │
│                              (run_syn.sbatch)                                   │
└─────────────────────┬───────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 1. Resource Allocation (4 nodes, 4 tasks/node, 16 CPUs/task, 64GB memory)       │
│ 2. Environment Setup (Python env, LIB_VARIANT=RVT, PVT_CORNER=TT)               │
│ 3. Parallelization Setup (Calculate TOTAL_RANKS from SLURM_ARRAY_TASK_COUNT)    │
│ 4. Launch srun with Python processes across nodes                               │
└─────────────────────┬───────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        MAIN ORCHESTRATION SCRIPT                                │
│                              (syn.py)                                           │
└─────────────────────┬───────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ INITIALIZATION PHASE:                                                           │
│ • Parse command line arguments (workers, stride, offset)                        │
│ • Load FreeSet dataset in streaming mode                                        │
│ • Setup output directories (work_0, work_1, etc.)                               │
│ • Scan for previously completed examples                                        │
└─────────────────────┬───────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ DATASET PROCESSING LOOP (Parallel Execution):                                   │
│                                                                                 │
│ For each example in dataset slice:                                              │
│   ├─ Skip if already completed                                                  │
│   ├─ Extract RTL code to disk (design.v)                                        │
│   ├─ Detect top-level module name                                               │
│   ├─ Run synthesizability precheck (Yosys → DC fallback)                        │
│   ├─ Prepare synthesis environment                                              │
│   └─ Call Design Compiler with TCL script                                       │
└─────────────────────┬───────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        DESIGN COMPILER SYNTHESIS                                │
│                              (syn.tcl)                                          │
└─────────────────────┬───────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ SYNTHESIS EXECUTION:                                                            │
│ • Load technology libraries (.db files)                                         │
│ • Read RTL files (Verilog/SystemVerilog/VHDL)                                   │
│ • Auto-detect or use specified top-level design                                 │
│ • Link design with libraries                                                    │
│ • Apply synthesis constraints (dont_use, clock period)                          │
│ • Run compile_ultra with clock gating                                           │
│ • Generate output netlist and reports                                           │
└─────────────────────┬───────────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│ OUTPUT GENERATION:                                                              │
│ • Synthesized Verilog netlist (.v file)                                         │
│ • Timing reports (.rpt files)                                                   │
│ • Area and reference reports                                                    │
│ • Results stored in example_XXXXXX/results/ directory                           │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## Process Flow

### Phase 1: Job Launch
1. **Slurm Job Submission**: Request cluster resources (4 nodes, 16 CPUs/task, 64GB memory)
2. **Environment Setup**: Activate Python environment, set technology library parameters
3. **Parallelization**: Calculate total ranks from Slurm array task configuration
4. **Process Distribution**: Launch Python processes across nodes with unique task IDs

### Phase 2: Dataset Management
1. **Dataset Loading**: Load [FreeSet dataset](https://huggingface.co/datasets/SETH-TAMU/FreeSet-V1.0-LabUse) in streaming mode
2. **Directory Setup**: Create output directories for each job array task (work_0, work_1, etc.)
3. **Completion Detection**: Scan existing work directories for already synthesized designs
4. **Dataset Slicing**: Each job processes a specific slice using stride/offset logic

### Phase 3: RTL Processing
9. **Example Iteration**: Each worker processes its assigned dataset slice
10. **RTL Extraction**: Write Verilog code to disk as `design.v`
11. **Module Detection**: Auto-identify top-level module name
12. **Pre-synthesis Validation**: Yosys or Design Compiler synthesizability check
13. **Environment Setup**: Prepare synthesis environment variables

### Phase 4: Hardware Synthesis
14. **Library Loading**: Load technology library files (.db) into Design Compiler
15. **RTL Reading**: Read and parse Verilog/SystemVerilog/VHDL files
16. **Design Linking**: Link design with technology libraries
17. **Constraint Application**: Apply synthesis constraints (dont_use cells, clock period)
18. **Compilation**: Run `compile_ultra` with clock gating

### Phase 5: Output Generation
19. **Netlist Generation**: Write synthesized Verilog netlist to results directory
20. **Report Creation**: Generate timing, area, and reference reports
21. **Result Storage**: Store outputs in organized directory structure
22. **Process Completion**: Exit Design Compiler and complete worker process

## Key Features

- **Fault Tolerance**: Skips completed examples, handles synthesis failures gracefully
- **Scalability**: Uses Slurm job arrays and multi-node processing
- **Efficiency**: Streaming dataset loading and parallel processing
- **Flexibility**: Supports multiple RTL languages and auto top-level module detection
- **Robustness**: Pre-synthesis validation to avoid wasting time on invalid designs

## Output Structure

```
transformers_atpg
├── data_processing/
│   ├── scripts
│   │   └── syn.tcl
│   │── syn.py
│   └── run_syn.sbatch
└── data/work_X/
    ├── example_000001/
    |   ├── dc_shell_run.log
    |   ├── yosys_check.log
    │   ├── design.v                    # Original RTL
    │   ├── results/
    │   │   └── top_module.v           # Synthesized netlist
    │   └── reports/
    │       ├── top_module.rpt     # Timing report
    │       ├── top_module_area.rpt # Area report
    │       └── top_module_references.rpt # Reference report
    └── example_000002/
        └── ...
```

This pipeline provides scalable, fault-tolerant synthesis of thousands of RTL designs using high-performance computing resources.