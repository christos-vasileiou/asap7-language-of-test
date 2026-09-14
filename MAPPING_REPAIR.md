# STIL mapping repair and validation before SFT

This repair reads the existing ATPG artifacts and uses `fast_fault_sim` as the
verifier. It does not invoke TetraMAX during dataset construction or SFT.

## Source-to-training contract

1. Synthesis produces structural Verilog. `scripts/tmax.tcl` exports the source
   `patterns.txt`, `simulation.stil`, and per-pattern detected-fault reports.
2. `fault_net_preprocessing.py` / the Rust preprocessor replace instance/pin fault
   locations with connected net names. This stage edits the original reports.
   **It was not rerun for this repair.** Pin faults and net-stem faults can have
   different behavior when a signal fans out; a translated `DS` status alone
   does not prove detection under the custom simulator's net-fault model.
3. The aggregation stage embeds a two-column pattern table and structural
   Verilog in `dataset.<suffix>.csv`. Its pattern index addresses
   `simulation/bad/machine_detected_faults_<index>.csv`.
4. `final_dataset_creation.py` now reads the original STIL `_pi` and `_po`
   groups. It maps each bit by that group's position, then serializes the named
   values in the simulator's canonical port order. Changing the serialization
   order alone would not repair the bit-to-signal association.
5. The original CSV and STIL pattern indices, PI bits and PO bits must agree.
   `H`/`L` output waveforms become `1`/`0`. The supported format is one unnamed
   `SignalGroups` block with explicit quoted signal sums and numbered
   `Call "capture"` records. Group arithmetic, unknown/high-impedance waveforms,
   scan procedures, duplicate signals, missing signals, mismatched widths and
   missing artifacts are rejected; bits are never silently removed or truncated.
6. Every retained fault is simulated. All primary outputs must resolve to binary
   values, the good-machine outputs must exactly match the STIL-derived label,
   and at least one canonical primary output must differ in the bad machine.
   Duplicate translated net-fault records within a pattern are deduplicated.
   Additional `detected_faults` claims are restricted to independently verified
   target faults for that exact source circuit, pattern and input vector. This
   list is conservative and non-exhaustive. A changing internal net is not enough
   to establish an independently detectable fault: an audit found 553 false
   claims among 18,105 distinct circuit/pattern/fault checks in 20 sampled circuits.
   The manifest must also contain `fault_claims_version=verified-net-faults-v1`.
7. The builder emits the original SFT fields plus `source_module_name`,
   `pattern_index`, `mapping_version`, `netlist_id` and `circuit_id`. Explanations
   use separate gate metadata; compiled simulator instructions retain their
   existing execution format. Port expansion now preserves nonzero bus bounds
   and places unpacked indices before packed indices.

The regression case `4077_ex_102_test_vector_and`, pattern 15, has PI bits
`110111100011111111` and PO bits `11101000011`. Its PO order is
`out1, out2, out3[8], ..., out3[0]`. The repaired mapping agrees with an independent
Boolean NAND calculation, as well as the custom simulator. The former mapping
instead attached these bits to ascending `out3` bits followed by the scalars.

## Local rebuild

Run from the workspace root, using the environment containing pandas, SymPy,
pyarrow and regex:

```bash
DATA_PATH="$PWD/data" DATASET=freeset LIBRARY=asap7sc7p5t_28 \
LIB_VARIANT=RVT PVT_CORNER=TT \
/work/cxv200006/myenv/bin/python data_preprocessing/final_dataset_creation.py \
  --sim_config data_preprocessing/sim_config.json \
  --output_dir data/freeset/dataset.freeset.asap7sc7p5t_28.rvt.tt.stil_repaired_v1 \
  --workers 8 --validation_circuits 8 --seed 20260910 \
  --quarantine_invalid_circuits
```

The output directory **must not exist**. A failed build retains `INCOMPLETE` and
cannot pass the SFT audit. There is no automatic upload. Existing dataset shards,
source artifacts, checkpoints and online datasets are preserved. Without
`--quarantine_invalid_circuits`, a validation error stops the build; with it, all
examples from the affected source circuit are excluded and its first failure is
recorded in `rejections.jsonl` and `split_manifest.json`. This policy favors
consistent supervision and makes the exclusion visible; it can remove valid
examples from a circuit that also contains an invalid fault record.

