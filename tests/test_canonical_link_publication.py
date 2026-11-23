import httpx
import pytest

from e2x_course_hub.cps.canonical_link_publication import publish_reviewed_aliases


@pytest.mark.asyncio
async def test_only_committed_alias_reviews_reach_gateway():
    calls = []

    async def receive(request):
        calls.append(request)
        return httpx.Response(200, json={'linked': True})

    linked = {
        'alice': {'person_id': 'uuid', 'review_actor': 'admin',
                  'reviewed_at': '2026-11-23T20:00:00Z', 'review_reason': 'reviewed',
                  'email': 'private@example.edu'},
        'bob': {'person_id': 'other-uuid', 'email': 'private2@example.edu'},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive), base_url='https://gateway') as client:
        assert await publish_reviewed_aliases(client, 'cps', linked) == ['alice']
    assert len(calls) == 1
    assert calls[0].url.path == '/internal/v1/canonical-links/cps'
    import json
    assert json.loads(calls[0].content) == {
        'username': 'alice', 'person_id': 'uuid', 'review_actor': 'admin',
        'reviewed_at': '2026-11-23T20:00:00Z', 'review_reason': 'reviewed',
    }


@pytest.mark.asyncio
async def test_relay_stops_on_failed_publication_and_can_be_replayed():
    statuses = iter([503, 200])

    async def receive(request):
        return httpx.Response(next(statuses), json={'linked': True})

    linked = {'alice': {'person_id': 'uuid', 'review_actor': 'admin',
                        'reviewed_at': '2026-11-23T20:00:00Z', 'review_reason': 'reviewed'}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(receive), base_url='https://gateway') as client:
        with pytest.raises(httpx.HTTPStatusError):
            await publish_reviewed_aliases(client, 'cit', linked)
        assert await publish_reviewed_aliases(client, 'cit', linked) == ['alice']
