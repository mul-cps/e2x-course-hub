"""Publish completed Source account reviews to the canonical gateway API."""

import os
import ssl
from urllib.parse import urlsplit

import httpx


async def publish_reviewed_aliases(client, source, linked):
    if source not in ('cps', 'cit'):
        raise ValueError('Exact owning Source required')
    published = []
    for username, review in linked.items():
        if 'review_actor' not in review:
            continue
        body = {field: review[field] for field in
                ('person_id', 'review_actor', 'reviewed_at', 'review_reason')}
        response = await client.post(f'/internal/v1/canonical-links/{source}',
                                     json={'username': username, **body})
        response.raise_for_status()
        if response.json() != {'linked': True}:
            raise ValueError('Gateway did not acknowledge the identity link')
        published.append(username)
    return published


async def publish_source_reviews(source, linked):
    token = os.environ.get('CPS_CANONICAL_LINK_PUBLISHER_TOKEN')
    if not token or not any('review_actor' in review for review in linked.values()):
        return None
    destination = os.environ.get('CPS_WORKSPACE_CONTROL_URL', '')
    url = urlsplit(destination)
    if (url.scheme != 'https' or not url.hostname or url.username is not None
            or url.password is not None or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('Trusted workspace gateway origin required')
    trust = ssl.create_default_context(cafile=os.environ.get('CPS_WORKSPACE_CONTROL_CA'))
    async with httpx.AsyncClient(base_url=destination, verify=trust, timeout=10, trust_env=False,
                                 headers={'Authorization': 'Bearer ' + token}) as client:
        return await publish_reviewed_aliases(client, source, linked)
