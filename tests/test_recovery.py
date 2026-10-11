"""Recovery contract tests use real SQLite and loopback HTTP, never cluster APIs."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from e2x_course_hub.cps.providers import LocalCourseProvider
from e2x_course_hub.cps import recovery


def sha(raw): return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    path.chmod(0o600)
    return str(path)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home()/'.local/state'); self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.capture_id = '11111111-1111-4111-8111-111111111111'
        self.hub = self.root/'hub'; self.hub.mkdir(mode=0o700)
        files = {}
        for source, namespace in [('cps','jupyterhub'),('cit','cit-jhub')]:
            for suffix, raw in [('.pgdump',b'PGDMPprivate'),('-roles.sql',b'PRIVATE_ROLE_PASSWORD'),
                                ('-workloads.json',b'{}'),('-configmaps.json',b'{}'),
                                ('-secrets.json',b'PRIVATE_SECRET_VALUE'),('-running-images.txt',('hub-private hub=registry/hub@sha256:'+'8'*64+'\n').encode())]:
                name=namespace+suffix; write(self.hub/name,raw); files[name]={'sha256':sha(raw),'bytes':len(raw)}
        write(self.hub/'manifest.json',{'created_at':'2026-10-11T12:00:05Z','files':files,'limitations':['Sequential capture']})
        self.inputs={'version':1,'capture_id':self.capture_id,'hub_bundle':str(self.hub),'sources':{}}
        for source, namespace in [('cps','jupyterhub'),('cit','cit-jhub')]:
            directory=self.root/source; directory.mkdir(mode=0o700)
            provider=LocalCourseProvider(directory/'live.sqlite',source)
            # The source provider owns the schema; capture takes its backup, not a live file copy.
            provider.db.execute("INSERT INTO records VALUES (?,?,?,?)",(source,'courses','course',json.dumps({'id':'course','source':'local'})))
            provider.db.commit(); provider.backup(directory/'console.sqlite'); provider.db.close()
            (directory/'console.sqlite').chmod(0o600)
            raw=(directory/'console.sqlite').read_bytes()
            write(directory/'manifest.json',{'owner':source,'application_image':'registry/admin@sha256:'+'a'*64,
                'policy_hash':'sha256:'+'b'*64,'user_version':6,'created_at':'2026-10-11T12:00:06Z','sha256':{'console.sqlite':sha(raw)}})
            fingerprints={'schema':'4621fec11365:'+'1'*64,'tables':{'users':'c'*64,'alembic_version':'0'*64},'roles':'d'*64,'role_members':'e'*64,'acls':'f'*64}
            write(directory/'postgres.json',{'namespace':namespace,'captured_at':'2026-10-11T12:00:07Z','fingerprints':fingerprints})
            storage={'pvc_uid':source+'-original-pvc','pv_uid':source+'-original-pv','nfs_server':'nas',
                     'nfs_path':'/original/'+source,'mount_path':'/home/jovyan/project','sentinel_path':'project/sentinel',
                     'sentinel_sha256':sha(b'preserved files\n')}
            self.inputs['sources'][source]={'console_bundle':str(directory),
                'config':write(directory/'config.py',b'PRIVATE_CONFIG_TOKEN'),
                'server_config':write(directory/'config.yml',b'profiles: {}'),
                'hub_fingerprint':str(directory/'postgres.json'),'storage':storage,
                'actor':source+'-admin','owner':source+'-neutral','server':'rtc','course_id':'course',
                'notebook_image':'registry/notebook@sha256:'+'9'*64}
        self.bundle=self.root/'bundle'

    def assemble(self):
        return recovery.capture(self.inputs,self.bundle,lambda _: {'jupyterhub':{'image':'registry/postgres@sha256:'+'1'*64},'cit-jhub':{'image':'registry/postgres@sha256:'+'2'*64}})

    def target(self, source):
        spec=self.inputs['sources'][source]; namespace='restore-'+source+'-11111111'
        directory=self.root/('restored-'+source); directory.mkdir(mode=0o700)
        for name in ('console.sqlite','config.py','config.yml','postgres.json'):
            write(directory/name,(self.root/source/name).read_bytes())
        pod={'apiVersion':'v1','kind':'Pod','metadata':{'namespace':namespace,'name':'jupyter-'+spec['owner']+'--rtc','uid':source+'-pod'},
             'spec':{'containers':[{'name':'notebook','image':spec['notebook_image'],
                 'volumeMounts':[{'name':'project','mountPath':'/home/jovyan/project','readOnly':False}], 'resources':{}}],
                 'volumes':[{'name':'project','persistentVolumeClaim':{'claimName':'project'}}]},
             'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}],
                 'containerStatuses':[{'name':'notebook','ready':True,'imageID':spec['notebook_image']}]}}
        pvc={'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'namespace':namespace,'name':'project','uid':source+'-clone-pvc'},'spec':{'volumeName':source+'-clone-pv'}}
        pv={'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':source+'-clone-pv','uid':source+'-clone-pv'},
            'spec':{'claimRef':{'namespace':namespace,'name':'project','uid':source+'-clone-pvc'},'nfs':{'server':'nas','path':'/recovery/'+source}}}
        def app_pod(name,container,image):
            return {'apiVersion':'v1','kind':'Pod','metadata':{'namespace':namespace,'name':name+'-restored','uid':source+'-'+name},
                'spec':{'containers':[{'name':container,'image':image}]},
                'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}],
                    'containerStatuses':[{'name':container,'ready':True,'imageID':image}]}}
        return {'namespace':namespace,'origin':'http://127.0.0.1:1','cookie_jar':write(directory/'cookies',b'# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tPRIVATE_COOKIE\n'),
                'console_database':str(directory/'console.sqlite'),'config':str(directory/'config.py'),
                'server_config':str(directory/'config.yml'),'hub_fingerprint':str(directory/'postgres.json'),
                'pod':write(directory/'pod.json',pod),'pvc':write(directory/'pvc.json',pvc),'pv':write(directory/'pv.json',pv),
                'hub_pod':write(directory/'hub-pod.json',app_pod('hub','hub','registry/hub@sha256:'+'8'*64)),
                'console_pod':write(directory/'console-pod.json',app_pod(source+'-admin','console','registry/admin@sha256:'+'a'*64))}

    def server(self, source, target, *, sentinel='preserved files\n', actor=None, status=200, redirect=None):
        spec=self.inputs['sources'][source]; seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path,self.headers.get('Cookie'),self.headers.get('Authorization')))
                values={'/hub/api/user':{'name':actor or spec['actor'],'admin':True},
                    '/services/'+source+'-admin/api/local/courses':{'records':[{'id':'course','source':'local'}]},
                    '/hub/api/users/'+spec['owner']:{'name':spec['owner'],'servers':{'rtc':{'ready':True,'pending':None,'url':'/user/'+spec['owner']+'/rtc/'}}},
                    '/user/'+spec['owner']+'/rtc/api/contents/project/sentinel':{'type':'file','format':'text','content':sentinel}}
                body=json.dumps(values.get(self.path,{'secret':'PRIVATE_HTTP_SECRET'})).encode()
                self.send_response(status)
                if redirect:self.send_header('Location',redirect)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        target['origin']='http://127.0.0.1:'+str(server.server_port)
        return seen

    def test_byte_preserving_capture_does_not_claim_coordinated_or_connected_recovery(self):
        report=self.assemble()
        self.assertFalse(report['coordinated_capture']);self.assertFalse(report['connected_restore_passed'])
        self.assertEqual((self.bundle/'jupyterhub-secrets.json').read_bytes(),b'PRIVATE_SECRET_VALUE')
        self.assertEqual((self.bundle/'cps-console.sqlite').read_bytes(),(self.root/'cps/console.sqlite').read_bytes())
        self.assertNotIn('PRIVATE_',json.dumps(report))
        self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in self.bundle.iterdir()))

    def test_foreign_console_backup_and_corrupt_bytes_publish_nothing(self):
        for case in ('owner','checksum'):
            with self.subTest(case=case):
                p=self.root/'cps/manifest.json';saved=p.read_bytes();value=json.loads(saved)
                if case=='owner':value['owner']='cit'
                else:value['sha256']['console.sqlite']='0'*64
                write(p,value)
                with self.assertRaises(ValueError):self.assemble()
                self.assertFalse(self.bundle.exists());write(p,saved)

    def test_public_or_symlink_configuration_is_rejected(self):
        p=Path(self.inputs['sources']['cps']['config']);p.chmod(0o644)
        with self.assertRaises(ValueError):self.assemble()
        p.chmod(0o600);original=p.read_bytes();p.unlink();p.symlink_to(self.root/'cps/config.yml')
        with self.assertRaises(ValueError):self.assemble()
        self.assertFalse(self.bundle.exists())

    def test_both_restored_apps_are_checked_through_real_authenticated_gets(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')};seen={s:self.server(s,targets[s])for s in targets}
        result=recovery.verify(self.bundle,targets)
        self.assertTrue(result['connected_restore_passed']);self.assertFalse(result['productionQualified'])
        for source in seen:
            self.assertEqual(len(seen[source]),4)
            self.assertTrue(all(cookie=='session=PRIVATE_COOKIE' and bearer is None for _,cookie,bearer in seen[source]))
        self.assertNotIn('PRIVATE_',json.dumps(result))

    def test_orm_open_alone_and_changed_postgres_roles_do_not_pass(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        p=Path(targets['cps']['hub_fingerprint']);value=json.loads(p.read_bytes());value['fingerprints']['roles']='0'*64;write(p,value)
        with self.assertRaisesRegex(ValueError,'roles, memberships or ACLs differ'):recovery.verify(self.bundle,targets)

    def test_restored_console_rows_are_compared_semantically_before_network(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        import sqlite3
        with sqlite3.connect(targets['cps']['console_database'])as db:db.execute("UPDATE records SET payload=?",(json.dumps({'id':'course','source':'local','name':'changed'}),))
        with self.assertRaisesRegex(ValueError,'schema or rows differ'):recovery.verify(self.bundle,targets)

    def test_production_origins_namespaces_and_original_storage_are_denied(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        for field,value in [('origin','https://hub.example.edu'),('namespace','jupyterhub')]:
            changed=copy.deepcopy(targets);changed['cps'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'loopback|namespace'):recovery.verify(self.bundle,changed)
        p=Path(targets['cps']['pvc']);value=json.loads(p.read_bytes());value['metadata']['uid']='cps-original-pvc';write(p,value)
        with self.assertRaisesRegex(ValueError,'isolated PVC/PV identities'):recovery.verify(self.bundle,targets)

    def test_wrong_login_and_missing_retained_sentinel_fail_connected_acceptance(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        self.server('cps',targets['cps'],sentinel='changed');self.server('cit',targets['cit'])
        with self.assertRaisesRegex(ValueError,'sentinel differs'):recovery.verify(self.bundle,targets)

    def test_http_denial_never_exposes_remote_credentials(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        self.server('cps',targets['cps'],status=403);self.server('cit',targets['cit'])
        with self.assertRaises(ValueError)as error:recovery.verify(self.bundle,targets)
        self.assertNotIn('PRIVATE_',str(error.exception))

    def test_gpu_pod_is_denied_before_any_http_call(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        p=Path(targets['cps']['pod']);value=json.loads(p.read_bytes());value['spec']['containers'][0]['resources']={'limits':{'nvidia.com/gpu':1}};write(p,value)
        with self.assertRaisesRegex(ValueError,'GPU resources'):recovery.verify(self.bundle,targets)

    def test_postgres_capture_sql_is_read_only_and_excludes_only_exact_bootstrap(self):
        sql=recovery.postgres_fingerprint_sql('cps')
        self.assertIn('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;',sql)
        self.assertIn('query_to_xml',sql);self.assertIn('pg_authid',sql);self.assertIn('pg_auth_members',sql)
        self.assertIn('aclexplode',sql);self.assertIn("'jupyterhub'",sql)
        self.assertNotIn('CREATE ',sql);self.assertNotIn('ALTER ',sql);self.assertNotIn('rolname LIKE',sql)
        self.assertIn("restore_0123456789abcdef",recovery.postgres_fingerprint_sql('cit','restore_0123456789abcdef'))
        for value in ('postgres','restore_%',"x'; SELECT 1; --"):
            with self.subTest(value=value),self.assertRaises(ValueError):recovery.postgres_fingerprint_sql('cit',value)

    def test_existing_logical_restore_callback_captures_without_claiming_source_equality(self):
        probe=recovery.LogicalRestoreProbe(self.root/'logical')
        calls=[]
        for source,namespace in [('cps','jupyterhub'),('cit','cit-jhub')]:
            def execute(query,source=source):
                calls.append(query)
                return (self.root/source/'postgres.json').read_bytes()
            result=probe(namespace,'restore_0123456789abcdef',execute)
            self.assertFalse(result['source_comparison_passed'])
        report=probe.finish()
        self.assertEqual(len(calls),2);self.assertTrue(all(b'READ ONLY' in q for q in calls))
        self.assertFalse(report['productionQualified']);self.assertFalse(report['connected_restore_passed'])
        self.assertFalse(report['source_comparison_passed']);self.assertTrue(report['logical_fingerprints_captured'])
        self.assertTrue((self.root/'logical/report.json').exists())

    def test_logical_restore_callback_compares_exact_source_roles_and_refuses_partial_receipt(self):
        expected={s:str(self.root/s/'postgres.json') for s in ('cps','cit')}
        probe=recovery.LogicalRestoreProbe(self.root/'logical',expected)
        raw=(self.root/'cps/postgres.json').read_bytes();value=json.loads(raw)
        value['fingerprints']['acls']='0'*64
        with self.assertRaisesRegex(ValueError,'source'):
            probe('jupyterhub','restore_0123456789abcdef',lambda _:json.dumps(value).encode())
        self.assertFalse((self.root/'logical/report.json').exists())
        with self.assertRaises(ValueError):probe.finish()

    def test_logical_restore_callback_denies_foreign_or_duplicate_scopes_before_execution(self):
        probe=recovery.LogicalRestoreProbe(self.root/'logical')
        def forbidden(_):self.fail('Foreign scope must fail before SQL')
        with self.assertRaises(ValueError):probe('foreign','restore_0123456789abcdef',forbidden)
        with self.assertRaises(ValueError):probe('jupyterhub','postgres',forbidden)
        probe('jupyterhub','restore_0123456789abcdef',lambda _:(self.root/'cps/postgres.json').read_bytes())
        with self.assertRaises(ValueError):probe('jupyterhub','restore_0123456789abcdef',forbidden)

    def test_wrong_source_login_is_denied(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        self.server('cps',targets['cps'],actor='cit-admin');self.server('cit',targets['cit'])
        with self.assertRaises(ValueError):recovery.verify(self.bundle,targets)

    def test_http_redirect_cannot_send_login_cookies_to_the_other_restore(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        cit=self.server('cit',targets['cit'])
        cps=self.server('cps',targets['cps'],status=302,redirect=targets['cit']['origin']+'/hub/api/user')
        with self.assertRaisesRegex(ValueError,'GET failed'):recovery.verify(self.bundle,targets)
        self.assertEqual(len(cps),1);self.assertEqual(cit,[])

    def test_foreign_notebook_pod_and_original_nfs_directory_are_denied(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        p=Path(targets['cps']['pod']);saved=p.read_bytes();value=json.loads(saved)
        value['metadata']['name']='jupyter-cit-neutral--rtc';write(p,value)
        with self.assertRaisesRegex(ValueError,'owner notebook identity'):recovery.verify(self.bundle,targets)
        write(p,saved);p=Path(targets['cps']['pv']);value=json.loads(p.read_bytes())
        value['spec']['nfs']['path']=self.inputs['sources']['cps']['storage']['nfs_path'];write(p,value)
        with self.assertRaisesRegex(ValueError,'separate NFS'):recovery.verify(self.bundle,targets)

    def test_nfs_aliases_and_extra_source_volumes_are_denied(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        p=Path(targets['cps']['pv']);saved=p.read_bytes()
        for alias in ('/original/cps/.','/elsewhere/../original/cps','/original//cps','//original/cps'):
            value=json.loads(saved);value['spec']['nfs']['path']=alias;write(p,value)
            with self.subTest(alias=alias),self.assertRaisesRegex(ValueError,'[Cc]anonical'):
                recovery.verify(self.bundle,targets)
        write(p,saved);p=Path(targets['cps']['pod']);saved=p.read_bytes()
        for volume in ({'name':'extra','nfs':{'server':'nas','path':'/original/cps'}},
                       {'name':'extra','persistentVolumeClaim':{'claimName':'original-source'}}):
            value=json.loads(saved);value['spec']['volumes'].append(volume);write(p,value)
            with self.subTest(volume=volume),self.assertRaisesRegex(ValueError,'volume'):
                recovery.verify(self.bundle,targets)

    def test_restored_hub_and_console_actual_images_must_match_capture(self):
        self.assemble();targets={s:self.target(s)for s in ('cps','cit')}
        for field in ('hub_pod','console_pod'):
            p=Path(targets['cps'][field]);saved=p.read_bytes();value=json.loads(saved)
            value['status']['containerStatuses'][0]['imageID']='registry/stale@sha256:'+'0'*64;write(p,value)
            with self.subTest(component=field),self.assertRaisesRegex(ValueError,'application image'):
                recovery.verify(self.bundle,targets)
            write(p,saved)

    def writer_window(self):
        writers={}
        for key,namespace,name in [('cps_hub','jupyterhub','hub'),('cit_hub','cit-jhub','hub'),
                                   ('cps_console','cps-compute','cps-admin'),('cit_console','cps-compute','cit-admin')]:
            writers[key]={'deployment':{'apiVersion':'apps/v1','kind':'Deployment',
                'metadata':{'namespace':namespace,'name':name,'uid':key+'-uid','generation':1},
                'spec':{'replicas':0,'selector':{'matchLabels':{'app':name}}},
                'status':{'replicas':0,'readyReplicas':0,'observedGeneration':1}},
                'pods':{'apiVersion':'v1','kind':'List','items':[]}}
        before={'captured_at':'2026-10-11T12:00:00Z','writers':writers}
        after=copy.deepcopy(before);after['captured_at']='2026-10-11T12:00:10Z'
        self.inputs['writer_window']={'before':write(self.root/'before.json',before),'after':write(self.root/'after.json',after)}
        return before,after

    def test_real_zero_writer_objects_bracket_all_snapshots_and_cannot_be_replaced_by_flags(self):
        before,after=self.writer_window();report=self.assemble()
        self.assertTrue(report['coordinated_capture']);self.assertFalse(report['connected_restore_passed'])
        # The next subcases each use a new destination, never overwrite evidence.
        for case in ('replicas','pod','uid','timestamp','foreign_namespace'):
            changed=copy.deepcopy(after)
            if case=='replicas':changed['writers']['cps_console']['deployment']['spec']['replicas']=1
            if case=='pod':changed['writers']['cps_console']['pods']['items']=[{'metadata':{'namespace':'cps-compute','labels':{'app':'cps-admin'}}}]
            if case=='uid':changed['writers']['cit_hub']['deployment']['metadata']['uid']='replacement'
            if case=='timestamp':changed['captured_at']='2026-10-11T12:00:04Z'
            if case=='foreign_namespace':changed['writers']['cit_console']['deployment']['metadata']['namespace']='jupyterhub'
            write(self.root/'after.json',changed)
            with self.subTest(case=case),self.assertRaises(ValueError):
                recovery.capture(self.inputs,self.root/('reject-'+case),lambda _: {'jupyterhub':{'image':'registry/postgres@sha256:'+'1'*64},'cit-jhub':{'image':'registry/postgres@sha256:'+'2'*64}})
        self.inputs['writer_window']={'passed':True}
        with self.assertRaises(ValueError):recovery.capture(self.inputs,self.root/'reject-flag',lambda _: {})


if __name__=='__main__':unittest.main()
