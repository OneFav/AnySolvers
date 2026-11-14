"""Prepare the native writer handoff and verify its published domain topic."""
import hashlib
from pathlib import Path
import re

from tools import common as c
from tools.execution import resolve
from tools.wiki_sources import collect


def _materials(cfg, state):
    if not state['deliveries']['report'] or any(s['status'] == 'active' for s in state['sessions']):
        raise ValueError('final Dream needs a delivered report and settled research sessions')
    sids = state.get('extensions', {}).get('selection_reports', {})
    materials = collect(cfg['run_id'], report_ids=sids)
    shared = {eid for finding in materials['findings'].values() for eid in finding['evidence_ids']}
    for sid, report in materials['reports'].items():
        for eid in report['evidence_ids']:
            row = materials['evaluations'].get(eid, {})
            if row.get('citation') and eid not in shared and row['candidate_id'] != 'baseline':
                row['citation'] += ' --report ' + sid
                shared.add(eid)
    return materials


def dream_context(run, cfg, state):
    materials = _materials(cfg, state)
    extensions = state.get('extensions', {})
    active = extensions.get('final_dream_writer')
    receipt = extensions.get('final_dream')
    if not active and receipt and receipt['materials_sha'] == c.digest(materials):
        if not receipt['page'] or c.contained(c.ROOT, 'dream/wiki/' + receipt['page']).is_file():
            return dict(status='completed', receipt=receipt)
    spec = resolve(cfg, 'final', 'dream')
    return dict(status='active' if active else 'ready', native_id=active, run_id=cfg['run_id'], materials=materials,
                spawn_options=spec['spawn_options'],
                instructions=str(Path(cfg['modules']['final']['path']).with_name('WRITER.md')))


def dream_start(run, cfg, state, native_id, replaces=None):
    if not native_id.strip():
        raise ValueError('actual native_id required')
    job = dream_context(run, cfg, state)
    extensions = state.setdefault('extensions', {})
    active = extensions.get('final_dream_writer')
    if active == native_id:
        return job
    if job['status'] == 'completed' or replaces != active:
        raise ValueError('check existing Dream writer or completed receipt first')
    extensions['final_dream_writer'] = native_id
    c.save_state(run, state)
    c.event(run, 'final-dream-writer', native_id=native_id, replaces=replaces)
    return dict(status='active', native_id=native_id)


def dream_finish(run, cfg, state, native_id, page):
    if not native_id.strip():
        raise ValueError('actual native_id required')
    extensions = state.setdefault('extensions', {})
    active = extensions.get('final_dream_writer')
    if native_id != active:
        prior = extensions.get('final_dream', {})
        if not active and prior.get('native_id') == native_id and prior.get('page') == page:
            return prior
        raise ValueError('completion requires the registered native Dream writer')
    materials = _materials(cfg, state)
    available = {row['citation'] for row in materials['evaluations'].values() if 'citation' in row}
    receipt = dict(native_id=native_id, materials_sha=c.digest(materials), page=page)
    if page:
        if len(Path(page).parts) != 2 or Path(page).parent != Path('topics') or Path(page).suffix != '.md':
            raise ValueError('expected a Markdown topic under topics/')
        if Path(page).name.startswith('无证据猜想-'):
            raise ValueError('final Dream requires an evidence topic')
        data = c.contained(c.ROOT, 'dream/wiki/' + page).read_bytes()
        body = data.decode()
        citations = dict(re.findall(r'^\[\^([^\]]+)\]:[ \t]*(.+?)\s*$', body, re.M))
        sections = re.findall(r'^## 有证据的发现[ \t]*\n(.*?)(?=^## |\Z)', body, re.M | re.S)
        text = '\n'.join(line for part in sections for line in part.splitlines() if not line.startswith('[^'))
        refs = re.findall(r'\[\^([^\]]+)\]', text)
        cited = {citations.get(key, '') for key in refs}
        current = {ref for ref in cited if ref.startswith(cfg['run_id'] + ' ')}
        if not current or current - available:
            raise ValueError('findings must cite a current shareable evaluation from this batch')
        receipt.update(page_sha=hashlib.sha256(data).hexdigest(), citations=sorted(available.intersection(cited)))
    elif available:
        raise ValueError('publish a cited wiki page for the available development evidence')
    del extensions['final_dream_writer']
    extensions['final_dream'] = receipt
    c.save_state(run, state)
    c.event(run, 'final-dream-finished', **receipt)
    return receipt
