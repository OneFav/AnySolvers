"""Offer task-aware starting limits, without running code or changing configuration."""
import math


def recommend_compute(protocol, dataset_counts, current_compute, resource_contract,
                      measured=None):
    """Measured values are caller-supplied public-baseline observations, not proof."""
    if protocol.get('kind') not in ('function', 'episode'):
        raise ValueError('unsupported evaluation protocol')
    if not dataset_counts or any(type(n) is not int or n <= 0 for n in dataset_counts.values()):
        raise ValueError('explicit, positive dataset counts are required')
    reserve = resource_contract['case_overhead_reserve_s']
    slots = current_compute['max_parallel_evaluations']
    if type(reserve) is not int or reserve < 0 or not 1 <= slots <= resource_contract['max_parallel_evaluations']:
        raise ValueError('invalid evaluation resource contract')
    episode = protocol['kind'] == 'episode'
    limits = dict(current_compute['limits'])
    limits.update(wall_s=120 if episode else 60,
                  cpu_s=60 if episode else 30,
                  memory_mb=1024 if episode else 512)
    basis = 'heuristic_starter_not_measured'
    if measured is not None:
        if (not isinstance(measured, dict) or not isinstance(measured.get('note'), str)
                or not measured['note'].strip()
                or set(measured) - {'note', 'wall_s', 'cpu_s', 'peak_memory_mb'}):
            raise ValueError('public baseline measurements need a source note')
        values = {k: v for k, v in measured.items() if k != 'note'}
        if not values or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values.values()):
            raise ValueError('public baseline measurements must be positive finite numbers')
        if 'wall_s' in values:
            limits['wall_s'] = max(10, math.ceil(4 * values['wall_s']))
            limits['cpu_s'] = max(2, math.ceil(limits['wall_s'] / 2))
        if 'cpu_s' in values:
            limits['cpu_s'] = max(2, math.ceil(4 * values['cpu_s']))
        if 'peak_memory_mb' in values:
            limits['memory_mb'] = max(128, 64 * math.ceil(2 * values['peak_memory_mb'] / 64))
        basis = 'caller_reported_public_baseline_with_margin_not_remeasured'
    limits['wall_s'] = max(limits['wall_s'], limits['cpu_s'],
                           math.ceil(protocol.get('action_wall_s', 0)))
    count = max(dataset_counts.values())
    margin = max(60, math.ceil(count * limits['wall_s'] * 0.1))
    evaluation_wall_s = count * (limits['wall_s'] + reserve) + margin
    return {
        'basis': basis,
        'protocol': dict(protocol),
        'dataset_counts': dict(dataset_counts),
        'configuration_patch': {'compute': {
            'max_parallel_evaluations': slots,
            'limits': limits,
            'evaluation_wall_s': evaluation_wall_s}},
        'capacity_to_check': {
            'cpu_cores_including_host_reserve': slots + 1,
            'memory_mb_including_host_reserve': slots * limits['memory_mb'] + 1024},
        'formula': {'largest_dataset_cases': count, 'case_wall_s': limits['wall_s'],
                    'case_overhead_reserve_s': reserve, 'additional_margin_s': margin},
        'notes': [
            'Instance counts must be agreed during initialization; null capacity does not choose counts.',
            'These are proposed limits, not a performance guarantee or a sandbox preflight result.',
            'Measured inputs, if supplied, must describe authorized public-baseline checks; this helper does not verify or repeat them.',
            'Write the chosen limits to the configuration and tell the user before sealing and starting research.',
            'Evaluation wall time excludes queueing; action and case limits remain independent.',
            'Recalculate the evaluation limit before a new batch if a larger dataset is introduced.'
        ]}
