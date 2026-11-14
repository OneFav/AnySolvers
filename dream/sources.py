"""Read published development evidence from one task/batch in this domain."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wiki_sources import collect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch')
    parser.add_argument('evaluation', nargs='?')
    parser.add_argument('--expect', help='verify the cited summary SHA-256')
    parser.add_argument('--cases', action='store_true')
    parser.add_argument('--report', action='append', default=[], help='explicitly shared method report session')
    args = parser.parse_args()
    if (args.expect or args.cases) and not args.evaluation:
        parser.error('--expect and --cases require an evaluation ID')
    try:
        result = collect(args.batch, args.evaluation,
                         args.expect, args.cases, args.report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (KeyError, ValueError, OSError) as exc:
        parser.exit(2, f'dream sources: {exc}\n')


if __name__ == '__main__':
    main()
