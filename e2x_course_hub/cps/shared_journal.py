"""Generation7 closed shared phases over the original Source Hub journal tables."""
from datetime import datetime,timezone
import json
from .hub_journal import _require,_json
from cps_compute.native_pool_shared_effects import SHARED_ACTIONS,PHASES,source_postimage,validate_stopped


def validate_record(journal,record,state,prepared):
    lifecycle=record['lifecycle']
    _require(type(lifecycle) is dict and set(lifecycle)=={'version','stopped','control','finalized','stops'} and type(lifecycle['version']) is int and lifecycle['version']==3)
    _require(all(record[k]==v for k,v in prepared.items() if k not in ('effect','effect_sha256','receipt','audit_rowid','audit_time','lifecycle')))
    stops=lifecycle['stops'];writers=record['intent']['request']['writers']
    _require(type(stops) is list and len(stops)<=len(writers))
    for event,writer in zip(stops,writers):
        _require(type(event) is dict and set(event)=={'actor','writer','started_at','completed_at','outcome','after'}
            and event['actor']==record['actor'] and event['writer']==writer
            and type(event['started_at']) is str and bool(event['started_at']))
        if event['outcome']=='unknown':_require(event['completed_at'] is None and event['after'] is None)
        else:
            _require(event['outcome']=='hub-stopped' and type(event['completed_at']) is str
                and event['completed_at']>=event['started_at'] and event['after']==dict(name=writer['owner'],server=dict(
                    name=writer['descriptor']['server'],full_name=writer['owner']+'/'+writer['descriptor']['server'],
                    stopped=True,ready=False,pending=None,started=None)))
    if state not in ('prepared','armed'):
        _require(len(stops)==len(writers) and all(e['outcome']=='hub-stopped' for e in stops))
    if state=='prepared':_require(record==prepared);return
    journal._effect(record,record['effect'],record['effect_sha256'])
    if state=='armed':_require(lifecycle['stopped'] is None)
    else:validate_stopped(record['intent']['request'],lifecycle['stopped'])
    if state in ('armed','stopped'):
        _require(all(record[k] is None for k in ('receipt','audit_rowid','audit_time')))
    else:
        journal._receipt(record,record['receipt'])
        if state in ('hub-completed','control-completed'):_require(record['audit_rowid'] is None and record['audit_time'] is None)
        else:
            _require(type(record['audit_rowid']) is int and record['audit_rowid']>0 and type(record['audit_time']) is str)
            receipt=journal._db.execute('SELECT input_sha256,result FROM pool_source_receipt WHERE transition_id=?',(record['operation_id'],)).fetchone()
            audit=journal._db.execute('SELECT * FROM audit WHERE rowid=?',(record['audit_rowid'],)).fetchone()
            _require(receipt is not None and tuple(receipt)==(record['input_sha256'],_json(record['receipt']))
                and audit is not None and tuple(audit)==journal._audit(record))
    if PHASES.index(state)<PHASES.index('control-completed'):_require(lifecycle['control'] is None)
    else:
        from cps_compute.native_pool_shared_effects import control_result
        _require(lifecycle['control']==control_result(record['intent'],record['receipt']))
    if state!='effective':_require(lifecycle['finalized'] is None)
    else:
        value=lifecycle['finalized']
        _require(type(value) is dict and value.get('transition_id')==record['operation_id'])
        # Actual coordinator finalized receipt is rechecked by installed history;
        # this local decoder cannot convert arbitrary copied JSON into authority.


