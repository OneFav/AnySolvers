"""Validate a problem definition; executable assets belong to datasets."""
from tools import common as c


def prepare(cfg):
    path = c.workspace_file(cfg['task'])
    task = c.read_json(path)
    c.validate('task', task, cfg)
    files = {key:c.file_ref(c.contained(path.parent, task[key])) for key in
             ('description_file',)}
    files['manifest'] = c.file_ref(path)
    return {'task_id':task['task_id'],'path':str(path.relative_to(c.ROOT)),
            'files':files,'objective':task['objective']}
