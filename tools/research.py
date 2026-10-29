"""Researcher-direct collaborative research tools; fixed programs validate, queue and measure."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import common as c
from tools.module_runtime import invoke


def command(run_id, operation, **kwargs):
    _, cfg, _ = c.load_run(run_id)
    if operation not in ('request', 'process', 'notebook'):
        return invoke(cfg, 'research', 'direct', run_id, operation, **kwargs)
    return invoke(cfg, 'research', 'exchange', run_id, operation, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['context', 'evaluate', 'result', 'findings', 'share', 'reuse', 'checkpoint', 'iteration', 'audit'])
    p.add_argument('run')
    p.add_argument('--session')
    p.add_argument('--file', '--manifest', dest='file', help='Draft or candidate manifest relative to this batch')
    p.add_argument('--id', help='Registered evaluation request ID')
    p.add_argument('--dataset')
    p.add_argument('--evaluation')
    p.add_argument('--probe', action='store_true')
    p.add_argument('--details', action='store_true')
    p.add_argument('--case')
    a = p.parse_args()
    try:
        if a.operation != 'audit' and not a.session:
            p.error('--session is required')
        value = command(a.run, a.operation, session_id=a.session, relative=a.file, request_id=a.id,
                        dataset_id=a.dataset, evaluation_id=a.evaluation, probe=a.probe,
                        case_id=a.case, details=a.details)
    except Exception as exc:
        p.exit(2, 'research: ' + str(exc) + '\n')
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
