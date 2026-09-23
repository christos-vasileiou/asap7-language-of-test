"""Authoritative TetraMAX simulation, independent of Python gate evaluation."""
from __future__ import annotations
import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from tetramax_seats import SimulationError, SimulationCancelled, lock_dir, run_tmax_subprocess, acquire_timeout_s, per_run_timeout_s, result_cache_size
from tetramax_stil import write_vector_stil

VERSION = 'tetramax-native-v1'
TCL = Path(__file__).with_name('scripts') / 'tmax_vector_fault_sim.tcl'


def canonical(name):
    return name.strip().removeprefix('\\')


class SimulationNetlist:
    """Port/declaration metadata only; no custom simulator or gate-function LUTs."""
    def __init__(self, text, *unused, **ignored):
        self.netlist = text
        clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
        modules = list(re.finditer(r'\bmodule\s+(\\\S+|[A-Za-z_]\w*)\s*\((.*?)\)\s*;', clean, re.S))
        if len(modules) != 1 or len(re.findall(r'\bendmodule\b', clean)) != 1:
            raise ValueError('TetraMAX requires one explicit, unparameterized top module')
        self.top, header = modules[0].groups()
        declarations = re.findall(r'\b(input|output|inout|wire|reg)\b\s*(\[[^\]]+\])?\s*([^;]+);',
                                  clean[modules[0].end():], re.S)
        if re.search(r'\b(input|output|inout)\b', header):
            kind = bounds = None
            for part in header.split(','):
                match = re.fullmatch(r'\s*(?:(input|output|inout)\s+)?(?:wire\s+)?(\[[^\]]+\])?\s*(\\\S+|[A-Za-z_]\w*)\s*', part)
                if match is None:
                    raise ValueError('Unsupported ANSI port declaration')
                new_kind, new_bounds, name = match.groups()
                if new_kind:
                    kind, bounds = new_kind, new_bounds
                elif new_bounds:
                    bounds = new_bounds
                if kind is None:
                    raise ValueError('Port direction missing')
                declarations.append((kind, bounds, name))
        nets = {kind: [] for kind in ['input', 'output', 'inout', 'wire', 'reg']}
        def indices(bounds):
            if not bounds:
                return [None]
            match = re.fullmatch(r'\[\s*(-?\d+)\s*:\s*(-?\d+)\s*\]', bounds)
            if not match:
                raise ValueError('Only constant bus bounds are supported')
            a, b = map(int, match.groups())
            if abs(a-b) > 65536:
                raise ValueError('Bus width exceeds simulator request limit')
            return range(min(a,b), max(a,b)+1)
        for kind, packed, rest in declarations:
            for token in rest.split(','):
                m = re.fullmatch(r'\s*(\\\S+|[A-Za-z_]\w*)\s*(\[[^\]]+\])?\s*', token)
                if m is None:
                    raise ValueError(f'Unsupported {kind} declaration: {token}')
                name, unpacked = m.groups()
                for u in indices(unpacked):
                    for p in indices(packed):
                        nets[kind].append(name + ''.join(f'[{i}]' for i in (u,p) if i is not None))
        if nets['inout'] or re.search(r'\b(always|initial)\b', clean):
            raise ValueError('Sequential, behavioral, and bidirectional designs are unsupported')
        self.input_nets, self.output_nets = nets['input'], nets['output']
        self.all_nets = list(dict.fromkeys(sum(nets.values(), [])))
        if not self.output_nets or len(set(self.input_nets + self.output_nets)) != len(self.input_nets + self.output_nets):
            raise ValueError('Missing or duplicate canonical ports')
        if len({canonical(n) for n in self.all_nets}) != len(self.all_nets):
            raise ValueError('Ambiguous escaped/canonical net names')


