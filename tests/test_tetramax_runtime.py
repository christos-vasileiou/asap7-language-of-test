"""License-free tests for native request validation, shared capacity and cleanup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tetramax_backend as backend
import tetramax_seats as seats
from tetramax_service import JobPool

DESIGN = 'module design(input a, input b, output y, output z); wire n; AND2 U1(.Y(n),.A(a),.B(b)); BUF U2(.Y(y),.A(n)); INV U3(.Y(z),.A(n)); endmodule'
CELLS = 'module AND2(input A, input B, output Y); and g(Y,A,B); endmodule\nmodule INV(input A, output Y); not g(Y,A); endmodule\nmodule BUF(input A, output Y); buf g(Y,A); endmodule\n'

@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv('TMAX_LOCK_DIR', str(tmp_path))
    monkeypatch.setenv('TMAX_MAX_CONCURRENT', '2')
    monkeypatch.delenv('TMAX_SERVER_FILE', raising=False)
    monkeypatch.delenv('TMAX_SERVER_URL', raising=False)
    return tmp_path


def test_metadata_and_vectors():
    model = backend.SimulationNetlist('module real_top (a,y) ; input [2:0] a; output y; endmodule')
    assert model.top == 'real_top'
    assert model.input_nets == ['a[0]', 'a[1]', 'a[2]']
    assert backend.assignment('a[2]:1,a[0]:0,a[1]:1', model.input_nets) == {'a[0]':0,'a[1]':1,'a[2]':1}
    for bad in ('a:1,a:0', "{'a':1,'a':0}", {'a':True}, {'a':'x'}, {'a':0,'extra':1}):
        with pytest.raises(ValueError):
            backend.assignment(bad, ['a'])
    with pytest.raises(ValueError):
        backend.make_request(DESIGN, {'a':1,'b':1}, {'y':1,'z':0}, 'sa0 missing')
    first = backend.make_request(DESIGN, {'a':1,'b':1}, {'y':1,'z':0}, 'sa0 n')
    second = backend.make_request(DESIGN, {'a':1,'b':1}, {'y':0,'z':1}, 'sa0 n')
    assert first == second  # Model output claims never determine simulation.


def test_strict_selection_without_binary(monkeypatch):
    import fault_sim
    monkeypatch.setenv('FAULT_SIM_BACKEND','tetramax')
    monkeypatch.setenv('TMAX_BIN','/missing/tmax')
    assert fault_sim.resolve_fault_sim_runner().__name__ == 'tetramax_fault_sim'
    assert isinstance(fault_sim.prepare_netlist(DESIGN), backend.SimulationNetlist)
    with pytest.raises(seats.SimulationError):
        backend.configuration()
    monkeypatch.setenv('FAULT_SIM_BACKEND','typo')
    with pytest.raises(ValueError):
        fault_sim.resolve_fault_sim_runner()


def test_pool_limit_disabled_conflict_and_drain(pool, monkeypatch):
    with seats.acquire_tmax_seat(0):
        with seats.acquire_tmax_seat(0):
            with pytest.raises(seats.SimulationError, match='Timed out'):
                with seats.acquire_tmax_seat(0):
                    pytest.fail('Third worker acquired')
    monkeypatch.setenv('TMAX_MAX_CONCURRENT','3')
    with pytest.raises(seats.SimulationError, match='belongs'):
        with seats.acquire_tmax_seat(0):
            pass
    monkeypatch.setenv('TMAX_MAX_CONCURRENT','0')
    with pytest.raises(seats.SimulationError, match='disabled'):
        with seats.acquire_tmax_seat(0):
            pass
    monkeypatch.setenv('TMAX_MAX_CONCURRENT','2')
    (pool/'DRAIN').touch()
    with pytest.raises(seats.SimulationError, match='draining'):
        with seats.acquire_tmax_seat(0):
            pass


def test_supervised_exit_timeout_cancel(pool):
    def run(code, **kwargs):
        return seats.run_tmax_subprocess([sys.executable,'-c',code], env=os.environ.copy(),cwd=str(pool),**kwargs)
    assert run("print('complete')").stdout.strip() == 'complete'
    with pytest.raises(seats.SimulationError, match='exceeded'):
        run('import time; time.sleep(60)', timeout_s=0.2)
    event = threading.Event()
    timer = threading.Timer(0.3,event.set)
    timer.start()
    with pytest.raises(seats.SimulationCancelled):
        run('import time; time.sleep(60)', cancel=event)
    timer.join()
    assert all(json.loads(p.read_text())['state']=='IDLE' for p in pool.glob('seat_*.json'))
    assert not list(pool.glob('*.quarantined'))


def test_owner_death_cleans_descendants(pool, monkeypatch):
    monkeypatch.setenv('TMAX_MAX_CONCURRENT','1')
    pidfile=pool/'descendant.pid'
    child = "import os,time; from pathlib import Path; Path('descendant.pid').write_text(str(os.getpid())); time.sleep(60)"
    wrapper = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'+repr(child)+']); time.sleep(60)'
    code = 'from tetramax_seats import run_tmax_subprocess; import os,sys; run_tmax_subprocess([sys.executable,"-c",'+repr(wrapper)+'],env=os.environ.copy(),cwd='+repr(str(pool))+')'
    env=dict(os.environ, PYTHONPATH=str(Path(backend.__file__).parent))
    owner=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+5
        while not pidfile.exists() and time.monotonic()<deadline:
            time.sleep(0.05)
        assert pidfile.exists()
        descendant=int(pidfile.read_text())
        owner.kill()
        owner.wait()
        with seats.acquire_tmax_seat(8):
            assert not Path(f'/proc/{descendant}').exists()
        assert json.loads((pool/'seat_0.json').read_text())['owner_lost'] is True
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait()


def test_uncertified_slot_is_quarantined(pool):
    (pool/'seat_0.json').write_text('{"state":"RUNNING"}')
    with seats.acquire_tmax_seat(0) as (_,state):
        assert state.name == 'seat_1'
    assert (pool/'seat_0.quarantined').exists()


def test_cache_coalesces_and_caches_negative(pool, monkeypatch):
    count=[]
    monkeypatch.setattr(backend,'configuration',lambda:('mock',[]))
    monkeypatch.setattr(backend,'_identity',lambda *a:('identity','test-version'))
    def run(*args):
        count.append(1)
        time.sleep(0.1)
        return {'schema':backend.VERSION,'identity':'identity','detected':False,'cache_hit':False}
    monkeypatch.setattr(backend,'_run',run)
    req=backend.make_request(DESIGN,{'a':0,'b':0},{'y':0,'z':0},'sa0 n')
    with ThreadPoolExecutor(8) as executor:
        results=list(executor.map(lambda _:backend.simulate(req),range(8)))
    assert len(count)==1
    assert all(r['detected'] is False for r in results)
    assert sum(r['cache_hit'] for r in results)==7
    assert not backend._key_locks


def test_service_bounded_cancel_idempotency():
    active=[]
    started=threading.Event()
    def runner(payload,cancel):
        active.append(payload)
        started.set()
        if not cancel.wait(2):
            raise AssertionError('Cancellation did not reach worker')
        raise seats.SimulationCancelled('cancelled')
    service=JobPool(1,1,runner)
    try:
        service.submit('a'*32,{'i':1})
        assert started.wait(2)
        service.submit('a'*32,{'i':1})
        service.submit('b'*32,{'i':2})
        with pytest.raises(seats.SimulationError,match='full'):
            service.submit('c'*32,{})
        with pytest.raises(ValueError,match='reused'):
            service.submit('a'*32,{'i':2})
        service.cancel('a'*32)
        service.cancel('b'*32)
    finally:
        service.close()
    assert service.poll('a'*32)['error_type']=='infrastructure'


def test_reconfiguration_requires_proven_cleanup(pool):
    from tetramax_pool import reconfigure
    with pytest.raises(seats.SimulationError,match='Drain'):
        reconfigure(pool)
    with seats.acquire_tmax_seat(0):
        (pool/'DRAIN').touch()
        with pytest.raises(BlockingIOError):
            reconfigure(pool)
    (pool/'seat_0.json').write_text('{"state":"RUNNING"}')
    with pytest.raises(seats.SimulationError,match='unconfirmed'):
        reconfigure(pool)
    (pool/'seat_0.json').write_text('{"state":"IDLE"}')
    (pool/'seat_0.quarantined').touch()
    with pytest.raises(seats.SimulationError,match='Quarantined'):
        reconfigure(pool)
    (pool/'seat_0.quarantined').unlink()
    reconfigure(pool)
    assert json.loads((pool/'pool.json').read_text())['seats']==2
    assert (pool/'DRAIN').exists()
