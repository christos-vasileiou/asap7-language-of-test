"""Opt-in licensed tests: RUN_TETRAMAX_TESTS=1; run serially, never with xdist."""
import os
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tetramax_backend import make_request, simulate
from test_tetramax_runtime import DESIGN, CELLS

pytestmark = pytest.mark.skipif(os.environ.get('RUN_TETRAMAX_TESTS') != '1', reason='Requires an explicitly authorized licensed TetraMAX run')

@pytest.fixture
def native(tmp_path, monkeypatch):
    lib=tmp_path/'cells.v'
    lib.write_text(CELLS)
    monkeypatch.setenv('CELL_LIBS_VERILOG',str(lib))
    monkeypatch.setenv('TMAX_TIMEOUT_S','30')
    monkeypatch.delenv('TMAX_SERVER_URL',raising=False)
    monkeypatch.delenv('TMAX_SERVER_FILE',raising=False)
    # Keep the project pool: tests must share the total allocation with all jobs.
    return lambda design,iv,ov,fault: simulate(make_request(design,iv,ov,fault))


def test_native_detect_nondetect_and_no_stale_state(native):
    detected=native(DESIGN,{'a':1,'b':1},{'y':0,'z':0},'sa0 n')
    assert detected['detected'] and detected['native_fault']=='U1/Y'
    assert detected['values']['n']==[1,0]
    assert detected['values']['y']==[1,0]
    assert detected['values']['z']==[0,1]
    negative=native(DESIGN,{'a':0,'b':1},{'y':1,'z':0},'sa0 n')
    assert not negative['detected']
    assert negative['values']['y']==[0,0]
    repeated=native(DESIGN,{'a':1,'b':1},{'y':1,'z':1},'sa0 n')
    assert repeated['cache_hit'] and repeated['identity']==detected['identity']
    opposite=native(DESIGN,{'a':0,'b':1},{'y':1,'z':0},'sa1 n')
    assert opposite['detected'] and opposite['values']['y']==[0,1]


def test_native_stem_vs_branch(native):
    branch=native(DESIGN,{'a':1,'b':1},{'y':0,'z':0},'sa0 pin:U2/A')
    assert branch['detected'] and branch['native_fault']=='U2/A'
    assert branch['values']['y']==[1,0]
    assert branch['values']['z']==[0,0]  # Other fanout is unaffected.


@pytest.mark.parametrize('driver', ['pi', 'cell'])
def test_native_assigned_output_uses_stem_driver(native, driver):
    if driver == 'pi':
        design = 'module design(input a,output y,output z); assign y=a; BUF U1(.A(a),.Y(z)); endmodule'
        expected_driver = 'a'
    else:
        design = 'module design(input a,output y,output z); wire n; BUF U0(.A(a),.Y(n)); assign y=n; BUF U1(.A(n),.Y(z)); endmodule'
        expected_driver = 'U0/Y'
    result = native(design, {'a':1}, {'y':0,'z':0}, 'sa0 y')
    assert result['detected'] and result['native_fault'] == expected_driver
    # Both fanouts must see the fault, not only the aliased primary output.
    assert result['values']['y'] == [1,0]
    assert result['values']['z'] == [1,0]
    negative = native(design, {'a':0}, {'y':0,'z':0}, 'sa0 y')
    assert not negative['detected']


def test_native_recorded_gray_decode_alias(monkeypatch):
    monkeypatch.delenv('CELL_LIBS_VERILOG', raising=False)
    monkeypatch.delenv('TMAX_SERVER_URL', raising=False)
    monkeypatch.delenv('TMAX_SERVER_FILE', raising=False)
    design = (Path(__file__).parent / 'fixtures/tetramax_gray_decode.v').read_text()
    inputs = {f'gray[{i}]':int(value) for i,value in enumerate('10101010')}
    outputs = {f'gray_decode[{i}]':0 for i in range(8)}
    result = simulate(make_request(design, inputs, outputs, 'sa1 gray_decode[7]'))
    assert result['detected'] and result['native_fault'] == 'gray[7]'
    expected_good = [0,1,1,0,0,1,1,0]
    for i,good in enumerate(expected_good):
        assert result['values'][f'gray_decode[{i}]'] == [good,1-good]


