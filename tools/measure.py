"""Compatibility import and CLI for the configured measurement module."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import common
from tools.module_runtime import invoke


def evaluate(run_id, candidate_id, dataset_id, evaluation_id, probe=False, task_dir=None):
    _, cfg, _ = common.load_run(run_id)
    if 'modules' not in cfg:  # Isolated measurement-unit fixtures only.
        from modules.evaluation.queued_sandbox import measure as _implementation
        return _implementation.evaluate(run_id,candidate_id,dataset_id,evaluation_id,probe,task_dir)
    return invoke(cfg,'evaluation','evaluate',run_id,candidate_id,dataset_id,evaluation_id,probe,task_dir)


def details(run_id, evaluation_id, case_id=None):
    _, cfg, _ = common.load_run(run_id)
    if 'modules' not in cfg:
        from modules.evaluation.queued_sandbox import measure as _implementation
        return _implementation.details(run_id,evaluation_id,case_id)
    return invoke(cfg,'evaluation','details',run_id,evaluation_id,case_id)


def __getattr__(name):
    # Explicit legacy helper access only; normal dispatch never imports the default.
    if name.startswith('__'):
        raise AttributeError(name)
    from modules.evaluation.queued_sandbox import measure as implementation
    return getattr(implementation,name)


def main(task_dir=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--candidate")
    parser.add_argument("--dataset")
    parser.add_argument("--evaluation")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--details", metavar="EVAL_ID")
    parser.add_argument("--case", metavar="CASE_ID")
    args = parser.parse_args()
    if args.details:
        if args.candidate or args.dataset or args.evaluation or args.probe:
            parser.error("--details cannot execute a measurement")
    elif not all((args.candidate, args.dataset, args.evaluation)) or args.case:
        parser.error("measurements require --candidate, --dataset, --evaluation; --case requires --details")
    try:
        result = details(args.run, args.details, args.case) if args.details else evaluate(
            args.run, args.candidate, args.dataset, args.evaluation, args.probe, task_dir)
    except Exception as exc:
        parser.exit(2, f"measure: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
