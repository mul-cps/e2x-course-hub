"""Explicit upstream config/Hub membership import with exact existing identifiers."""
async def import_upstream(server, hub_api, provider, *, actor, reviewed_identity_rows=None):
    from .identity import reviewed_email_mapping
    mapping = reviewed_email_mapping(reviewed_identity_rows) if reviewed_identity_rows is not None else None
    records = {kind: [] for kind in ('courses', 'terms', 'memberships', 'groups', 'groupings')}
    for course_id, course in server.courses.items():
        records['courses'].append({'id': course_id, 'name': course.metadata.course_name,
                                   'source': course.metadata.source, 'external_id': course.metadata.external_id})
        for term_id in course.terms:
            records['terms'].append({'id': f'{course_id}.{term_id}', 'course_id': course_id,
                                     'term_id': term_id, 'source': course.config.terms[term_id].source})
            for role in server.roles.root:
                group_id = f'{course_id}.{term_id}.{role}'
                from ..api.errors import GroupNotFoundError
                try:
                    group = await hub_api.get_group(group_id)
                except GroupNotFoundError:
                    continue
                records['groups'].append({'id': group_id, 'course_id': course_id,
                                           'term_id': term_id, 'role': role})
                for username in group.get('users', []):
                    canonical = {}
                    if mapping is not None:
                        if (provider.console,username) not in mapping: raise ValueError('missing reviewed email mapping for existing user: '+username)
                        canonical = {'canonical_person_id':mapping[(provider.console,username)]}
                    records['memberships'].append({**canonical,'id': f'{group_id}:{username}', 'course_id': course_id,
                        'term_id': term_id, 'person_id': username, 'role': role, 'group_id': group_id})
    # Nothing is removed from Hub, YAML or storage. Canonical person aliases require reviewed mapping.
    if reviewed_identity_rows is not None:
        await provider.link_identities(reviewed_identity_rows,actor=actor)
    await provider.migrate_local(records, actor=actor)
    return records