def assignment(value, names):
    if isinstance(value, str):
        value = value.strip()
        if value.startswith('{'):
            node = ast.parse(value, mode='eval').body
            if not isinstance(node, ast.Dict):
                raise ValueError('Assignment must be an object')
            pairs = [(ast.literal_eval(k), ast.literal_eval(v)) for k,v in zip(node.keys,node.values)]
        else:
            pairs = []
            for field in value.split(','):
                sep = ':' if ':' in field else '='
                key, bit = field.rsplit(sep, 1)
                pairs.append((key.strip(), bit.strip()))
    elif isinstance(value, dict):
        pairs = list(value.items())
    else:
        raise ValueError('Expected a vector object or net: bit assignments')
    result = {}
    for key, bit in pairs:
        if not isinstance(key, str) or key in result or type(bit) not in (int,str) or bit not in (0,1,'0','1'):
            raise ValueError('Duplicate net, invalid name, or nonbinary vector value')
        result[key] = int(bit)
    if set(result) != set(names):
        raise ValueError(f'Expected exactly these nets: {list(names)}')
    return {name: result[name] for name in names}


def tcl_word(value):
    return '[encoding convertfrom utf-8 [binary format H* {' + str(value).encode().hex() + '}]]'


def configuration():
    from tmax import infer_tmax_binary, cell_verilog_paths_from_env_or_kit
    binary = shutil.which(infer_tmax_binary())
    if binary is None:
        raise SimulationError('TetraMAX executable unavailable; load the site module or set TMAX_BIN')
    try:
        libraries = cell_verilog_paths_from_env_or_kit(Path(__file__).parent, True)
    except (OSError, ValueError) as exc:
        raise SimulationError(str(exc)) from exc
    return binary, libraries


