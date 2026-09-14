import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circuit_split import audit_dataset, file_digest, group_circuits, netlist_identity, select_validation
from final_dataset_creation import process_per_row
from fault_sim import OptimizedNetlist
from netlist_utils import declared_nets
from pattern_mapping import read_pattern_mapping
from repaired_dataset import build_dataset
from fault_claims import verified_fault_claims, repair_completed_build

DECL = re.compile(r'^\s*(?P<kind>input|output|wire)\s*(?P<packed>\[\s*\d+\s*:\s*\d+\s*\])?\s*(?P<rest>[^;]+);', re.M)
NAME = re.compile(r'\s*(?P<name>\\\S+|[A-Za-z_]\w*)\s*(?P<unpacked>\[\s*\d+\s*:\s*\d+\s*\])?\s*')
GATES = {'BUF_X': {'Y': 'A'}, 'NAND_X': {'Y': '~(A & B)'}}


def stil(inputs, outputs, pi, po, index=0):
    def group(names):
        return ' + '.join(json.dumps(name) for name in names)
    return f'''STIL 1.0;
SignalGroups {{ "_pi" = '{group(inputs)}'; "_po" = '{group(outputs)}'; }}
Pattern "p" {{ "pattern {index}": Call "capture" {{ "_pi"={pi}; "_po"={po}; }} }}'''


def circuit(tmp_path, name='one', pi_name='a'):
    source = '17_' + name
    netlist = f'''module {name} ({pi_name}, y);
input {pi_name};
output y;
wire n;
BUF_X u0 (.A({pi_name}), .Y(n));
BUF_X u1 (.A(n), .Y(y));
endmodule'''
    folder = tmp_path / source
    (folder / 'simulation/bad').mkdir(parents=True)
    (folder / 'simulation.stil').write_text(stil([pi_name], ['y'], '1', 'H'))
    (folder / 'simulation/bad/machine_detected_faults_0.csv').write_text('sa0 DS y\nsa0 DS y\n')
    return dict(module_name=source, netlist=netlist, patterns='  0  1\n0 1 1', num_instances=2)


def test_stil_order_maps_bits_before_canonical_serialization():
    mapping = read_pattern_mapping(stil(['a[1]', 'a[0]'], ['scalar', 'out[1]', 'out[0]'], '10', 'HLH'))
    inputs, outputs = mapping.vectors(0, '10', '101', ['a[0]', 'a[1]'], ['out[0]', 'out[1]', 'scalar'])
    assert list(inputs.items()) == [('a[0]', 0), ('a[1]', 1)]
    assert list(outputs.items()) == [('out[0]', 1), ('out[1]', 0), ('scalar', 1)]


def test_original_ex102_pattern15_boolean_truth():
    # Regression from the original 4077_ex_102_test_vector_and pattern 15.
    ins = [f'{name}[{i}]' for name in ('a', 'b') for i in range(8, -1, -1)]
    outs = ['out1', 'out2'] + [f'out3[{i}]' for i in range(8, -1, -1)]
    mapping = read_pattern_mapping(stil(ins, outs, '110111100011111111', 'HHHLHLLLLHH', 15))
    iv, ov = mapping.vectors(15, '110111100011111111', '11101000011', sorted(ins), sorted(outs))
    truth = {f'out3[{i}]': 1 - (iv[f'a[{i}]'] & iv[f'b[{i}]']) for i in range(9)}
    truth.update(out1=1-int(all(iv[f'a[{i}]'] for i in range(9))),
                 out2=1-int(all(iv[f'b[{i}]'] for i in range(9))))
    assert ov == truth


@pytest.mark.parametrize('bad', [
    lambda s: s.replace('"a" + "b"', '"a" + "a"'),
    lambda s: s.replace('"a" + "b"', '"a" - "b"'),
    lambda s: s.replace('"_pi"=10', '"_pi"=1X'),
    lambda s: s.replace('"_po"=H', '"_po"=X'),
    lambda s: s.replace('"_pi"=10', '"_pi"=1'),
    lambda s: s.replace('Call "capture"', 'Call "scan"'),
])
def test_unsupported_stil_fails_closed(bad):
    with pytest.raises(ValueError):
        read_pattern_mapping(bad(stil(['a', 'b'], ['y'], '10', 'H')))


def test_vector_provenance_and_port_set_are_checked():
    mapping = read_pattern_mapping(stil(['a', 'b'], ['y'], '10', 'H'))
    for args in [(0, '01', '1', ['a', 'b'], ['y']),
                 (1, '10', '1', ['a', 'b'], ['y']),
                 (0, '10', '1', ['a', 'z'], ['y']),
                 (0, '10', '1', ['a', 'a'], ['y'])]:
        with pytest.raises(ValueError):
            mapping.vectors(*args)


def test_actual_indices_and_unpacked_arrays():
    text = 'input [9:8] a;\ninput [8:9] b;\ninput [1:0] c [4:3];\ninput \\escaped.name ;'
    assert declared_nets(text, 'input', DECL, NAME) == [
        'a[8]', 'a[9]', 'b[8]', 'b[9]', 'c[3][0]', 'c[3][1]', 'c[4][0]', 'c[4][1]', '\\escaped.name']
    mapping = read_pattern_mapping(stil(['\\escaped.name'], ['y'], '1', 'H'))
    assert mapping.inputs == ('\\escaped.name',)


