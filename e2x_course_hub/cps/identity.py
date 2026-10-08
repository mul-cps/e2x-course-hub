"""Explicit verified email or reviewed account alias authority for canonical UUIDs."""
import re
import uuid
from datetime import datetime, timezone

def reviewed_email_mapping(rows):
    if not isinstance(rows,list):raise ValueError('explicit mapping rows list required')
    result,seen,people={},set(),set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('mapping row must be an object')
        hub,username,email=row.get('hub'),row.get('username'),row.get('email')
        if hub not in ('cps','cit') or not isinstance(username,str) or not username:
            raise ValueError('explicit hub and existing username required')
        if not isinstance(email,str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email.strip()):
            raise ValueError('explicit valid email required; usernames are not identity authority')
        if row.get('verified') is not True:
            raise ValueError('email verification is required before person linkage; administrator review alone is insufficient')
        try:person=str(uuid.UUID(row.get('person_id','')))
        except (ValueError,TypeError,AttributeError):raise ValueError('explicit canonical person_id UUID required')
        email=email.strip().casefold()
        key=(hub,username)
        if key in result or (hub,email) in seen or (hub,person) in people:
            raise ValueError('duplicate username/email mapping within a Hub requires reviewed alias resolution')
        if any(v['email']==email and v['person_id']!=person for v in result.values()):
            raise ValueError('same verified email cannot identify different canonical people')
        result[key]={'email':email,'person_id':person}
        seen.add((hub,email));people.add((hub,person))
    return result


def reviewed_alias_mapping(rows, *, actor):
    """Explicit account review is independent of email assurance or equality."""
    if not isinstance(rows, list) or not isinstance(actor, str) or not actor.strip():
        raise ValueError('explicit alias rows and authenticated review actor required')
    result, people = {}, set()
    allowed = {'hub', 'username', 'person_id', 'authority', 'administrator_reviewed',
               'review_reason', 'email', 'verified'}
    for row in rows:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError('alias review fields must be explicit; actor and time are service-owned')
        hub, username = row.get('hub'), row.get('username')
        if hub not in ('cps', 'cit') or not isinstance(username, str) or not username or username != username.strip():
            raise ValueError('explicit owning Hub and unchanged existing username required')
        if row.get('authority') != 'reviewed_account_alias' or row.get('administrator_reviewed') is not True:
            raise ValueError('explicit administrator-reviewed account alias authority required')
        reason = row.get('review_reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('administrator review reason required')
        if 'verified' in row and row['verified'] is not False:
            raise ValueError('alias email is optional unverified metadata; use verified_email for email authority')
        try:
            person = str(uuid.UUID(row.get('person_id', '')))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('explicit canonical person_id UUID required')
        email = row.get('email')
        if email is not None:
            if not isinstance(email, str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email.strip()):
                raise ValueError('optional email metadata must be valid')
            email = email.strip().casefold()
        key = (hub, username)
        if key in result or (hub, person) in people:
            raise ValueError('duplicate account alias or canonical person within a Hub')
        result[key] = dict(person_id=person, authority='reviewed_account_alias', email=email,
                           review_actor=actor, review_reason=reason.strip(),
                           reviewed_at=datetime.now(timezone.utc).isoformat())
        people.add((hub, person))
    return result


def resolve_membership_person(provider, record):
    """One authority check for membership writes and workspace member admission."""
    username = record['person_id']
    email = provider.db.execute('SELECT email,canonical_person_id,verified FROM email_links WHERE console=? AND username=?',
                                (provider.console, username)).fetchone()
    alias = provider.db.execute('SELECT canonical_person_id,review_actor,review_reason,reviewed_at FROM reviewed_account_aliases WHERE console=? AND username=?',
                                (provider.console, username)).fetchone()
    proofs = []
    if email and email['verified'] == 1:
        if not isinstance(email['email'], str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email['email']):
            raise ValueError('invalid verified email authority')
        proofs.append(email['canonical_person_id'])
    if alias:
        if not alias['review_actor'] or not alias['review_actor'].strip() or not alias['review_reason'] or not alias['review_reason'].strip():
            raise ValueError('account alias requires administrator review provenance')
        try:
            instant = datetime.fromisoformat(alias['reviewed_at'])
            if instant.tzinfo is None or instant.utcoffset().total_seconds() != 0:
                raise ValueError('UTC administrator review time required')
        except (ValueError, TypeError, AttributeError):
            raise ValueError('UTC administrator review time required')
        proofs.append(alias['canonical_person_id'])
    if not proofs:
        raise ValueError('email verification or explicit reviewed account alias authority required for member linkage')
    try:
        people = {str(uuid.UUID(person)) for person in proofs}
        expected = str(uuid.UUID(record.get('canonical_person_id', '')))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('explicit canonical person UUID required')
    # Even an old unverified UUID cannot silently be reassigned by a new review.
    if email and email['canonical_person_id'] is not None:
        try:
            people.add(str(uuid.UUID(email['canonical_person_id'])))
        except (ValueError, TypeError, AttributeError):
            if email['verified'] == 1:
                raise ValueError('invalid verified email canonical UUID')
    if people != {expected}:
        raise ValueError('conflicting canonical account identity authority')
    return expected
