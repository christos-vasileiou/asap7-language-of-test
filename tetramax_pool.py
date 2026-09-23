"""Inspect, drain, or move the shared pool after all processes have stopped."""
import argparse
import fcntl
import json
import socket
from pathlib import Path
from tetramax_seats import lock_dir, max_concurrent_seats, write_state, SimulationError


def reconfigure(root):
    if not (root/'DRAIN').exists():
        raise SimulationError('Drain the pool and stop the coordinator before reconfiguration')
    with (root/'pool.lock').open('a+') as policy, (root/'service.lock').open('a+') as service:
        fcntl.flock(policy, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(service, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held=[]
        try:
            for path in root.glob('seat_*.lock'):
                guard=path.open('a+')
                held.append(guard)
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if list(root.glob('*.quarantined')):
                raise SimulationError('Quarantined slots require process/license investigation; cannot reconfigure')
            for path in root.glob('seat_*.json'):
                if json.loads(path.read_text()).get('state') != 'IDLE':
                    raise SimulationError(f'Cleanup unconfirmed: {path}')
            write_state(root/'pool.json', {'host':socket.gethostname(),'seats':max_concurrent_seats()})
        finally:
            for guard in held:
                guard.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','drain','reconfigure','resume'])
    args=parser.parse_args()
    root=lock_dir()
    if args.action=='drain':
        (root/'DRAIN').touch()
    elif args.action=='reconfigure':
        reconfigure(root)
    elif args.action=='resume':
        # Recheck the full shutdown proof before removing DRAIN.
        reconfigure(root)
        (root/'DRAIN').unlink()
    else:
        print(json.dumps({'pool':json.loads((root/'pool.json').read_text()) if (root/'pool.json').exists() else None,
            'draining':(root/'DRAIN').exists(),'quarantined':[p.name for p in root.glob('*.quarantined')],
            'slots':{p.name:json.loads(p.read_text()) for p in root.glob('seat_*.json')}},indent=2))


if __name__=='__main__':
    main()