The existing `0 < num_instances < 100` input scope is retained. The manifest
records source CSV and STIL hashes, simulator gate-function identity, circuit
assignments, shard checksums, accepted row counts and rejected source circuits.
`validation_netlists/` contains readable copies of the reserved netlists.

## Circuit-disjoint validation

Grouping happens before any train/validation assignment. Records with the same
actual Verilog module name or duplicate normalized structure are joined into one
connected group, including transitive links through aliases. Normalization ignores
comments, whitespace, statement order, top-module names and gate-instance names.
Port and internal-net names remain significant: this is not Boolean-equivalence
checking or arbitrary graph-isomorphism detection. Same-module grouping is
conservative and can group unrelated designs named `top` together.

The default reserves eight deterministic groups, with one unique normalized
netlist per selected group. Candidates have 2–12 instances, excluding the
existing SFT gate-count exclusion of five. This produces a small development
validation set likely to survive the 2048-token prompt limit. It is not a broad
evaluation of large-circuit generalization. All patterns and faults belonging
to each selected group go to `validation/`; none go to `train/`.

Before loading SFT weights, `check_sft_dataset` hashes every shard, inspects row
identities and verifies zero overlap in circuit IDs, normalized netlist IDs and
module names. It also rejects incomplete builds and resumes without matching
repaired-dataset provenance. Checksums protect a completed build against later
file changes; they do not substitute for the simulator checks during creation.

Use the new configuration, leaving the original experiment configuration intact:

```bash
cd atpgllm
./scripts/train/submit_training_code.sh configs/sft_granite_4.2_8b_repaired.conf
```

The configuration starts from the **base model**, with no old SFT adapter or
stream offset. An earlier SFT checkpoint trained on the unsplit dataset cannot
make these circuits unseen retroactively. Independence from base-model
pretraining cannot be established here.

The SFT loader chooses up to eight distinct faults per validation circuit in a
fixed order, applies the same prompt/gate filters as training, and excludes
validation sequences that would be truncated. Losing an entire held-out circuit
is an error. It writes `sft_data_manifest.json` in the new run directory and logs
validation loss at step zero and every ten updates, retaining the best saved
checkpoint by validation loss. This is teacher-forced loss; generated ATPG
success still needs evaluation using the custom simulator. If every trained
checkpoint is worse than the step-zero baseline, do not select one merely
because it is the best among the saved checkpoints.

Future GRPO training must consume only this dataset's `train` split to preserve
the holdout. The old GRPO evaluation manifest was selected after the original
SFT training and cannot establish an SFT generalization gap.

## Verification

`tests/test_dataset_mapping.py` exercises original bit-order regression data,
independent Boolean truth, malformed STIL, index/width/port checks, escaped names,
actual bus bounds, simulator agreement, failed detection, gate metadata,
duplicate grouping, complete local dataset creation and tamper detection.
`atpgllm/tests/unit/test_sft_validation.py` covers deterministic balanced
selection, filtered-out circuits, checkpoint provenance and evaluation settings.
The rebuild's measured counts and tokenizer checks are recorded alongside its
manifest in `build_validation_report.json` after completion.

## Completed rebuild counts

The local rebuild retained 2,247 of the 3,388 source circuits in the existing
gate-count scope. It contains 986,853 training rows and 432 validation rows.
Validation reserves eight normalized circuit groups, represented by ten source
variants; all variants in each reserved group are excluded from training.

The 1,141 exclusions consist of 1,000 circuits with at least one reported `DS`
fault that the custom simulator did not detect, 136 missing pattern tables,
three unsupported/nonbinary STIL captures, one multi-module source, and one
missing per-pattern fault report. These counts classify the first failure per
source circuit, not every bad example within it.

The auxiliary-claim repair removed 292,357 unverified claims from 151,650 rows.
It preserves every retained row's input vector, expected output, target fault,
and simulation snapshot. The completed shard audit found zero train/validation
overlap in circuit IDs, normalized netlist IDs and actual module names.

Verification includes 17 preprocessing tests, 39 training-related tests, a CPU
SFT smoke run with evaluation at steps zero and one and best-checkpoint loading,
and a launcher dry run. The production SFT job and any Hub publication remain
unlaunched; the new SFT configuration points directly at the local repaired build.