@pytest.mark.parametrize('driver', ['pi', 'cell'])
def test_native_internal_alias_chain_faults_entire_stem(native, driver):
    prefix = 'assign source=a;' if driver == 'pi' else 'BUF U0(.A(a),.Y(source));'
    design = ('module design(input a,output y,output z); wire source,alias1,alias2; '
              + prefix + ' assign alias2=alias1; assign alias1=source; '
              'BUF U1(.A(alias2),.Y(y)); BUF U2(.A(source),.Y(z)); endmodule')
    for bit in (0, 1):
        result = native(design, {'a':bit}, {'y':0, 'z':0}, 'sa0 alias2')
        assert result['native_fault'] == ('a' if driver == 'pi' else 'U0/Y')
        assert result['detected'] == bool(bit)
        for name in ('source', 'alias1', 'alias2', 'y', 'z'):
            assert result['values'][name] == [bit, 0]


def test_native_recorded_alu_decoder_internal_alias(monkeypatch):
    from tetramax_backend import SimulationNetlist
    monkeypatch.delenv('CELL_LIBS_VERILOG', raising=False)
    monkeypatch.delenv('TMAX_SERVER_URL', raising=False)
    monkeypatch.delenv('TMAX_SERVER_FILE', raising=False)
    design = (Path(__file__).parent/'fixtures/tetramax_alu_decoder.v').read_text()
    model = SimulationNetlist(design)
    # Exact pattern and fault saved from the failed training tool call.
    inputs = {f'inst[{i}]': int(bit) for i, bit in enumerate('10100000000000100000000000000000')}
    outputs = dict.fromkeys(model.output_nets, 0)
    result = simulate(make_request(design, inputs, outputs, 'sa1 inst_13'))
    reference = simulate(make_request(design, inputs, outputs, 'sa1 inst[13]'))
    assert result['native_fault'] == 'inst[13]'
    assert result['values']['inst_13'] == [0, 1]
    assert result['values'] == reference['values']
    assert result['fault_status'] == reference['fault_status']
    # Compare all four aliases with their explicit PI targets, including both
    # stuck polarities and activated/non-activated patterns.
    for bit in (0, 1):
        inputs = dict.fromkeys(model.input_nets, bit)
        outputs = dict.fromkeys(model.output_nets, 0)
        for index in (12, 13, 14, 30):
            for stuck in (0, 1):
                alias = f'inst_{index}'
                source = f'inst[{index}]'
                result = simulate(make_request(design, inputs, outputs, f'sa{stuck} {alias}'))
                reference = simulate(make_request(design, inputs, outputs, f'sa{stuck} {source}'))
                assert result['native_fault'] == source
                assert result['values'][alias] == result['values'][source] == [bit, stuck]
                assert result['values'] == reference['values']
                assert result['fault_status'] == reference['fault_status']


def test_native_escaped_wire_and_bus_bit_stay_distinct(native):
    design = r'''module design(input a,input b,output [0:0] y,output z);
wire \y[0] ;
BUF U0(.A(a),.Y(y[0]));
BUF U1(.A(b),.Y(\y[0] ));
BUF U2(.A(\y[0] ),.Y(z));
endmodule'''
    vector = {'a':1,'b':0}
    outputs = {'y[0]':0,'z':0}
    bit_fault = native(design, vector, outputs, 'sa0 y[0]')
    assert bit_fault['detected'] and bit_fault['native_fault'] == 'U0/Y'
    assert bit_fault['values']['y[0]'] == [1,0]
    assert bit_fault['values'][r'\y[0]'] == [0,0]
    assert bit_fault['values']['z'] == [0,0]
    literal_fault = native(design, vector, outputs, r'sa1 \y[0]')
    assert literal_fault['detected'] and literal_fault['native_fault'] == 'U1/Y'
    assert literal_fault['values']['y[0]'] == [1,1]
    assert literal_fault['values'][r'\y[0]'] == [0,1]
    assert literal_fault['values']['z'] == [0,1]


