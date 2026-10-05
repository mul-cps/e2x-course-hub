"""Local grouping without external providers or hidden name matching."""
import csv
import io
import random

def allocate(members, *, mode, rows=None, group_size=None, seed=None):
    members = sorted(set(members))
    if mode == 'random':
        if not isinstance(group_size, int) or group_size < 1 or seed is None:
            raise ValueError('random allocation requires positive group_size and explicit seed')
        random.Random(str(seed)).shuffle(members)
        return {f'group-{1+i//group_size}': members[i:i+group_size] for i in range(0,len(members),group_size)}
    if mode == 'csv':
        reader = csv.DictReader(io.StringIO(rows or ''))
        if reader.fieldnames != ['person_id','group_id']:
            raise ValueError('CSV header must be person_id,group_id')
        pairs = [(r['person_id'],r['group_id']) for r in reader]
    elif mode == 'manual':
        if not isinstance(rows, dict): raise ValueError('manual allocation requires group -> member list')
        pairs = [(person,group) for group,people in rows.items() for person in people]
    else: raise ValueError('allocation mode must be csv, manual or random')
    result, seen = {}, set()
    for person,group in pairs:
        if person not in members or person in seen or not group:
            raise ValueError('unknown, duplicate member or empty group')
        seen.add(person); result.setdefault(group,[]).append(person)
    return {group:sorted(people) for group,people in sorted(result.items())}
