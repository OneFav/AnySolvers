"""Persist measured facts, not a duplicate of every episode observation.

Called only after trusted scoring and cleanup. It never changes execution,
scores, metrics, resource accounting or request identity. Old sealed records
are not rewritten by this module.
"""
import hashlib
import json

POLICIES = ('summary', 'full')
DIAGNOSTIC_BYTES = 4096
FAILURE_SAMPLE_BYTES = 4096
FAILURE_SAMPLE_STEPS = 3


def policy(config):
    value = config.get('modules', {}).get('evaluation', {}).get('params', {}).get('trace_retention', 'summary')
    return validate_policy(value)


def validate_policy(value):
    if value not in POLICIES:
        raise ValueError('trace_retention must be summary or full')
    return value


def fingerprint(value):
    """Hash incrementally without serializing a second whole trace in memory."""
    sha = hashlib.sha256()
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for chunk in encoder.iterencode(value):
        data = chunk.encode('utf-8')
        sha.update(data)
        size += len(data)
    return dict(sha256=sha.hexdigest(), canonical_bytes=size)


def retain(result, mode='summary'):
    validate_policy(mode)
    value = dict(result)
    marker = dict(version=1, trace_retention=mode)
    if value.get('storage_policy') == marker:
        return value
    if mode == 'summary' and 'trace' in value:
        trace = value.pop('trace')
        value['trace_summary'] = dict(fingerprint(trace), stored=False,
                                     records=len(trace) if isinstance(trace, list) else None,
                                     encoding='canonical-json-v1')
        # A small terminal window helps diagnose errors without retaining the
        # potentially multi-megabyte observations from an entire failed run.
        if isinstance(trace, list) and (not value.get('valid') or value.get('infrastructure_error')):
            sample = []
            for item in reversed(trace[-FAILURE_SAMPLE_STEPS:]):
                proposed = [item, *sample]
                if fingerprint(proposed)['canonical_bytes'] > FAILURE_SAMPLE_BYTES:
                    break
                sample = proposed
            value['trace_sample'] = sample
            value['trace_summary']['sample_records'] = len(sample)
    if isinstance(value.get('diagnostic'), dict):
        bounded, metadata = {}, {}
        for name, content in value['diagnostic'].items():
            if not isinstance(content, str):
                raise ValueError('Execution diagnostic streams must be text')
            data = content.encode('utf-8')
            metadata[name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                  truncated=len(data) > DIAGNOSTIC_BYTES)
            if len(data) > DIAGNOSTIC_BYTES:
                half = DIAGNOSTIC_BYTES // 2
                bounded[name] = (data[:half].decode('utf-8', errors='ignore')
                                 + '\n[truncated]\n'
                                 + data[-half:].decode('utf-8', errors='ignore'))
            else:
                bounded[name] = content
        value['diagnostic'], value['diagnostic_summary'] = bounded, metadata
    value['storage_policy'] = marker
    return value
