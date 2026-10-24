"""Server-only v2 Source write fencing. No activation or runtime authorization."""
import hashlib
import json
import uuid


def digest(value):
    return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


class PoolMutationRPCGuard:
    VERSION='source-record-write-v1'

    def __init__(self,client,*,database_id):
        from .compute import ComputePolicyClient
        if type(client) is not ComputePolicyClient or str(uuid.UUID(database_id))!=database_id:
            raise ValueError('Typed server policy client and exact reservation database identity required')
        self.client=client;self.database_id=database_id
        self.binding=(client.console,client.base_url,database_id)

    async def prepare(self,operation,payload):
        # This call must finish before any outer SQLite transaction starts.
        result=await self.client._request('native-pool-head/prepare-write','POST',
                                          {'operation':operation,'payload':payload})
        if (not isinstance(result,dict) or result.get('database_id')!=self.database_id
                or result.get('operation')!=operation
                or result.get('input_sha256')!=digest({'operation':operation,'payload':payload})
                or result.get('receipt') is not None or result.get('mutation_result') is not None):
            raise ValueError('Exact pending remote writer intent required')
        return result

    async def complete(self,ticket,result):
        response=await self.client._request('native-pool-head/complete-write','POST',
            {'transition_id':ticket['transition_id'],'result':result})
        if response!={'state':'pending','transition_id':ticket['transition_id']}:
            raise ValueError('Remote mutation completion is pending/unavailable')

    def deny_uncovered(self,operation):
        raise ValueError('V2 '+operation+' writer not integrated; mutation denied')


SOURCE_SCHEMA={
 'pool_source_authority':'CREATE TABLE pool_source_authority(console TEXT NOT NULL,origin TEXT NOT NULL,database_id TEXT NOT NULL,source_path TEXT NOT NULL)',
 'pool_source_receipt':'CREATE TABLE pool_source_receipt(transition_id TEXT PRIMARY KEY,input_sha256 TEXT NOT NULL,result TEXT NOT NULL)',
}


async def migrate_pool_source_guard(provider,guard):
    """Explicit operator action; never called by a constructor or runtime factory.

    The server denies active-head migration. This is a preparatory migration only;
    v2 activation remains denied until the real operator applicator is installed.
    """
    from .providers import LocalCourseProvider
    if type(provider) is not LocalCourseProvider or type(guard) is not PoolMutationRPCGuard or provider.console!=guard.client.console:
        raise ValueError('Exact owned Source and coordinator required')
    if provider.db.in_transaction:raise ValueError('Source migration cannot call RPC inside SQLite')
    permit=await guard.client._request('native-pool-head/migration-permit')
    if permit.get('database_id')!=guard.database_id or permit.get('state') not in ('absent','fenced'):
        raise ValueError('Authenticated operator disabled/fenced migration permit required')
    from pathlib import Path
    actual=str(Path(next(r[2] for r in provider.db.execute('PRAGMA database_list') if r[1]=='main')).resolve())
    expected=(*guard.binding,actual)
    installed=dict(provider.db.execute("SELECT name,sql FROM sqlite_master WHERE name GLOB 'pool_source_*'"))
    if installed:
        if installed!=SOURCE_SCHEMA or [tuple(r) for r in provider.db.execute('SELECT console,origin,database_id,source_path FROM pool_source_authority')]!=[expected]:
            raise ValueError('Source migration custody differs')
    else:
        with provider.db:
            provider.db.execute('BEGIN IMMEDIATE')
            for sql in SOURCE_SCHEMA.values():provider.db.execute(sql)
            provider.db.execute('INSERT INTO pool_source_authority VALUES(?,?,?,?)',expected)
    provider.pool_guard=guard
