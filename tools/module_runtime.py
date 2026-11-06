"""Load declared module packages and invoke trusted handlers; no research scheduler."""
import importlib.util
from pathlib import Path

import yaml

from tools import common as c


def entry_path(use):
    path = c.workspace_file(use)
    path = path / 'MODULE.md' if path.is_dir() else path
    parts = path.relative_to(c.ENGINE_ROOT).parts
    if len(parts) != 4 or parts[0] != 'modules' or parts[-1] != 'MODULE.md':
        raise ValueError('active module must be modules/<kind>/<method>/MODULE.md; spare_modules is inactive')
    return c.workspace_file(str(path.relative_to(c.ENGINE_ROOT)))


def dependency(entry, relative):
    """A method owns its complete package; sibling/standby files are not dependencies."""
    main = entry_path(entry['path'])
    parts = Path(entry['path']).parts
    if parts[1] != entry['kind']:
        raise ValueError('module kind must match modules/<kind>/<method>/')
    if Path(relative).is_absolute():
        raise ValueError('module dependency must be relative')
    # Check symlinks before normalizing '..', then enforce the owning module boundary.
    current = main.parent
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink in module dependency')
    path = current.resolve()
    if not path.is_relative_to(main.parent):
        raise ValueError('module dependency escapes its package')
    return c.workspace_file(str(path.relative_to(c.ENGINE_ROOT)))


def read_module(use, params=None):
    from tools.configuration import merge
    path = entry_path(use)
    data = path.read_bytes()
    pieces = data.decode().split('---', 2)
    if len(pieces) != 3 or pieces[0].strip():
        raise ValueError('module needs YAML frontmatter')
    meta = yaml.load(pieces[1], Loader=c.StrictLoader)
    c.validate('module', meta)
    entry = dict(meta, **c.file_ref(path))
    entry['resources'] = {}
    relatives = [*meta['contracts'].values(), *meta['handlers'].values(), *meta['files']]
    if meta.get('parameters'):
        relatives.append(meta['parameters'])
    for relative in relatives:
        file = dependency(entry, relative)
        entry['resources'][str(file.relative_to(c.ENGINE_ROOT))] = c.file_ref(file)
    entry['params'] = merge(meta['defaults'], params or {})
    if meta.get('parameters'):
        c.validate_schema(c.read_json(dependency(entry, meta['parameters'])), entry['params'])
    elif entry['params']:
        raise ValueError('module has no parameter schema: ' + meta['id'])
    c.validate('module_ref', entry)
    return entry


def resolve_bindings(bindings):
    result = {slot:read_module(binding['use'], binding.get('params'))
              for slot,binding in bindings.items()}
    for slot,entry in result.items():
        if entry['kind'] != slot:
            raise ValueError('module kind does not match slot: ' + slot)
    return result


def verify(entry):
    c.read_pinned(entry, 'module file')
    for ref in entry['resources'].values():
        c.read_pinned(ref, 'module dependency')


def output_schema(entry, kind):
    if kind not in entry['contracts']:
        raise ValueError('module does not declare output contract: ' + kind)
    path = dependency(entry, entry['contracts'][kind])
    ref = entry['resources'][str(path.relative_to(c.ENGINE_ROOT))]
    return c.parse_json(c.read_pinned(ref, 'module contract').decode())


def compatible(old, new):
    if old['kind'] != new['kind'] or set(old['produces']) != set(new['produces']):
        raise ValueError('incompatible output contract or module kind')
    if not set(old['handlers']).issubset(new['handlers']):
        raise ValueError('replacement is missing a required module operation')
    # Compare recorded schema hashes, so an edited old package need not be loaded to bind.
    for kind in old['contracts']:
        if kind not in new['contracts']:
            raise ValueError('incompatible output contract: ' + kind)
        a = str(dependency(old, old['contracts'][kind]).relative_to(c.ENGINE_ROOT))
        b = str(dependency(new, new['contracts'][kind]).relative_to(c.ENGINE_ROOT))
        if old['resources'][a]['sha'] != new['resources'][b]['sha']:
            raise ValueError('incompatible output contract version: ' + kind)


def invoke(cfg, slot, operation, *args, **kwargs):
    """Only module-owned reviewed Python is imported; candidate source never is."""
    entry = cfg['modules'][slot]
    verify(entry)
    if operation not in entry['handlers']:
        raise ValueError('module has no handler: ' + slot + '/' + operation)
    path = dependency(entry, entry['handlers'][operation])
    spec = importlib.util.spec_from_file_location('anysolvers_module_' + c.sha(path), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, operation)(*args, **kwargs)
