"""Initialize, check and record work; never launches an agent or a research loop."""
import argparse
import copy
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from tools.common import (ROOT, contained, digest, event, file_ref, identifier, load_run,
    mutation, parse_json, read_pinned, read_plugin, read_json, read_yaml, run_path, safe_boundary,
    save_state, sha, utc, validate, working_file_changes, workspace_file, write_json)
from tools import common as c
from tools.execution import resolve, template, validate_execution
from tools.configuration import effective, overlay, read_base
from tools.module_runtime import compatible, invoke, read_module, resolve_bindings


def init(reference, config_path=None, researchers=None, rounds=None, new_batch=False):
    from tools import study
    from tools.common import run_identifier
    run_identifier(reference)
    study_id = reference.split('/')[0]
    directory = run_path(study_id)
    if (directory/'run.yaml').exists():
        raise ValueError('historical run is read-only; choose a study name')
    # Validate first; the directory lock then serializes number allocation and sealing.
    study.load(study_id, require_sealed=True)
    with study._lock(directory):
        initialization, manifest = study._load(study_id, require_sealed=True)
        if '/' in reference:
            if new_batch:
                raise ValueError('--new-batch takes a study name')
            run_id = reference
        else:
            numbers = sorted(int(p.name) for p in directory.iterdir()
                             if p.name.isascii() and p.name.isdigit())
            initialized = [n for n in numbers if
                           (contained(directory, format(n, '03d'))/'state.json').is_file()]
            number = initialized[-1] if initialized and not new_batch else max(numbers, default=0) + 1
            run_id = f'{study_id}/{number:03d}'
        if not (run_path(run_id)/'state.json').exists():
            return _init(run_id, config_path, researchers, rounds, initialization, manifest)
    # Release the study lock before opening the batch.
    document = read_yaml(run_path(run_id)/'run.yaml')
    if config_path and config_path != document['extends']:
        raise ValueError('edit this batch run.yaml and resume, or request --new-batch')
    if researchers is not None or rounds is not None:
        cfg = overlay(document, read_base(document['extends']))
        for key, value in [('researchers', researchers), ('rounds_per_researcher', rounds)]:
            if value is not None and cfg['budgets'][key] != value:
                raise ValueError('change the budget in this batch run.yaml and resume')
    return resume(run_id)


def _init(run_id, config_path, researchers, rounds, initialization, manifest):
    run = run_path(run_id)
    document_path = contained(run, 'run.yaml')
    if document_path.is_file():
        document = read_yaml(document_path)
        validate('run_document', document)
        if config_path and config_path != document['extends']:
            raise ValueError('draft extends a different base configuration')
        config_path = document['extends']
    else:
        if not config_path:
            raise ValueError('specify --config or prepare a numbered batch run.yaml first')
        document = {'schema_version':8, 'extends':config_path}
    if run.exists():
        # A crash may leave empty output directories before state.json is committed.
        if any(p.is_symlink() or (not p.is_dir() and p != document_path) for p in run.rglob('*')):
            raise ValueError('uninitialized batch already contains files; check its original state')
    shared_ref = file_ref(initialization/'manifest.json')
    if document.get('initialization', shared_ref['path']) != shared_ref['path']:
        raise ValueError('batch must use its own study initialization')
    document['initialization'] = shared_ref['path']
    # A study cannot silently become a different initialization for later batches.
    for prior in run.parent.iterdir():
        if not prior.name.isascii() or not prior.name.isdigit():
            continue
        prior = contained(run.parent, prior.name)
        if prior.is_dir() and (prior/'state.json').is_file():
            previous = read_json(contained(prior,'state.json'))
            if previous.get('configuration',{}).get('initialization') != shared_ref:
                raise ValueError('shared initialization differs from an existing batch')
    base = read_base(config_path)
    cfg = overlay(document, base)
    if not document_path.exists():
        document['budgets'] = {key:cfg['budgets'][key] for key in ('researchers','rounds_per_researcher')}
    for key,value in (('researchers',researchers), ('rounds_per_researcher',rounds)):
        if value is not None:
            document.setdefault('budgets',{})[key] = value
    cfg, lock = _configuration(run_id, document, base, initialization, manifest, shared_ref, utc())
    run.mkdir(exist_ok=True)
    c.sync_directory(run.parent)
    c.atomic_write(document_path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode())
    for relative in ('research/candidates','research/evaluations','research/selections',
                     'research/datasets','research/ideas','final','visualization'):
        contained(run,relative).mkdir(parents=True,exist_ok=True)
    state = {'schema_version':2,'config_sha':digest(cfg),'created_at':cfg['created_at'],
             'configuration':lock,
             'sessions':[], 'deliveries':{k:{} for k in ('dataset','candidate','ideas','figure','report','iteration')},
             'evaluations':{},'selection':None}
    save_state(run,state)
    event(run,'initialized',config_sha=state['config_sha'],initialization_sha=shared_ref['sha'])
    return {'run_id':run_id,'config':str((run/'run.yaml').relative_to(ROOT)),
            'initialization':shared_ref}


