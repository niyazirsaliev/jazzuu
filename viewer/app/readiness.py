"""Canonical pure projection of durable recording pipeline state."""
from __future__ import annotations

STAGES = ('asr', 'summary', 'mindmap', 'card')
OPEN_STATES = frozenset(('queued', 'processing', 'retry_wait'))


def project(states):
    """Return one reader-facing state from a stage->state mapping."""
    normalized = {stage: states.get(stage) for stage in STAGES}
    if not any(normalized.values()):
        return {'state': 'queued', 'ready': False, 'stages': normalized}
    failed = next((stage for stage in STAGES if normalized[stage] == 'failed'), None)
    if failed:
        return {'state': 'failed', 'ready': False, 'stage': failed,
                'stages': normalized}
    active = next((stage for stage in STAGES
                   if normalized[stage] in OPEN_STATES), None)
    if active:
        return {'state': normalized[active], 'ready': False, 'stage': active,
                'stages': normalized}
    ready = normalized['asr'] == 'done'
    return {'state': 'done' if ready else 'queued', 'ready': ready,
            'stages': normalized}
