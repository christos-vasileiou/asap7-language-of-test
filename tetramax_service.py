"""Authenticated bounded TetraMAX job service for DDP and evaluation clients.

Run on the simulator host (CPU service; vLLM may use a GPU on the same host):
  python data_preprocessing/tetramax_service.py --workers 16 --host 0.0.0.0
Clients export TMAX_SERVER_FILE to the emitted shared credentials file. The file
is private to the user; it is not a model tool argument. No remote shell commands
or client-supplied executable/library paths are accepted.
"""
from __future__ import annotations
import argparse
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import threading
import time
from urllib import request as http, error as http_error
import uuid

from tetramax_seats import SimulationError, SimulationCancelled, lock_dir, _pool, acquire_timeout_s, per_run_timeout_s, max_concurrent_seats


class TetraMaxHTTPServer(ThreadingHTTPServer):
    # DDP ranks submit and poll concurrently (16 reward threads per rank).
    # The default backlog of five drops bursts before JobPool sees them.
    # This is connection capacity, not simulator/license concurrency.
    request_queue_size = 256


class JobPool:
    def __init__(self, workers, capacity, runner=None):
        from tetramax_backend import simulate
        self.runner = runner or (lambda payload, cancel: simulate(payload, cancel, local=True))
        self.jobs = {}
        self.guard = threading.Lock()
        self.queue = queue.Queue(capacity)
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]
        for thread in self.threads:
            thread.start()
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def submit(self, job_id, payload):
        if self.stop.is_set():
            raise SimulationError('Service is draining')
        if not isinstance(job_id,str) or len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):
            raise ValueError('Invalid request ID')
        with self.guard:
            if job_id in self.jobs:
                if self.jobs[job_id]['payload'] != payload:
                    raise ValueError('Request ID was reused for different inputs')
                return
            # Bound retained results, including forgotten clients.
            if len(self.jobs) >= 2048:
                terminal = [k for k,j in self.jobs.items() if j['state'] in ('done','error')]
                if not terminal:
                    raise SimulationError('Service request capacity exhausted')
                self.jobs.pop(terminal[0])
            self.jobs[job_id] = {'payload':payload,'cancel':threading.Event(),'state':'queued',
                                 'created':time.monotonic(),'last_poll':time.monotonic()}
            try:
                self.queue.put_nowait(job_id)
            except queue.Full:
                self.jobs.pop(job_id)
                raise SimulationError('TetraMAX queue is full; retry later')

    def poll(self, job_id):
        with self.guard:
            job = self.jobs[job_id]
            job['last_poll'] = time.monotonic()
            return {k:job[k] for k in ('state','result','error','error_type') if k in job}

    def cancel(self, job_id):
        with self.guard:
            self.jobs[job_id]['cancel'].set()

    def _work(self):
        while not self.stop.is_set():
            try:
                job_id = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self.guard:
                job = self.jobs[job_id]
                job['state'] = 'running'
            try:
                if job['cancel'].is_set():
                    raise SimulationCancelled('Request cancelled before execution')
                result = self.runner(job['payload'],job['cancel'])
                with self.guard:
                    job.update(state='done',result=result)
            except Exception as exc:
                with self.guard:
                    job.update(state='error',error=str(exc),error_type='invalid_request' if isinstance(exc,ValueError) else 'infrastructure')
            finally:
                self.queue.task_done()

    def _monitor(self):
        while not self.stop.wait(1):
            now = time.monotonic()
            with self.guard:
                for key,job in list(self.jobs.items()):
                    if job['state'] in ('queued','running'):
                        if now-job['last_poll'] > 30 or now-job['created'] > acquire_timeout_s()+per_run_timeout_s()+10:
                            job['cancel'].set()
                    elif now-job['last_poll'] > 600:
                        self.jobs.pop(key)

    def close(self):
        self.stop.set()
        with self.guard:
            for job in self.jobs.values():
                job['cancel'].set()
        for thread in self.threads:
            thread.join(timeout=10)


def credentials():
    path = os.environ.get('TMAX_SERVER_FILE')
    if path:
        try:
            data = json.loads(Path(path).read_text())
            return data['url'].rstrip('/'),data['token']
        except (OSError, ValueError, KeyError) as exc:
            raise SimulationError(f'Cannot read TetraMAX service credentials: {path}') from exc
    url = os.environ.get('TMAX_SERVER_URL','').rstrip('/')
    token = os.environ.get('TMAX_SERVER_TOKEN','')
    if not url or not token:
        raise SimulationError('Set TMAX_SERVER_FILE or TMAX_SERVER_URL plus TMAX_SERVER_TOKEN')
    return url,token