def _identity(request, binary, libraries):
    # Version probing does not check out licenses; cache it per executable identity.
    stamp = (binary, Path(binary).stat().st_mtime_ns, os.environ.get('LD_LIBRARY_PATH',''))
    with _version_lock:
        if stamp not in _versions:
            try:
                p = subprocess.run([binary, '-version'], capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SimulationError(f'TetraMAX version preflight failed: {exc}') from exc
            if p.returncode or not re.search(r'[A-Z]-\d{4}\.\d{2}', p.stdout) or 'error while loading' in p.stderr:
                raise SimulationError(f'TetraMAX runtime unavailable: {(p.stderr or p.stdout).strip()}')
            _versions[stamp] = p.stdout.strip()
        version = _versions[stamp]
    identity = {'adapter': VERSION, 'request': request, 'binary': binary, 'tool_version': version,
                'python': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'tcl': hashlib.sha256(TCL.read_bytes()).hexdigest(),
                'stil': hashlib.sha256(Path(__file__).with_name('tetramax_stil.py').read_bytes()).hexdigest(),
                'libraries': [(p, hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in libraries]}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(), version


_versions = {}
_version_lock = threading.Lock()
_key_locks = {}
_key_guard = threading.Lock()


def make_request(netlist, inputs, outputs, fault):
    model = SimulationNetlist(netlist)
    iv = assignment(inputs, model.input_nets)
    assignment(outputs, model.output_nets)
    m = re.fullmatch(r'sa([01])\s+(.+)', fault.strip())
    if m is None or any(c in m[2] for c in '\n\r\t\x00'):
        raise ValueError('Expected one sa0/sa1 fault')
    net = m[2].strip()
    kind = 'net'
    if net.startswith('pin:'):
        kind, net = 'pin', net[4:]
    elif net not in model.all_nets:
        raise ValueError(f'Target net is not declared: {net}; explicit branch faults use pin:instance/pin')
    return {'netlist': netlist, 'input_vector': iv, 'fault_net': net, 'stuck_at': int(m[1]), 'fault_kind': kind}


def simulate(request, cancel=None, *, local=False):
    if not local and (os.environ.get('TMAX_SERVER_URL') or os.environ.get('TMAX_SERVER_FILE')):
        from tetramax_service import remote_simulate
        return remote_simulate(request, cancel)
    model = SimulationNetlist(request['netlist'])
    # Revalidate clients at the service boundary, including exact key set.
    if set(request) != {'netlist','input_vector','fault_net','stuck_at','fault_kind'}:
        raise ValueError('Invalid simulator request fields')
    if type(request['stuck_at']) is not int or request['stuck_at'] not in (0,1) or request['fault_kind'] not in ('net','pin'):
        raise ValueError('Invalid fault model')
    fault = f"sa{request['stuck_at']} " + ('pin:' if request['fault_kind']=='pin' else '') + request['fault_net']
    make_request(model.netlist, request['input_vector'], {n:0 for n in model.output_nets}, fault)
    binary, libraries = configuration()
    key, version = _identity(request, binary, libraries)
    cache_dir = lock_dir() / 'cache'
    cache_dir.mkdir(exist_ok=True)
    result_path = cache_dir / (key + '.json')
    with _key_guard:
        entry = _key_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    deadline = time.monotonic() + acquire_timeout_s() + per_run_timeout_s()
    def check_wait():
        if cancel is not None and cancel.is_set():
            raise SimulationCancelled('Cancelled while awaiting identical simulation')
        if time.monotonic() >= deadline:
            raise SimulationError('Deadline exceeded while awaiting identical simulation')
    try:
        check_wait()
        while not entry[0].acquire(timeout=0.1):
            check_wait()
            if cancel is not None and cancel.is_set():
                raise SimulationCancelled('Cancelled while awaiting identical simulation')
        try:
            # Cross-process coalescing for tools, rewards, and independent jobs.
            import fcntl
            with (cache_dir / (key[:3]+'.lock')).open('a+') as guard:
                while True:
                    try:
                        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        check_wait()
                        if cancel is not None and cancel.is_set():
                            raise SimulationCancelled('Cancelled while awaiting cached simulation')
                        time.sleep(0.1)
                if result_cache_size() and result_path.exists():
                    try:
                        data = json.loads(result_path.read_text())
                        if data.get('identity') == key and data.get('schema') == VERSION:
                            data['cache_hit'] = True
                            return data
                    except (OSError, ValueError):
                        pass  # Recompute interrupted/corrupt cache entries.
                data = _run(request, model, binary, libraries, version, key, cancel)
                if not result_cache_size():
                    return data
                tmp = result_path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
                tmp.write_text(json.dumps(data))
                tmp.replace(result_path)
                trim_cache(cache_dir, result_cache_size())
                return data
        finally:
            entry[0].release()
    finally:
        with _key_guard:
            entry[1] -= 1
            if not entry[1]:
                _key_locks.pop(key, None)


def trim_cache(directory, limit):
    # Never remove key lock files: unlinking a held flock creates two owners.
    entries = []
    for path in directory.glob('*.json'):
        try:
            entries.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            pass
    for _, path in sorted(entries)[:max(0, len(entries) - limit)]:
        path.unlink(missing_ok=True)


def _run(request, model, binary, libraries, version, key, cancel):
    root = lock_dir() / 'runs'
    root.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix='request_', dir=root))
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    try:
        (run/'design.v').write_text(model.netlist)
        write_vector_stil(run/'vector.stil', model.input_nets, request['input_vector'], model.output_nets)
        values = {'verilog_file': str(run/'design.v'), 'top_module': model.top,
                  'stil_file': str(run/'vector.stil'), 'fault_net': request['fault_net'],
                  'fault_kind': request['fault_kind'], 'stuck_at': request['stuck_at'], 'request_id': request_id}
        manifest = '\n'.join(f'set {k} {tcl_word(v)}' for k,v in values.items())
        for name, entries in [('cell_libraries',libraries),('po_names',model.output_nets)]:
            manifest += '\nset '+name+' [list '+' '.join(tcl_word(v) for v in entries)+']'
        (run/'request.tcl').write_text(manifest+'\n')
        env = os.environ.copy()
        env['REQUEST_FILE'] = str(run/'request.tcl')
        p = run_tmax_subprocess([binary,'-shell','-nostartup',str(TCL)], env=env, cwd=str(run), cancel=cancel)
        (run/'stdout.log').write_text(p.stdout)
        (run/'stderr.log').write_text(p.stderr)
        if (run/'error.txt').exists() or re.search(r'^\s*Error:', p.stdout+'\n'+p.stderr,re.M):
            raise SimulationError('TetraMAX reported an error')
        fields = dict(line.split('\t',1) for line in (run/'result.tsv').read_text().splitlines())
        if fields.get('complete') != request_id:
            raise SimulationError('Missing/mismatched TetraMAX completion marker')
        good_log = (run/'good_simulation.log').read_text()
        if not re.search(r'Simulation completed: #patterns=1, #fail_pats=0\(0\), #failing_meas=0\(0\), #rejected_pats=0',good_log):
            raise SimulationError('Good-machine pattern validation failed')
        if 'Fault simulation completed:' not in (run/'fault_simulation.log').read_text():
            raise SimulationError('Fault simulation did not complete')
        native = fields['fault_status']
        allowed = {'DS','NC','ND','AU','AN','AP','NP','PT','UU','UT','UB','UR','UC','UO','RE'}
        if native not in allowed:
            raise SimulationError(f'Unsupported native fault status {native!r}')
        names = {canonical(n):n for n in model.all_nets}
        snapshot = {}
        for line in (run/'values.tsv').read_text().splitlines():
            n,g,b = line.split('\t')
            if g not in {'0','1','X','Z'} or b not in {'0','1','X','Z'}:
                raise SimulationError('Malformed native value')
            row = [int(v) if v in '01' else v.lower() for v in (g,b)]
            name = names.get(n,n)
            if name in snapshot and snapshot[name] != row:
                raise SimulationError(f'Conflicting native values for {name}')
            snapshot[name] = row
        if not set(model.input_nets+model.output_nets).issubset(snapshot):
            raise SimulationError('Native results omit canonical ports')
        data = {'schema': VERSION,'backend':'tetramax','tool_version':version,'identity':key,
                'status':'ok','fault_status':native,'detected':native=='DS',
                'indeterminate':native != 'DS' and (native in {'AP','NP','PT'} or any(
                    any(v in ('x','z') for v in snapshot[po]) for po in model.output_nets)),
                'native_fault':fields['fault_pin'],
                'fault':f"sa{request['stuck_at']} " + ('pin:' if request['fault_kind']=='pin' else '') + request['fault_net'],
                'inputs':request['input_vector'],'outputs':model.output_nets,'values':snapshot,
                'capabilities':{'good_po':True,'bad_po':True,'internal_values':True},
                'cache_hit':False,'elapsed_s':time.monotonic()-started,
                'licenses':(run/'licenses.log').read_text().strip().splitlines()}
        return data
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            (run/'stdout.log').write_text(exc.stdout or '')
            (run/'stderr.log').write_text(exc.stderr or '')
        raise SimulationError(f'TetraMAX request failed; diagnostics: {run}: {exc}') from exc
    except SimulationError as exc:
        raise type(exc)(f'{exc}; diagnostics: {run}') from exc
    finally:
        if 'data' in locals() and os.environ.get('TMAX_KEEP_ARTIFACTS', '0') != '1':
            shutil.rmtree(run)
        else:
            (run/'finished').touch()
            # Bound completed diagnostics without touching live requests.
            limit = max(1, int(os.environ.get('TMAX_ARTIFACT_LIMIT', '32')))
            finished = []
            for marker in root.glob('request_*/finished'):
                try:
                    finished.append((marker.stat().st_mtime, marker.parent))
                except FileNotFoundError:
                    pass
            for _, old in sorted(finished)[:max(0, len(finished)-limit)]:
                if old != run:
                    shutil.rmtree(old, ignore_errors=True)


def as_frame(result):
    import pandas as pd
    frame = pd.DataFrame.from_dict(result['values'], orient='index', columns=['Good Machine','Bad Machine'])
    frame['PIs'] = frame.index.isin(result['inputs'])
    frame['POs'] = frame.index.isin(result['outputs'])
    frame.attrs['simulation_result'] = result
    return frame


def observation(frame):
    # Existing trained models receive the familiar table, with actual native data.
    return frame[['Good Machine','Bad Machine']].to_json()
