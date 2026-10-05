"""Durable request-level mutation audit, including validation and authorization failures."""
import contextvars
import json
from datetime import datetime,timezone

actor_context=contextvars.ContextVar('cps_audit_actor',default=None)

class AuditMixin:
    def prepare(self):
        provider=self.settings.get('course_provider')
        if provider and self.request.method in ('POST','PUT','DELETE','PATCH'):
            self._audit_previous=self._audit_records(provider)
        return super().prepare()

    def _audit_records(self,provider):
        return {(row['kind'],row['id']):json.loads(row['payload']) for row in provider.db.execute(
            'SELECT kind,id,payload FROM records WHERE console=?',(provider.console,))}

    def on_finish(self):
        super().on_finish()
        provider=self.settings.get('course_provider')
        if not provider or self.request.method not in ('POST','PUT','DELETE','PATCH'):return
        user=getattr(self,'_audit_actor',None)
        if user is None:
            try: current=self.current_user
            except Exception:current=None
            user=current.get('name') if isinstance(current,dict) else None
        actor=user or 'unauthenticated'
        before=getattr(self,'_audit_previous',{})
        after=self._audit_records(provider)
        changed={key for key in before.keys()|after.keys() if before.get(key)!=after.get(key)}
        previous=[{'kind':k[0],'id':k[1],'value':before.get(k)} for k in sorted(changed)]
        current=[{'kind':k[0],'id':k[1],'value':after.get(k)} for k in sorted(changed)]
        try:requested=json.loads(self.request.body or b'{}')
        except (ValueError,UnicodeDecodeError):requested={'invalid_json':True,'bytes':len(self.request.body)}
        status=self.get_status()
        outcome='success' if status<400 else 'denied' if status in (401,403) else 'invalid' if status<500 else 'failure'
        if not changed:
            # Failed requests retain the previous targeted record and proposed values.
            identifier=requested.get('id') if isinstance(requested,dict) else None
            previous=[{'kind':k[0],'id':k[1],'value':v} for k,v in before.items() if k[1]==identifier]
            current={'requested':requested,'status':status}
        with provider.db:
            provider.db.execute('INSERT INTO audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)',(
                actor,provider.console,'http:'+self.request.method,self.request.path,
                json.dumps(previous,sort_keys=True),json.dumps(current,sort_keys=True),
                datetime.now(timezone.utc).isoformat(),outcome))
