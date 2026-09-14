"""Deterministic circuit grouping shared by dataset creation and SFT checks."""
import hashlib
import json
from pathlib import Path
import re


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def netlist_identity(netlist):
    """Group duplicate structures despite comments, layout and top/instance names.

    Port and internal net names are preserved. This is not Boolean equivalence
    checking or a graph-isomorphism test for arbitrary internal-net renaming.
    """
    tokens = re.findall(r'\\\S+|//[^\n]*|/\*.*?\*/|[A-Za-z_$][\w$]*|\d+|[^\s]', netlist, re.S)
    tokens = [t for t in tokens if not t.startswith(('//', '/*'))]
    if not tokens or tokens[0] != 'module' or tokens.count('module') != 1 or tokens[-1] != 'endmodule':
        raise ValueError('Expected a single structural module')
    # Header ordering is not an electrical difference for these named connections.
    body = ' '.join(tokens[tokens.index(';') + 1:-1])
    statements = []
    for statement in body.split(';'):
        statement = statement.strip()
        if not statement:
            continue
        statement = re.sub(r'^(\S+)\s+\S+\s+\(\s+\.', r'\1 INSTANCE ( .', statement)
        statements.append(statement)
    return digest('\n'.join(sorted(statements)))


def group_circuits(records):
    """Keep same-module variants and structural duplicates in one component."""
    parent = list(range(len(records)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    seen = {}
    for i, record in enumerate(records):
        for key in (('module', record['module_name']), ('netlist', record['netlist_id'])):
            if key in seen:
                parent[root(i)] = root(seen[key])
            seen[key] = i
    groups = {}
    for i, record in enumerate(records):
        groups.setdefault(root(i), []).append(record)
    for members in groups.values():
        circuit_id = digest('\n'.join(sorted({r['netlist_id'] for r in members})))
        for record in members:
            record['circuit_id'] = circuit_id
    return records


def select_validation(records, count, seed):
    """Reserve a few small, single-structure groups that survive the SFT gate filter."""
    groups = {}
    for record in records:
        if record.get('rows', 0):
            groups.setdefault(record['circuit_id'], []).append(record)
    candidates = [cid for cid, members in groups.items()
                  if len({r['netlist_id'] for r in members}) == 1
                  and all(2 <= r['num_instances'] <= 12 and r['num_instances'] != 5 for r in members)]
    candidates.sort(key=lambda cid: digest(f'{seed}:{cid}'))
    if count < 1 or count >= len(groups) or count > len(candidates):
        raise ValueError(f'Cannot reserve {count} validation circuits from {len(candidates)} eligible groups')
    return set(candidates[:count])


def audit_dataset(directory, *, require_verified_fault_claims=True):
    """Verify completed build, file contents, row identities and split membership.

    Called before SFT loads a model. Never trust just a split's name or a manifest
    assertion: inspect the actual parquet identity columns and hash every shard.
    """
    import pyarrow.parquet as pq
    directory = Path(directory)
    if (directory / 'INCOMPLETE').exists():
        raise ValueError('Dataset build is incomplete')
    manifest = json.loads((directory / 'split_manifest.json').read_text())
    if manifest.get('mapping_version') != 'stil-signal-groups-v1':
        raise ValueError('Dataset is not a verified STIL-mapped build')
    if require_verified_fault_claims and manifest.get('fault_claims_version') != 'verified-net-faults-v1':
        raise ValueError('Additional detected-fault claims have not been verified')
    expected = {r['source_module_name']: r for r in manifest['circuits'] if r.get('rows', 0)}
    memberships = {split: {'circuit_id': set(), 'netlist_id': set(), 'module_name': set()}
                   for split in ('train', 'validation')}
    counts = {'train': 0, 'validation': 0}
    source_counts = {}
    actual_paths = {str(p.relative_to(directory)) for split in memberships for p in (directory / split).glob('*.parquet')}
    if actual_paths != set(manifest['shards']):
        raise ValueError('Parquet file list differs from manifest')
    for relative, metadata in manifest['shards'].items():
        path = directory / relative
        if path.resolve().parent not in ((directory / 'train').resolve(), (directory / 'validation').resolve()):
            raise ValueError('Invalid shard path in manifest')
        if file_digest(path) != metadata['sha256']:
            raise ValueError(f'Shard checksum mismatch: {relative}')
        split = Path(relative).parts[0]
        identities = ['source_module_name', 'module_name', 'circuit_id', 'netlist_id', 'mapping_version']
        table = pq.read_table(path, columns=identities)
        counts[split] += table.num_rows
        for row in table.to_pylist():
            record = expected.get(row['source_module_name'])
            if record is None or record['split'] != split or any(row[k] != record[k] for k in identities[1:-1]):
                raise ValueError(f'Row has incorrect circuit identity or split: {row}')
            if row['mapping_version'] != manifest['mapping_version']:
                raise ValueError('Mixed mapping versions')
            source_counts[row['source_module_name']] = source_counts.get(row['source_module_name'], 0) + 1
            for key in memberships[split]:
                memberships[split][key].add(row[key])
    if source_counts != {name: r['rows'] for name, r in expected.items()}:
        raise ValueError('Circuit row counts differ from manifest')
    for key in memberships['train']:
        if memberships['train'][key] & memberships['validation'][key]:
            raise ValueError(f'Train/validation leakage: {key}')
    if counts != manifest['row_counts'] or not all(counts.values()):
        raise ValueError('Empty split or incorrect row counts')
    return manifest