def _configuration(run_id, document, base, initialization, manifest, shared_ref, created_at):
    cfg = overlay(document, base)
    cfg['modules'] = resolve_bindings(cfg['modules'])
    validate_execution(cfg)
    for slot in ('task','datasets','ideas'):
        compatible(manifest['modules'][slot],cfg['modules'][slot])
    cfg['task'] = copy.deepcopy(manifest['task'])
    cfg['initialization'] = shared_ref
    for record in manifest['deliveries']['dataset'].values():
        data = contained(initialization, record['manifest']['cases_file']).read_text()
        cap = cfg['budgets']['max_cases_per_dataset']
        if cap is not None and sum(bool(line.strip()) for line in data.splitlines()) > cap:
            raise ValueError('shared dataset exceeds batch case budget')
    if cfg['final']['authorized'] and not any(r['manifest']['purpose']=='final'
                                             for r in manifest['deliveries']['dataset'].values()):
        raise ValueError('authorized final evaluation requires a shared final dataset')
    cfg.update(run_id=run_id, created_at=created_at)
    pinpaths = [*(p for p in sorted((c.ENGINE_ROOT/'tools').glob('*.py')) if p.name != 'archive.py'),
                c.ENGINE_ROOT/'tools/schemas.json']
    cfg['_pins'] = {c.logical_path(p):file_ref(p) for p in pinpaths}
    cfg['implementation_sha'] = c.implementation_digest(cfg)
    validate('config', cfg)
    base_ref = file_ref(contained(ROOT,document['extends']))
    if read_base(document['extends']) != base:
        raise ValueError('base configuration changed during initialization')
    read_pinned(shared_ref, 'shared initialization manifest')
    lock = {key:copy.deepcopy(cfg[key]) for key in ('run_id','created_at','task','modules','_pins','initialization','implementation_sha')}
    lock.update(base=base_ref, document_sha=digest(document))
    if effective(document,lock) != cfg:
        raise ValueError('resolved configuration mismatch')
    return cfg, lock


def resume(run_id, scorer=None):
    """Adopt repaired working files while preserving this batch's inputs and history."""
    from tools import study
    require_mutable(run_id)
    run = run_path(run_id)
    with mutation(run):
        state = read_json(run/'state.json')
        old = state['configuration']
        initialization, manifest = study.load(run_id.split('/')[0], require_sealed=True)
        shared = file_ref(initialization/'manifest.json')
        if old['run_id'] != run_id or old['initialization'] != shared:
            raise ValueError('shared initialization or run identity changed')
        document = read_yaml(run/'run.yaml')
        cfg, lock = _configuration(run_id, document, read_base(document['extends']),
                                   initialization, manifest, shared, old['created_at'])
        previous = state['config_sha']
        state.update(config_sha=digest(cfg), configuration=lock)
        _, _, view = load_run(run_id, proposed=state)
        if previous != state['config_sha'] or scorer:
            # A live local measurement keeps this shared lease until its state commit.
            lease = run/'research/.measurement.lock'
            with lease.open('a+') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if scorer:
                    c.repair_scorer(run, cfg, view, Path(scorer))
                if previous != state['config_sha']:
                    view.setdefault('previous_configurations', []).append(previous)
                    for session in view['sessions']:
                        if session['status'] == 'active':
                            session.setdefault('config_history', []).append(state['config_sha'])
                save_state(run, view)
                event(run, 'resumed', previous_config_sha=previous, config_sha=state['config_sha'],
                      scorer_overrides=view.get('scorer_overrides', {}))
        return dict(run_id=run_id, resumed=True, config=str((run/'run.yaml').relative_to(ROOT)),
                    initialization=shared, stale_evaluations=sorted(view.get('stale_evaluations', [])))


def assert_time(cfg):
    elapsed=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(cfg['created_at'])).total_seconds()
    if elapsed >= cfg['budgets']['run_wall_s']:
        raise ValueError('run admission deadline reached')


