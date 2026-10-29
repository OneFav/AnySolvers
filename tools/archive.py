"""Explicit file archiving and restoration. Normal workflow tools never call this module."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import common as c


def object_path(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('expected a SHA-256 object ID')
    return c.contained(c.ROOT, 'assets/objects/' + value)


def read_object(value):
    data = object_path(value).read_bytes()
    if hashlib.sha256(data).hexdigest() != value:
        raise ValueError('archive object corrupted: ' + value)
    return data


def replace_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        if path.is_file():
            temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def store(data):
    value = hashlib.sha256(data).hexdigest()
    path = object_path(value)
    if path.exists():
        read_object(value)
    else:
        replace_bytes(path, data)
    return value


def save(relative):
    data = c.workspace_file(relative).read_bytes()
    return {'path': relative, 'sha': store(data)}


def restore(value, relative, replace=False):
    path = c.workspace_file(relative)
    if Path(relative).parts[0].casefold() == 'runs':
        raise ValueError('restore outside runs; sealed run records must not be rolled back in place')
    if path.exists() and not replace:
        raise ValueError('destination exists; use --replace to archive it before restoring')
    data = read_object(value)
    previous = save(relative) if path.exists() else None
    replace_bytes(path, data)
    return {'path': relative, 'sha': value, 'previous': previous}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    saving = commands.add_parser('save', help='Archive named files; output their path and SHA-256 references')
    saving.add_argument('paths', nargs='+')
    restoring = commands.add_parser('restore', help='Restore one archived object to a working file')
    restoring.add_argument('sha')
    restoring.add_argument('path')
    restoring.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    try:
        result = ([save(path) for path in args.paths] if args.command == 'save'
                  else restore(args.sha, args.path, args.replace))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
