"""Initialize a domain wiki and publish Markdown edits against the version read."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import common


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    common.contained(root, 'topics').mkdir(exist_ok=True)
    path = common.contained(root, 'README.md')
    try:
        with path.open('x', encoding='utf-8') as stream:
            stream.write('# 领域知识库\n\n## 主题\n')
    except FileExistsError:
        pass


def page_path(root, name):
    path = common.contained(root, name)
    if name != 'README.md' and not (path.parent == root.resolve() / 'topics' and path.suffix == '.md'):
        raise ValueError('expected README.md or topics/<topic>.md')
    return path


def publish(root, name, expected, content):
    path = page_path(root, name)
    if name != 'README.md':
        body = content.decode('utf-8')
        lines = body.splitlines()
        labeled = path.name.startswith('无证据猜想-')
        idea_title = any(line.startswith('# 无证据猜想：') for line in lines)
        idea_section = '## 无证据猜想' in lines
        if any((labeled, idea_title, idea_section)) and not all((labeled, idea_title, idea_section)):
            raise ValueError('use 无证据猜想- in the filename, # 无证据猜想： in the title and ## 无证据猜想 in the body')
        if labeled and '## 有证据的发现' in lines:
            raise ValueError('publish verified findings in an evidence topic')
    # Serialize cooperating editors without leaving a lock file in the wiki.
    fd = os.open(root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        old = path.read_bytes() if path.exists() else None
        if old == content:
            return 'unchanged'
        actual = 'new' if old is None else hashlib.sha256(old).hexdigest()
        if expected != actual:
            raise ValueError('page changed; reread and merge the current text')
        common.atomic_write(path, content)
        return 'updated'
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('init')
    for action in ('read', 'write'):
        sub = commands.add_parser(action)
        sub.add_argument('page')
        if action == 'write':
            sub.add_argument('--expect', required=True, help='read digest, or new for a new page')
    args = parser.parse_args()
    root = common.contained(common.ROOT, 'dream/wiki')
    try:
        if args.action == 'init':
            initialize(root)
        elif args.action == 'read':
            data = page_path(root, args.page).read_bytes()
            print(hashlib.sha256(data).hexdigest(), file=sys.stderr)
            sys.stdout.buffer.write(data)
        else:
            print(publish(root, args.page, args.expect, sys.stdin.read().encode('utf-8')))
    except (OSError, ValueError) as exc:
        parser.exit(2, f'dream wiki: {exc}\n')


if __name__ == '__main__':
    main()
