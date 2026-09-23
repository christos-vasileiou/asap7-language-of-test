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