def test_native_neuron_ram_escaped_net(monkeypatch):
    from tetramax_backend import SimulationNetlist
    monkeypatch.delenv('CELL_LIBS_VERILOG', raising=False)
    monkeypatch.delenv('TMAX_SERVER_URL', raising=False)
    monkeypatch.delenv('TMAX_SERVER_FILE', raising=False)
    design = (Path(__file__).parent/'fixtures/tetramax_neuron_ram.v').read_text()
    model = SimulationNetlist(design)
    inputs = dict.fromkeys(model.input_nets, 0)
    outputs = dict.fromkeys(model.output_nets, 0)
    for fault in ('sa1 weights_out[15]', r'sa1 \weights_out[15]'):
        result = simulate(make_request(design, inputs, outputs, fault))
        assert result['detected'] and result['native_fault'] == 'U5/L'
        assert all(result['values'][f'weights_out[{i}]'] == [0,1] for i in range(16))
        assert result['values']['loaded'] == [1,1]


def test_native_bus_order_and_escaped_net(native):
    design=r'''module actual_top(input [1:0] a, output [0:1] y);
wire \odd$name ;
BUF U0(.A(a[0]),.Y(\odd$name ));
BUF U1(.A(\odd$name ),.Y(y[1]));
BUF U2(.A(a[1]),.Y(y[0]));
endmodule'''
    result=native(design,{'a[0]':1,'a[1]':0},{'y[0]':1,'y[1]':0},r'sa0 \odd$name')
    assert result['detected']
    assert result['values']['y[0]']==[0,0]
    assert result['values']['y[1]']==[1,0]


def test_native_unknown_outputs_are_masked(native):
    design="module design(input a,output y,output z); BUF U0(.A(a),.Y(y)); BUF U1(.A(1'bx),.Y(z)); endmodule"
    result=native(design,{'a':1},{'y':0,'z':1},'sa0 y')
    assert result['detected']
    assert result['values']['z']==['x','x']


def test_repaired_dataset_with_bundled_asap7(monkeypatch):
    import json
    import pyarrow.parquet as pq
    monkeypatch.delenv('CELL_LIBS_VERILOG',raising=False)
    root=Path(__file__).resolve().parents[2]
    path=root/'data/freeset/dataset.freeset.asap7sc7p5t_28.rvt.tt.stil_repaired_v1/validation/data-03007.parquet'
    if not path.exists():
        pytest.skip('Local repaired dataset unavailable')
    row=next(pq.ParquetFile(path).iter_batches(batch_size=1)).to_pylist()[0]
    result=simulate(make_request(row['netlist'],row['input_vector'],row['expected_output'],row['fault']))
    assert result['detected']
    expected=json.loads(row['snapshot'])
    for name,value in result['values'].items():
        if name in expected['Good Machine']:
            assert value==[expected['Good Machine'][name],expected['Bad Machine'][name]]


def test_native_remote_tool_service(native, monkeypatch):
    import json
    import subprocess
    import time
    from tetramax_seats import lock_dir
    root=lock_dir()
    endpoint=root/'server.json'
    if endpoint.exists():
        pytest.skip('A coordinator is already published; do not disturb it')
    service=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'tetramax_service.py'),
                              '--workers','1','--port','0'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+10
        while not endpoint.exists() and service.poll() is None and time.monotonic()<deadline:
            time.sleep(0.1)
        assert endpoint.exists(), service.communicate(timeout=2)
        monkeypatch.setenv('TMAX_SERVER_FILE',str(endpoint))
        result=simulate(make_request(DESIGN,{'a':0,'b':0},{'y':0,'z':0},'sa1 n'))
        assert result['detected'] and result['values']['y']==[0,1]
        assert json.loads(endpoint.read_text())['workers']==1
    finally:
        service.terminate()
        service.communicate(timeout=12)
    assert service.returncode==0
    assert not endpoint.exists()
