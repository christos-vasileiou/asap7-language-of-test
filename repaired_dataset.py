"""Conservative local build: validate circuits, reserve groups, publish no data."""
from functools import partial
import json
import multiprocessing as mp
from pathlib import Path
import random

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

from circuit_split import digest, file_digest, group_circuits, netlist_identity, select_validation, audit_dataset
from netlist_utils import verify_module_name
from pattern_mapping import MAPPING_VERSION
from fault_claims import FAULT_CLAIMS_VERSION


def _process(record, tetramax_folder, gate_func, decl_re, name_re, seed):
    from final_dataset_creation import process_per_row
    if record.get('preflight_error'):
        return None, record['preflight_error']
    random.seed(digest(f"{seed}:{record['module_name']}"))
    try:
        frame = process_per_row(record, tetramax_folder, gate_func, decl_re, name_re)
        return frame, None
    except (ValueError, KeyError, FileNotFoundError, pd.errors.ParserError) as error:
        return None, f'{type(error).__name__}: {error}'


def build_dataset(args, gate_func, decl_re, name_re):
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)  # Never delete/overwrite an earlier build.
    (output / 'INCOMPLETE').write_text('Do not train from this directory until the build succeeds.\n')
    stage = output / 'staging'
    stage.mkdir()
    records = []
    for chunk in pd.read_csv(args.csv_dataset, chunksize=100):
        # Preserve the existing gate-count scope; missing required data is an error.
        chunk = chunk[(chunk.num_instances > 0) & (chunk.num_instances < 100)]
        records.extend(chunk.to_dict('records'))
    if len({r['module_name'] for r in records}) != len(records):
        raise ValueError('Duplicate source module identifiers in input CSV')
    metadata = []
    for record in records:
        module_name, _ = verify_module_name(record['netlist'], record['module_name'])
        try:
            netlist_id = netlist_identity(record['netlist'])
        except ValueError as error:
            if not args.quarantine_invalid_circuits:
                raise
            record['preflight_error'] = str(error)
            netlist_id = 'unsupported:' + digest(record['netlist'])
        metadata.append(dict(source_module_name=record['module_name'], module_name=module_name,
                             netlist_id=netlist_id,
                             num_instances=int(record['num_instances'])))
    group_circuits(metadata)  # Include invalid variants too when establishing identity.
    worker = partial(_process, tetramax_folder=Path(args.tetramax_folder), gate_func=gate_func,
                     decl_re=decl_re, name_re=name_re, seed=args.seed)
    rejected = []
    pool = mp.Pool(args.workers) if args.workers > 1 else None
    try:
        iterator = pool.imap(worker, records) if pool else map(worker, records)
        for index, (frame, error) in enumerate(tqdm(iterator, total=len(records), desc='Validating circuits')):
            info = metadata[index]
            if error or frame.empty:
                info['rows'] = 0
                info['rejection'] = error or 'No detected-fault examples'
                rejected.append(info.copy())
                with (output / 'rejections.jsonl').open('a') as stream:
                    stream.write(json.dumps(info) + '\n')
                if error and not args.quarantine_invalid_circuits:
                    raise ValueError(info['rejection'])
                continue
            for key in ('circuit_id', 'netlist_id', 'module_name'):
                frame[key] = info[key]
            for column in ('input_vector', 'expected_output'):
                frame[column] = frame[column].map(json.dumps)
            info['rows'] = len(frame)
            source = Path(args.tetramax_folder) / info['source_module_name'] / 'simulation.stil'
            info['stil_sha256'] = file_digest(source)
            pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), stage / f'{index:05d}.parquet')
    finally:
        if pool:
            pool.terminate()
            pool.join()
    validation = select_validation(metadata, args.validation_circuits, args.seed)
    counts = {'train': 0, 'validation': 0}
    shards = {}
    for split in counts:
        (output / split).mkdir()
    for index, info in enumerate(metadata):
        if not info['rows']:
            continue
        split = 'validation' if info['circuit_id'] in validation else 'train'
        info['split'] = split
        relative = f'{split}/data-{index:05d}.parquet'
        destination = output / relative
        (stage / f'{index:05d}.parquet').rename(destination)
        counts[split] += info['rows']
        shards[relative] = {'sha256': file_digest(destination), 'rows': info['rows']}
    stage.rmdir()
    manifest = dict(schema_version=1, mapping_version=MAPPING_VERSION,
                    fault_claims_version=FAULT_CLAIMS_VERSION, seed=args.seed,
                    source_csv=str(Path(args.csv_dataset).resolve()), source_csv_sha256=file_digest(args.csv_dataset),
                    sim_config=str(Path(args.sim_config).resolve()) if args.sim_config else 'Liberty extraction',
                    gate_functions_sha256=digest(json.dumps(
                        {cell: {pin: str(data['function']) if isinstance(data, dict) else str(data) for pin, data in pins.items()}
                         for cell, pins in gate_func.items()}, sort_keys=True)),
                    validation_circuits=args.validation_circuits, row_counts=counts,
                    rejected_circuits=len(rejected), circuits=metadata, shards=shards)
    (output / 'split_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'INCOMPLETE').unlink()
    try:
        audit_dataset(output)
    except Exception:
        (output / 'INCOMPLETE').write_text('Final audit failed.\n')
        raise
    export_validation_netlists(output)
    (output / 'README.md').write_text(
        '---\nconfigs:\n- config_name: default\n  data_files:\n'
        '  - split: train\n    path: train/*.parquet\n'
        '  - split: validation\n    path: validation/*.parquet\n---\n\n'
        '# Repaired ATPG dataset\n\n'
        'PI/PO bits are mapped by original STIL groups, then checked with the custom simulator. '
        'Entire circuit groups are reserved before SFT. See split_manifest.json for identities, '
        'source hashes and rejected circuits. This does not establish Boolean inequivalence '
        'between circuits or independence from base-model pretraining. Start SFT from the base '
        'model; old SFT adapters have already seen the old dataset.\n')
    print(json.dumps(dict(output_dir=str(output), row_counts=counts,
                         validation_circuits=len(validation), rejected_circuits=len(rejected)), indent=2))
    return manifest


def export_validation_netlists(directory):
    """Export the held-out source netlists for inspection without loading SFT."""
    directory = Path(directory)
    folder = directory / 'validation_netlists'
    folder.mkdir(exist_ok=True)
    for path in sorted((directory / 'validation').glob('*.parquet')):
        for row in pq.read_table(path, columns=['netlist_id', 'netlist']).slice(0, 1).to_pylist():
            target = folder / f"{row['netlist_id']}.v"
            if target.exists() and target.read_text() != row['netlist']:
                # Equivalent normalized source variants may differ in comments/names.
                continue
            target.write_text(row['netlist'])