def deliver(run,cfg,state,kind,relative,slot):
    if kind not in state['deliveries'] or slot not in cfg['modules']:
        raise ValueError('unknown delivery kind or module slot')
    entry=cfg['modules'][slot]
    if kind not in entry['produces']:
        raise ValueError('module does not declare this output contract')
    prefix = {'dataset':'research/datasets', 'ideas':'research/ideas',
              'candidate':'research/candidates','iteration':'research/selections',
              'report':'final','figure':'visualization'}[kind]
    def local_file(path):
        target = contained(run,path)
        if not target.is_relative_to(contained(run,prefix)):
            raise ValueError('batch '+kind+' files must be under '+prefix+'/')
        return target
    manifest_path=local_file(relative)
    data=read_json(manifest_path)
    validate(kind,data,cfg)
    if kind=='dataset' and data['purpose']!='challenge':
        raise ValueError('anchor/final belong to shared initialization; batch data must be challenge')
    if state['selection'] and kind in ('candidate','dataset','ideas','iteration'):
        raise ValueError('research artifacts cannot be added after selection')
    files={relative:sha(manifest_path)}
    def add(path):
        p=local_file(path)
        if not p.is_file():raise ValueError('missing delivery: '+path)
        files[path]=sha(p)
        return p
    key=data.get(kind+'_id',Path(relative).stem)
    identifier(key)
    if key in state['deliveries'][kind]:
        raise ValueError('delivery ID already exists; create a new version')
    if 'validate_delivery' in entry['handlers']:
        invoke(cfg,slot,'validate_delivery',run,cfg,state,data,add,entry)
    state['deliveries'][kind][key]={'manifest':data,'manifest_path':relative,'files':files,
          'module_sha':entry['sha'],'module_id':entry['id'],
          'config_sha':state['config_sha'],'delivered_at':utc()}
    save_state(run,state);event(run,'delivered',artifact_kind=kind,id=key,module_sha=entry['sha'])
    result = {'kind':kind,'id':key,'checked_files':len(files)}
    if kind == 'report' and 'dream_context' in entry['handlers']:
        result['next'] = dict(operation='final-dream', run_id=cfg['run_id'])
    return result


def select(run,cfg,state,candidate):
    return invoke(cfg,'final','select',run,cfg,state,candidate)


def _slot(args):
    return getattr(args,'module',None) or getattr(args,'plugin',None)


def require_mutable(run_id):
    if read_yaml(run_path(run_id)/'run.yaml').get('schema_version') != 8:
        raise ValueError('historical run is read-only; initialize a new run')


def inspect(args):
    run,cfg,state=load_run(args.run,read_only=True)
    historical=read_yaml(run/'run.yaml')['schema_version'] in (2,3,4,6,7)
    if args.command=='config':return cfg
    if args.command=='check':
        if not historical:
            invoke(cfg, 'research', 'audit', run, cfg, state)
        return {'ok':True,'config_sha':state['config_sha'],
                'sealed_artifacts':sum(map(len,state['deliveries'].values())),
                'implementation_sha':cfg.get('implementation_sha'),
                'historical_read_only':historical,
                'working_file_changes':working_file_changes(cfg) if historical else []}
    if args.command in ('module','plugin'):
        entry=cfg.get('modules',cfg.get('plugins',{}))[args.slot]
        return {'slot':args.slot,'sha':entry['sha'],
                'text':read_pinned(entry,'module file').decode(),
                'execution':resolve(cfg,args.slot) if not historical else None,
                'historical_read_only':historical}
    selection = state['selection']
    if selection and selection.get('evaluation_id') in state.get('stale_evaluations', []):
        selection = dict(selection, frozen_score=selection['score'], score=None, requires_rescoring=True)
    elif selection and selection.get('evaluation_id') in state.get('evaluation_history', {}):
        current = read_json(contained(run, state['evaluations'][selection['evaluation_id']]))
        selection = dict(selection, frozen_score=selection['score'], score=current.get('score'))
    return {'run_id':args.run,'sessions':state['sessions'],
            'artifacts':{k:list(v) for k,v in state['deliveries'].items()},
            'evaluations':len(state['evaluations']),'selection':selection,
            'budgets':cfg['budgets'], 'stale_evaluations':state.get('stale_evaluations', []),
            'historical_read_only':historical}


