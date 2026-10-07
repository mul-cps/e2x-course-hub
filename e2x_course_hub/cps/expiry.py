"""Time bounds on explicitly matched local teaching memberships."""
from datetime import datetime,timezone

def instant(value):
    if value is None:return None
    if not isinstance(value,str):raise ValueError('permission time bound must be an ISO timestamp')
    try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError('permission time bound must be an ISO timestamp')
    if parsed.tzinfo is None:raise ValueError('permission time bound must include timezone')
    return parsed

def active(record,now=None):
    now=now or datetime.now(timezone.utc)
    start,end=instant(record.get('starts')),instant(record.get('expires'))
    return (start is None or start<=now) and (end is None or now<end)

def effective_groups(provider,username,groups,legacy_roles=None):
    import json
    from .authorization import canonical_group
    records=[json.loads(r['payload']) for r in provider.db.execute(
        "SELECT payload FROM records WHERE console=? AND kind='memberships'",(provider.console,))]
    result=[]
    for group in groups:
        canonical=canonical_group(group,legacy_roles)
        grants=[r for r in records if r['person_id']==username and r.get('group_id') is not None
                and canonical_group(r['group_id'],legacy_roles)==canonical]
        if not grants or any(active(r) for r in grants):result.append(group)
    return result