def remote_simulate(payload, cancel=None):
    url,token = credentials()
    job_id = uuid.uuid4().hex
    def call(method, path, body=None):
        req = http.Request(url+path, data=None if body is None else json.dumps(body).encode(),method=method,
                           headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            # Internal cluster requests must not be routed through proxy settings.
            with http.build_opener(http.ProxyHandler({})).open(req, timeout=5) as response:
                return json.load(response)
        except (OSError,ValueError) as exc:
            raise SimulationError(f'TetraMAX service request failed: {exc}') from exc
    done = False
    deadline = time.monotonic()+acquire_timeout_s()+per_run_timeout_s()+15
    try:
        call('POST','/jobs',{'id':job_id,'request':payload})
        while time.monotonic()<deadline:
            if cancel is not None and cancel.is_set():
                raise SimulationCancelled('Remote TetraMAX request cancelled')
            status = call('GET','/jobs/'+job_id)
            if status['state']=='done':
                done=True
                return status['result']
            if status['state']=='error':
                done=True
                cls=ValueError if status['error_type']=='invalid_request' else SimulationError
                raise cls(status['error'])
            time.sleep(0.2)
        raise SimulationError('TetraMAX service deadline exceeded')
    finally:
        if not done:
            try:
                call('DELETE','/jobs/'+job_id)
            except SimulationError:
                pass  # Heartbeat expiry and the worker deadline still enforce cleanup.


def handler(pool,token,simulator=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def respond(self,status,data):
            raw=json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):
                pass
        def dispatch(self):
            self.connection.settimeout(5)
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.respond(401,{'error':'Unauthorized'})
                return
            try:
                if self.command=='GET' and self.path=='/health':
                    self.respond(200,{'status':'ok','workers':len(pool.threads),'queue':pool.queue.qsize(),'simulator':simulator})
                elif self.command=='POST' and self.path=='/jobs':
                    length=int(self.headers.get('Content-Length',0))
                    if length<=0 or length>2_000_000:
                        raise ValueError('Invalid request size')
                    data=json.loads(self.rfile.read(length))
                    pool.submit(data['id'],data['request'])
                    self.respond(202,{'id':data['id']})
                elif self.path.startswith('/jobs/') and self.command=='GET':
                    self.respond(200,pool.poll(self.path[len('/jobs/'):]))
                elif self.path.startswith('/jobs/') and self.command=='DELETE':
                    pool.cancel(self.path[len('/jobs/'):])
                    self.respond(200,{'cancelled':True})
                else:
                    self.respond(404,{'error':'Not found'})
            except (ValueError,KeyError,TypeError) as exc:
                self.respond(400,{'error':str(exc)})
            except SimulationError as exc:
                self.respond(503,{'error':str(exc)})
        do_GET=do_POST=do_DELETE=dispatch
    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers',type=int,default=16)
    parser.add_argument('--queue-size',type=int,default=128)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--advertise-host')
    parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args()
    allocation = max_concurrent_seats()
    if not 1<=args.workers<=allocation or args.queue_size<1:
        parser.error(f'Workers must be 1..{allocation}; queue size must be positive')
    # All entry points retain the same 16-seat global allocation. A smaller
    # service pool uses fewer slots without changing the project policy.
    root=lock_dir()
    _pool(root,allocation)
    with (root/'service.lock').open('a+') as guard:
        try:
            fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('A TetraMAX coordinator already owns this project pool')
        from tetramax_backend import configuration, _identity
        binary,libs=configuration()
        fingerprint, version = _identity({},binary,libs)  # No license checkout.
        from tetramax_backend import VERSION
        simulator = {'schema':VERSION, 'fingerprint':fingerprint, 'tool_version':version}
        pool=JobPool(args.workers,args.queue_size)
        token=secrets.token_urlsafe(32)
        server=TetraMaxHTTPServer((args.host,args.port),handler(pool,token,simulator))
        advertised = args.advertise_host or ('127.0.0.1' if args.host == '127.0.0.1' else socket.gethostname())
        path=root/'server.json'
        temporary = root/('.server-'+uuid.uuid4().hex)
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
            json.dump({'url':f'http://{advertised}:{server.server_port}','token':token,'workers':args.workers},f)
        temporary.replace(path)
        print(f'TetraMAX service ready; clients export TMAX_SERVER_FILE={path}',flush=True)
        def stop(*_):
            threading.Thread(target=server.shutdown,daemon=True).start()
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            pool.close()
            server.server_close()
            path.unlink(missing_ok=True)


if __name__=='__main__':
    main()
