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
from tools.execution import resolve, template, validate_execution
from tools.configuration import effective, overlay, read_base
from tools.module_runtime import compatible, invoke, read_module, resolve_bindings


def init(reference, config_path=None, researchers=None, rounds=None):
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
            run_id = reference
        else:
            numbers = [int(p.name) for p in directory.iterdir()
                       if p.name.isascii() and p.name.isdigit()]
            run_id = study_id + '/' + format(max(numbers,default=0)+1,'03d')
        return _init(run_id, config_path, researchers, rounds, initialization, manifest)


def _init(run_id, config_path, researchers, rounds, initialization, manifest):
    run = run_path(run_id)
    document_path = contained(run, 'run.yaml')
    if run.exists():
        if config_path or not document_path.is_file() or any(p.name!='run.yaml' for p in run.iterdir()):
            raise ValueError('run exists; choose a new number or initialize a run.yaml-only draft')
        document = read_yaml(document_path)
        validate('run_document', document)
        config_path = document['extends']
    else:
        if not config_path:
            raise ValueError('specify --config or prepare a numbered batch run.yaml first')
        document = {'schema_version':8, 'extends':config_path}
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
    if not run.exists():
        document['budgets'] = {key:cfg['budgets'][key] for key in ('researchers','rounds_per_researcher')}
    for key,value in (('researchers',researchers), ('rounds_per_researcher',rounds)):
        if value is not None:
            document.setdefault('budgets',{})[key] = value
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
    cfg.update(run_id=run_id, created_at=utc())
    pinpaths = [*(p for p in sorted((ROOT/'tools').glob('*.py')) if p.name != 'archive.py'),
                ROOT/'tools/schemas.json']
    cfg['_pins'] = {str(p.relative_to(ROOT)):file_ref(p) for p in pinpaths}
    validate('config', cfg)
    base_ref = file_ref(contained(ROOT,config_path))
    if read_base(config_path) != base:
        raise ValueError('base configuration changed during initialization')
    read_pinned(shared_ref, 'shared initialization manifest')
    lock = {key:copy.deepcopy(cfg[key]) for key in ('run_id','created_at','task','modules','_pins','initialization')}
    lock.update(base=base_ref, document_sha=digest(document))
    if effective(document,lock) != cfg:
        raise ValueError('resolved configuration mismatch')
    run.mkdir(exist_ok=True)
    for relative in ('research/candidates','research/evaluations','research/selections',
                     'research/datasets','research/ideas','final','visualization'):
        contained(run,relative).mkdir(parents=True,exist_ok=True)
    document_path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    state = {'schema_version':2,'config_sha':digest(cfg),'created_at':cfg['created_at'],
             'configuration':lock,
             'sessions':[], 'deliveries':{k:{} for k in ('dataset','candidate','ideas','figure','report','iteration')},
             'evaluations':{},'selection':None}
    save_state(run,state)
    event(run,'initialized',config_sha=state['config_sha'],initialization_sha=shared_ref['sha'])
    return {'run_id':run_id,'config':str((run/'run.yaml').relative_to(ROOT)),
            'initialization':shared_ref}


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
    return {'kind':kind,'id':key,'checked_files':len(files)}


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
        return {'ok':True,'config_sha':state['config_sha'],
                'sealed_artifacts':sum(map(len,state['deliveries'].values())),
                'historical_read_only':historical,
                'working_file_changes':working_file_changes(cfg) if historical else []}
    if args.command in ('module','plugin'):
        entry=cfg.get('modules',cfg.get('plugins',{}))[args.slot]
        return {'slot':args.slot,'sha':entry['sha'],
                'text':read_pinned(entry,'module file').decode(),
                'execution':resolve(cfg,args.slot) if not historical else None,
                'historical_read_only':historical}
    return {'run_id':args.run,'sessions':state['sessions'],
            'artifacts':{k:list(v) for k,v in state['deliveries'].items()},
            'evaluations':len(state['evaluations']),'selection':state['selection'],
            'budgets':cfg['budgets'],'historical_read_only':historical}


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
                                      getattr(args,'researchers',None),getattr(args,'rounds',None))
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
            if args.slot=='evaluation' and state['evaluations']:
                raise ValueError('task/evaluation protocol is fixed after measurement; use a new run')
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
        if args.command=='deliver':return deliver(run,cfg,state,args.kind,args.manifest,_slot(args))
        if args.command=='select':return select(run,cfg,state,args.candidate)
        if args.command=='iteration-context':
            return invoke(cfg,'selections','context',run,cfg,state,getattr(args,'actor',None))
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
    for name in ('init','check','module','plugin','status','config','bind','deliver','select','iteration-context','begin-session','activate-session','end-session'):
        q=sub.add_parser(name);q.add_argument('run')
        if name=='init':
            q.add_argument('--config');q.add_argument('--researchers',type=int);q.add_argument('--rounds',type=int)
        if name in ('bind','module','plugin'):q.add_argument('slot')
        if name=='bind':q.add_argument('path')
        if name=='deliver':
            q.add_argument('kind');q.add_argument('manifest');q.add_argument('--module','--plugin',dest='module',required=True)
        if name=='select':q.add_argument('--candidate',required=True)
        if name=='iteration-context':q.add_argument('--actor')
        if name.endswith('session'):q.add_argument('--actor',required=True)
        if name=='begin-session':
            q.add_argument('--module','--plugin',dest='module',required=True);q.add_argument('--role',default='researcher')
            source=q.add_mutually_exclusive_group(required=True)
            source.add_argument('--idea');source.add_argument('--iteration')
        if name=='activate-session':q.add_argument('--native-id',required=True)
        if name=='end-session':
            q.add_argument('--status',choices=['completed','failed'],required=True);q.add_argument('--native-id')
    args=p.parse_args()
    try:print(json.dumps(command(args),ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__,'message':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(1)


if __name__=='__main__':main()