def advance(journal,operation_id,input_sha256,phase,*,stopped=None,receipt=None,control=None,finalized=None):
    _require(phase in ('stopped','hub-completed','control-completed','source-completed','effective'))
    with journal._transaction(write=True):
        journal._validate();record=journal._find(operation_id,input_sha256)
        _require(record is not None and record['intent']['request']['action'] in SHARED_ACTIONS)
        previous=record.pop('state');rank=PHASES.index(phase);old=PHASES.index(previous)
        _require(rank<=old+1 and old>=1)
        if stopped is not None:
            validate_stopped(record['intent']['request'],stopped)
            _require(record['lifecycle']['stopped'] in (None,stopped))
        if receipt is not None:journal._receipt(record,receipt);_require(record['receipt'] in (None,receipt))
        if rank<=old:
            _require(stopped is None or record['lifecycle']['stopped']==stopped)
            _require(receipt is None or record['receipt']==receipt)
            _require(finalized is None or record['lifecycle']['finalized']==finalized)
            return dict(record,state=previous)
        if phase=='stopped':
            _require(stopped is not None and record['source_preimage']==journal._snapshot())
            record['lifecycle']['stopped']=stopped
        elif phase=='hub-completed':
            _require(receipt is not None and record['source_preimage']==journal._snapshot());record['receipt']=receipt
        elif phase=='control-completed':
            from cps_compute.native_pool_shared_effects import control_result
            _require(control==control_result(record['intent'],record['receipt']))
            record['lifecycle']['control']=control
        elif phase=='source-completed':
            _require(record['source_preimage']==journal._snapshot())
            post=source_postimage(record['source_preimage'],record['intent']['request'])
            for change in record['intent']['request']['source_changes']:
                key=(journal._binding['console'],change['kind'],change['id'],change['before'])
                if change['after'] is None:
                    count=journal._db.execute('DELETE FROM records WHERE console=? AND kind=? AND id=? AND payload=?',key).rowcount
                else:
                    count=journal._db.execute('UPDATE records SET payload=? WHERE console=? AND kind=? AND id=? AND payload=?',(change['after'],*key)).rowcount
                _require(count==1)
            _require(journal._snapshot()==post)
            record['audit_time']=datetime.now(timezone.utc).isoformat()
            journal._db.execute('INSERT INTO pool_source_receipt VALUES(?,?,?)',(operation_id,input_sha256,_json(record['receipt'])))
            record['audit_rowid']=journal._db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)',journal._audit(record)).lastrowid
        else:
            installation=getattr(journal.provider,'managed_installation',None)
            _require(installation is not None)
            with installation.owner.heads._db() as db:
                row=installation.owner.heads._row(db,operation_id)
                _require(row['receipt'] is not None and json.loads(row['receipt'])==finalized and row['input_sha256']==input_sha256)
            _require(installation.owner.heads.status()['state']=='active')
            record['lifecycle']['finalized']=finalized
        return journal._save(record,phase)


def upgrade_shared_journal(journal,operation_id,input_sha256,*,intent):
    """Explicit installation maintenance only; never called by a request/constructor."""
    from cps_compute.native_pool_hub_effects import validate_hub_intent
    from .hub_journal import _hash
    intent=validate_hub_intent(intent)
    _require(intent['request']['action'] in SHARED_ACTIONS and _hash(dict(operation='admin.hub',payload=intent))==input_sha256)
    installation=getattr(journal.provider,'managed_installation',None)
    _require(installation is not None and not journal._db.in_transaction)
    installation.check_source(journal.provider)
    from cps_compute.native_pool_activation import _snapshot
    db=journal._db;before=_snapshot(db)
    _require(before['__user_version__']==6)
    status=installation.owner.heads.status();row=status['transition']
    _require(status['state']=='fenced' and status['transition_id']==operation_id and row['operation']=='admin.hub'
        and row['input_sha256']==input_sha256 and row['effect_sha256'] is None and row['mutation_result'] is None
        and row['target'] is None and row['receipt'] is None)
    journal._validate()
    db.execute('BEGIN IMMEDIATE')
    try:
        _require(_snapshot(db)==before)
        db.execute('PRAGMA user_version=7');after=_snapshot(db)
        ticket=installation.stage_shared_migration(journal,before,after,operation_id,input_sha256,intent)
        db.commit();installation.finish(ticket)
    except Exception:
        db.rollback();raise


def stop_event(journal,operation_id,input_sha256,writer,*,after=None):
    """Retain one exact armed writer stop intent/outcome; no independent SQL audit."""
    with journal._transaction(write=True):
        journal._validate();record=journal._find(operation_id,input_sha256)
        _require(record is not None and record.pop('state')=='armed')
        writers=record['intent']['request']['writers'];stops=record['lifecycle']['stops']
        _require(writer in writers);index=writers.index(writer)
        if index==len(stops):
            _require(after is None)
            stops.append(dict(actor=record['actor'],writer=writer,started_at=datetime.now(timezone.utc).isoformat(),
                completed_at=None,outcome='unknown',after=None))
        else:
            _require(index<len(stops) and stops[index]['writer']==writer)
            if after is not None and stops[index]['outcome']=='unknown':
                stops[index].update(completed_at=datetime.now(timezone.utc).isoformat(),outcome='hub-stopped',after=after)
        return journal._save(record,'armed')
