"""Explicit verified/reviewed email authority; never derive email from usernames."""
import re

def reviewed_email_mapping(rows):
    if not isinstance(rows,list):raise ValueError('explicit mapping rows list required')
    result, seen = {}, set()
    for row in rows:
        if not isinstance(row,dict):raise ValueError('mapping row must be an object')
        hub, username, email = row.get('hub'), row.get('username'), row.get('email')
        if hub not in ('cps','cit') or not isinstance(username,str) or not username:
            raise ValueError('explicit hub and existing username required')
        if not isinstance(email,str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email.strip()):
            raise ValueError('explicit valid email required; usernames are not identity authority')
        if row.get('verified') is not True and row.get('administrator_reviewed') is not True:
            raise ValueError('email mapping must be verified or explicitly administrator reviewed')
        email=email.strip().casefold()
        key=(hub,username)
        if key in result or (hub,email) in seen:
            raise ValueError('duplicate username/email mapping within a Hub requires reviewed alias resolution')
        result[key]=email
        seen.add((hub,email))
    return result
