"""Seal a report only against recorded, ended measurements."""
from tools.common import contained, read_json


def validate_delivery(run,cfg,state,data,add,entry):
    for eid in data['evaluation_ids']:
        if eid not in state['evaluations']:raise ValueError('unknown evaluation reference')
        if read_json(contained(run,state['evaluations'][eid]))['status'] not in ('completed','failed'):
            raise ValueError('referenced evaluation not complete')
    add(data['report_file'])
