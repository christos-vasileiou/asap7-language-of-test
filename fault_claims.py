"""Keep additional fault claims grounded in independently verified target rows."""
FAULT_CLAIMS_VERSION = 'verified-net-faults-v1'


def verified_fault_claims(records):
    """Return conservative, non-exhaustive claims for records of one input pattern.

    The caller has already simulated every record's target fault and verified PO
    detection. A changed internal net alone is not evidence of detection when
    that net is independently faulted (especially with reconvergent fanout).
    """
    verified = {record['fault'] for record in records}
    claims = []
    for record in records:
        candidates = record['detected_faults'].split(', ')
        accepted = list(dict.fromkeys(fault for fault in candidates if fault in verified))
        if record['fault'] not in accepted:
            accepted.insert(0, record['fault'])
        claims.append(', '.join(accepted))
    return claims


def repair_completed_build(directory):
    """Reseal a local pre-fix build using its already verified target rows.

    This migration is only for the STIL-checked builder's outputs, not old Hub
    data: it audits the existing manifest, groups by source + pattern + vector,
    updates auxiliary claims, recomputes checksums, then audits again.
    """
    import json
    from pathlib import Path
    from collections import defaultdict
    import pyarrow as pa
    import pyarrow.parquet as pq
    from circuit_split import audit_dataset, file_digest

    directory = Path(directory)
    manifest = audit_dataset(directory, require_verified_fault_claims=False)
    if manifest.get('fault_claims_version') == FAULT_CLAIMS_VERSION:
        return {'changed_rows': 0, 'already_verified': True}
    incomplete = directory / 'INCOMPLETE'
    incomplete.write_text('Verifying auxiliary fault claims.\n')
    changed = removed = 0
    for relative, metadata in manifest['shards'].items():
        path = directory / relative
        table = pq.read_table(path)
        rows = table.select(['source_module_name', 'pattern_index', 'input_vector', 'fault', 'detected_faults']).to_pylist()
        groups = defaultdict(list)
        for index, row in enumerate(rows):
            groups[(row['source_module_name'], row['pattern_index'], row['input_vector'])].append(index)
        result = [row['detected_faults'] for row in rows]
        for indices in groups.values():
            for index, value in zip(indices, verified_fault_claims([rows[i] for i in indices]), strict=True):
                if result[index] != value:
                    changed += 1
                    removed += len(set(result[index].split(', ')) - set(value.split(', ')))
                    result[index] = value
        table = table.set_column(table.schema.get_field_index('detected_faults'), 'detected_faults', pa.array(result))
        temporary = path.with_suffix('.parquet.tmp')
        pq.write_table(table, temporary)
        temporary.replace(path)
        metadata['sha256'] = file_digest(path)
    manifest['fault_claims_version'] = FAULT_CLAIMS_VERSION
    manifest['fault_claims_repair'] = {'changed_rows': changed, 'unverified_claims_removed': removed}
    temporary = directory / 'split_manifest.json.tmp'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(directory / 'split_manifest.json')
    incomplete.unlink()
    try:
        audit_dataset(directory)
    except Exception:
        incomplete.write_text('Auxiliary-fault audit failed.\n')
        raise
    return manifest['fault_claims_repair']
