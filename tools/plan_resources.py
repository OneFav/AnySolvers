"""Read study metadata and ask its datasets module for a resource proposal."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import common as c, study
from tools.configuration import prepare
from tools.module_runtime import invoke, read_module


def plan(study_id, config_path, measured=None):
    cfg = prepare(config_path)
    root, manifest = study.load(study_id)
    cfg['modules'] = {slot: read_module(binding['use'], binding.get('params'))
                      for slot, binding in cfg['modules'].items()}
    configured = cfg['modules']['datasets']
    registered = manifest['modules']['datasets']
    if configured['sha'] != registered['sha'] or configured['params'] != registered['params']:
        raise ValueError('resource planning must use the registered datasets method')
    records = manifest['deliveries']['dataset']
    anchors = [r for r in records.values() if r['manifest']['purpose'] == 'anchor']
    if len(anchors) != 1:
        raise ValueError('register the development anchor before resource planning')
    benchmark = c.read_json(c.contained(root, anchors[0]['manifest']['benchmark_file']))
    counts = {}
    for key, record in records.items():
        # Count records only. In particular, do not parse final inputs or load
        # candidate, baseline, evaluator, or environment code for this proposal.
        with c.contained(root, record['manifest']['cases_file']).open('rb') as stream:
            counts[key] = sum(1 for line in stream if line.strip())
    result = invoke(cfg, 'datasets', 'recommend_compute', benchmark['protocol'], counts,
                    cfg['compute'], invoke(cfg, 'evaluation', 'resource_contract'), measured)
    return dict(schema_version=1, study_id=study_id, study_status=manifest['status'], **result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('study')
    parser.add_argument('--config', required=True)
    parser.add_argument('--observed-wall-s', type=float)
    parser.add_argument('--observed-cpu-s', type=float)
    parser.add_argument('--observed-memory-mb', type=float)
    parser.add_argument('--measurement-note', help='Source and scope of public baseline measurements')
    args = parser.parse_args()
    measured = {key: value for key, value in {
        'wall_s': args.observed_wall_s, 'cpu_s': args.observed_cpu_s,
        'peak_memory_mb': args.observed_memory_mb}.items() if value is not None}
    if measured or args.measurement_note:
        measured['note'] = args.measurement_note
    try:
        print(json.dumps(plan(args.study, args.config, measured or None), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
