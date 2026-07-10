"""
Per-workspace resource caps (MULTI_TENANCY_DESIGN.md §4.3).

Hosted-demo workspaces are free and anonymous, so every user-creatable
row type gets a hard per-workspace ceiling — enough to explore the
product, far too little to squat storage or bulk-import real data.

No-ops everywhere that isn't a hosted visitor workspace: clone-and-own
deployments (flag off), platform-context requests (no workspace), and
the default/template workspace are all uncapped, so nothing changes for
real deployments.

Defaults are env-overridable per resource: ``WORKSPACE_CAP_QUEUES=25``.
"""

from __future__ import annotations

import os

# resource key -> (default cap, model import path)
_CAPS: dict[str, int] = {
    'queues': 10,
    'collections': 5,
    'documents': 50,
    'contacts': 200,
    'users': 10,
    'callbacks': 50,
}


def _model_for(resource: str):
    # Lazy imports — this module is imported by API modules at boot.
    from app.models import (
        Callback,
        Contact,
        Document,
        DocumentCollection,
        Queue,
        User,
    )
    return {
        'queues': Queue,
        'collections': DocumentCollection,
        'documents': Document,
        'contacts': Contact,
        'users': User,
        'callbacks': Callback,
    }[resource]


def workspace_cap(resource: str) -> int:
    raw = os.getenv(f'WORKSPACE_CAP_{resource.upper()}', '').strip()
    try:
        n = int(raw) if raw else _CAPS[resource]
    except ValueError:
        n = _CAPS[resource]
    return max(1, n)


def cap_denial(resource: str):
    """``(body, 403)`` when the current workspace is at its cap for
    ``resource``, else None. Call at the top of create endpoints.

    Counts with an explicit ``workspace_id`` filter rather than relying
    on the auto-scope, so the answer is right even if a caller wraps the
    endpoint in a ``workspace_context`` override.
    """
    from app.tenancy import DEFAULT_WORKSPACE_ID, current_workspace_id
    from app.utils.demo_config import tenancy_mode_active

    if not tenancy_mode_active():
        return None
    ws = current_workspace_id()
    if not ws or ws == DEFAULT_WORKSPACE_ID:
        return None
    if resource not in _CAPS:
        return None
    cap = workspace_cap(resource)
    model = _model_for(resource)
    count = model.query.filter(model.workspace_id == ws).count()
    if count >= cap:
        return (
            {
                'error': (
                    f'This workspace has reached its limit of {cap} '
                    f'{resource}. Delete some first, or start fresh.'
                ),
                'code': 'workspace_cap',
                'resource': resource,
                'cap': cap,
            },
            403,
        )
    return None