def test_process_validates_simulation_and_keeps_gate_metadata(tmp_path):
    row = circuit(tmp_path)
    frame = process_per_row(row, tmp_path, GATES, DECL, NAME)
    assert len(frame) == 1  # duplicate net-level faults are not duplicated
    assert frame.iloc[0]['expected_output'] == {'y': 1}
    assert frame.iloc[0]['fault_propagation_gates'] == 'u1'
    assert frame.iloc[0]['module_name'] == 'one'
    opt = OptimizedNetlist(row['netlist'], GATES, DECL, NAME)
    assert all(len(instruction) == 6 for instruction in opt.instructions)
    assert opt.gate_metadata[0][:3] == ('BUF_X', 'u0', 'Y')
    (tmp_path / row['module_name'] / 'simulation.stil').write_text(stil(['a'], ['y'], '1', 'L'))
    row['patterns'] = '  0  1\n0 1 0'
    with pytest.raises(ValueError, match='disagree with custom simulation'):
        process_per_row(row, tmp_path, GATES, DECL, NAME)


def test_undetected_fault_and_missing_artifact_are_errors(tmp_path):
    row = circuit(tmp_path)
    path = tmp_path / row['module_name'] / 'simulation/bad/machine_detected_faults_0.csv'
    path.write_text('sa1 DS y\n')
    with pytest.raises(ValueError, match='not detected'):
        process_per_row(row, tmp_path, GATES, DECL, NAME)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        process_per_row(row, tmp_path, GATES, DECL, NAME)


def test_changed_internal_net_is_not_automatically_a_detected_fault():
    rows = [dict(fault='sa0 a', detected_faults='sa0 a, sa1 reconvergent, sa0 y'),
            dict(fault='sa0 y', detected_faults='sa0 y')]
    assert verified_fault_claims(rows) == ['sa0 a, sa0 y', 'sa0 y']


def test_unobserved_changed_branch_is_removed_from_generated_claims(tmp_path):
    row = circuit(tmp_path)
    row['netlist'] = '''module one(a,b,y);
input a,b;
output y;
wire n;
BUF_X u0 (.A(a), .Y(n));
NAND_X u1 (.A(a), .B(b), .Y(y));
endmodule'''
    row['patterns'] = '  0  1\n0 11 0'
    folder = tmp_path / row['module_name']
    (folder / 'simulation.stil').write_text(stil(['a', 'b'], ['y'], '11', 'L'))
    (folder / 'simulation/bad/machine_detected_faults_0.csv').write_text('sa0 DS a\nsa1 DS y\n')
    frame = process_per_row(row, tmp_path, GATES, DECL, NAME)
    target = frame[frame.fault == 'sa0 a'].iloc[0]
    snapshot = json.loads(target['snapshot'])
    assert snapshot['Good Machine']['n'] != snapshot['Bad Machine']['n']
    assert 'sa0 n' not in target['detected_faults']
    assert set(target['detected_faults'].split(', ')) == {'sa0 a', 'sa1 y'}


def test_duplicate_structure_and_same_module_variants_stay_together():
    a = 'module one(a,y); input a; output y; BUF_X u0 (.A(a),.Y(y)); endmodule'
    b = a.replace('one', 'alias').replace('u0', 'other_instance') + '// comment'
    assert netlist_identity(a) == netlist_identity(b)
    records = [dict(module_name='one', netlist_id=netlist_identity(a)),
               dict(module_name='alias', netlist_id=netlist_identity(b)),
               dict(module_name='alias', netlist_id='different structure')]
    group_circuits(records)
    assert len({r['circuit_id'] for r in records}) == 1


def test_complete_build_disjoint_and_tampering_rejected(tmp_path):
    rows = [circuit(tmp_path, 'one', 'a'), circuit(tmp_path, 'two', 'b')]
    csv = tmp_path / 'source.csv'
    pd.DataFrame(rows).to_csv(csv, index=False)
    args = SimpleNamespace(output_dir=tmp_path / 'build', csv_dataset=csv, workers=1,
                           tetramax_folder=tmp_path, seed=42, validation_circuits=1,
                           quarantine_invalid_circuits=False, sim_config=None)
    build_dataset(args, GATES, DECL, NAME)
    manifest = audit_dataset(args.output_dir)
    assert manifest['row_counts'] == {'train': 1, 'validation': 1}
    assert repair_completed_build(args.output_dir)['already_verified']
    assert select_validation(manifest['circuits'], 1, 42) == select_validation(list(reversed(manifest['circuits'])), 1, 42)
    with pytest.raises(FileExistsError):
        build_dataset(args, GATES, DECL, NAME)
    shard = next((args.output_dir / 'train').glob('*.parquet'))
    shard.write_bytes(shard.read_bytes() + b'corruption')
    with pytest.raises(ValueError, match='checksum'):
        audit_dataset(args.output_dir)


def test_reseal_stil_build_with_unverified_auxiliary_claims(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    rows = [circuit(tmp_path, 'one', 'a'), circuit(tmp_path, 'two', 'b')]
    csv = tmp_path / 'source.csv'
    pd.DataFrame(rows).to_csv(csv, index=False)
    args = SimpleNamespace(output_dir=tmp_path / 'build', csv_dataset=csv, workers=1,
                           tetramax_folder=tmp_path, seed=42, validation_circuits=1,
                           quarantine_invalid_circuits=False, sim_config=None)
    manifest = build_dataset(args, GATES, DECL, NAME)
    manifest.pop('fault_claims_version')
    relative = next(iter(manifest['shards']))
    path = args.output_dir / relative
    table = pq.read_table(path)
    table = table.set_column(table.schema.get_field_index('detected_faults'), 'detected_faults',
                             pa.array(['sa0 y, sa0 unobserved']))
    pq.write_table(table, path)
    manifest['shards'][relative]['sha256'] = file_digest(path)
    (args.output_dir / 'split_manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='claims have not been verified'):
        audit_dataset(args.output_dir)
    report = repair_completed_build(args.output_dir)
    assert report == {'changed_rows': 1, 'unverified_claims_removed': 1}
    assert audit_dataset(args.output_dir)['fault_claims_version'] == 'verified-net-faults-v1'