def command(args):
    if args.command.startswith('study-'):
        from tools import study
        if args.command=='study-init':return study.create(args.study,args.config)
        if args.command=='study-deliver':return study.deliver(args.study,args.kind,args.manifest,_slot(args))
        if args.command=='study-seal':return study.seal(args.study)
        if args.command=='study-check':return study.check(args.study)
        raise ValueError('unknown study command')
    if args.command=='launch-spec':
        if args.config:
            return resolve(template(args.config),_slot(args),args.role)
        require_mutable(args.run)
        with mutation(run_path(args.run)):
            _,cfg,_=load_run(args.run)
            assert_time(cfg)
            return resolve(cfg,_slot(args),args.role)
    if args.command=='init':return init(args.run,getattr(args,'config',None),
                                      getattr(args,'researchers',None),getattr(args,'rounds',None),getattr(args,'new_batch',False))
    if args.command == 'resume':return resume(args.run, getattr(args, 'scorer', None))
    if args.command == 'retry-evaluation':
        _, cfg, _ = load_run(args.run)
        return invoke(cfg, 'evaluation', 'retry', args.run, args.evaluation, getattr(args, 'remote_stopped', False))
    if args.command == 'reconcile-evaluation':
        require_mutable(args.run)
        _, cfg, _ = load_run(args.run)
        return invoke(cfg, 'evaluation', 'reconcile', args.run, args.evaluation)
    if args.command in ('config','check','status','module','plugin'):
        return inspect(args)
    require_mutable(args.run)
    run=run_path(args.run)
    with mutation(run):
        run,cfg,state=load_run(args.run,replacing_slot=args.slot if args.command=='bind' else None)
        if args.command=='bind':
            safe_boundary(state);assert_time(cfg)
            if args.slot not in cfg['modules']:raise ValueError('unknown slot')
            if args.slot=='task':
                raise ValueError('task belongs to shared initialization; use a new study')
            document=read_yaml(run/'run.yaml')
            base=read_base(document['extends'])
            merged=overlay(document,base)
            params=merged['modules'][args.slot].get('params',{})
            new=read_module(args.path,params);old=cfg['modules'][args.slot]
            compatible(old,new)
            proposed=copy.deepcopy(cfg);proposed['modules'][args.slot]=new
            validate('config',proposed);validate_execution(proposed)
            document.setdefault('modules',{}).setdefault(args.slot,{})['use']=args.path
            lock=copy.deepcopy(state['configuration'])
            lock['modules'][args.slot]=new;lock['document_sha']=digest(document)
            lock['implementation_sha']=c.implementation_digest(lock)
            cfg=effective(document,lock);validate_execution(cfg)
            previous=file_ref(run/'run.yaml');previous_config_sha=state['config_sha']
            old_config=(run/'run.yaml').read_bytes();old_state=(run/'state.json').read_bytes()
            try:
                (run/'run.yaml').write_text(yaml.safe_dump(document,allow_unicode=True,sort_keys=False))
                state['configuration']=lock;state['config_sha']=digest(cfg);save_state(run,state)
            except Exception:
                (run/'run.yaml').write_bytes(old_config);(run/'state.json').write_bytes(old_state)
                raise
            event(run,'module_bound',slot=args.slot,old=old['sha'],new=new['sha'],
                  previous_config=previous['sha'],previous_config_sha=previous_config_sha,
                  config_sha=state['config_sha'])
            return {'slot':args.slot,'module':new['id'],'sha':new['sha']}
        if args.command == 'reconcile-exchange':
            return invoke(cfg, 'research', 'reconcile', run, cfg, state)
        if args.command == 'final-dream':
            return invoke(cfg, 'final', 'dream_context', run, cfg, state)
        if args.command == 'start-final-dream':
            return invoke(cfg, 'final', 'dream_start', run, cfg, state, args.native_id, args.replaces)
        if args.command == 'finish-final-dream':
            return invoke(cfg, 'final', 'dream_finish', run, cfg, state, args.native_id, args.page)
        if args.command == 'recommendation':
            return invoke(cfg, 'final', 'recommendation', run, cfg, state)
        if args.command == 'method-reports':
            return invoke(cfg, 'selections', 'pending_reports', run, cfg, state)
        if args.command == 'method-report-context':
            return invoke(cfg, 'selections', 'report_context', run, cfg, state, args.session)
        if args.command == 'method-report-submit':
            return invoke(cfg, 'selections', 'report_submit', run, cfg, state, args.session,
                          read_json(contained(run, args.manifest)))
        if args.command=='deliver':return deliver(run,cfg,state,args.kind,args.manifest,_slot(args))
        if args.command=='select':return select(run,cfg,state,args.candidate)
        if args.command=='iteration-context':
            query = getattr(args, 'wiki_query', None)
            options = {'wiki_query': query} if query else {}
            return invoke(cfg,'selections','context',run,cfg,state,getattr(args,'actor',None), **options)
        if args.command=='next':
            raise ValueError('next plans are retired; read iteration-context and deliver an algorithm iteration')
        actor=identifier(args.actor)
        if args.command=='begin-session':
            if _slot(args) != 'research':
                raise ValueError('begin-session counts research delegates only')
            if getattr(args,'plan',None):
                raise ValueError('--plan is retired; use --idea or --iteration')
            return invoke(cfg,'research','begin_session',run,cfg,state,actor,args.role,
                          getattr(args,'idea',None),getattr(args,'iteration',None),utc())
        if args.command=='resume-session':
            return invoke(cfg,'research','resume_session',run,cfg,state,actor,args.native_id,args.stopped_at)
        if args.command=='activate-session':
            return invoke(cfg,'research','activate_session',run,cfg,state,actor,args.native_id,utc())
        if args.command=='end-session':
            return invoke(cfg,'research','end_session',run,cfg,state,actor,args.status,
                          getattr(args,'native_id',None),utc())
        raise ValueError('unknown command')


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    for name in ('study-init','study-deliver','study-seal','study-check'):
        q=sub.add_parser(name);q.add_argument('study')
        if name=='study-init':q.add_argument('--config',required=True)
        if name=='study-deliver':
            q.add_argument('kind',choices=['dataset','ideas']);q.add_argument('manifest')
            q.add_argument('--module',required=True)
    spec=sub.add_parser('launch-spec',help='Resolve exact native launch options without calling a model')
    source=spec.add_mutually_exclusive_group(required=True)
    source.add_argument('--config');source.add_argument('--run')
    spec.add_argument('--module','--plugin',dest='module',required=True);spec.add_argument('--role',default='self')
    for name in ('init','resume','retry-evaluation','resume-session','check','module','plugin','status','config','bind','deliver','select','recommendation','final-dream','start-final-dream','finish-final-dream','method-reports','method-report-context','method-report-submit','reconcile-exchange','reconcile-evaluation','iteration-context','begin-session','activate-session','end-session'):
        q=sub.add_parser(name);q.add_argument('run')
        if name=='resume':q.add_argument('--scorer')
        if name=='retry-evaluation':q.add_argument('--remote-stopped', action='store_true')
        if name=='init':
            q.add_argument('--new-batch', action='store_true')
            q.add_argument('--config');q.add_argument('--researchers',type=int);q.add_argument('--rounds',type=int)
        if name in ('bind','module','plugin'):q.add_argument('slot')
        if name=='bind':q.add_argument('path')
        if name=='deliver':
            q.add_argument('kind');q.add_argument('manifest');q.add_argument('--module','--plugin',dest='module',required=True)
        if name in ('method-report-context', 'method-report-submit'):q.add_argument('--session',required=True)
        if name=='method-report-submit':q.add_argument('--manifest',required=True)
        if name in ('retry-evaluation','reconcile-evaluation'):q.add_argument('--evaluation',required=True)
        if name in ('start-final-dream','finish-final-dream'):
            q.add_argument('--native-id',required=True)
        if name=='start-final-dream':
            q.add_argument('--replaces')
        if name=='finish-final-dream':
            q.add_argument('--page')
        if name=='select':q.add_argument('--candidate',required=True)
        if name=='iteration-context':
            q.add_argument('--actor')
            q.add_argument('--wiki-query', help='Include matching domain wiki excerpts in this context')
        if name.endswith('session'):q.add_argument('--actor',required=True)
        if name=='begin-session':
            q.add_argument('--module','--plugin',dest='module',required=True);q.add_argument('--role',default='researcher')
            source=q.add_mutually_exclusive_group(required=True)
            source.add_argument('--idea');source.add_argument('--iteration')
        if name=='resume-session':q.add_argument('--stopped-at')
        if name in ('activate-session','resume-session'):q.add_argument('--native-id',required=True)
        if name=='end-session':
            q.add_argument('--status',choices=['completed','failed'],required=True);q.add_argument('--native-id')
    args=p.parse_args()
    try:print(json.dumps(command(args),ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(1)


if __name__=='__main__':main()
