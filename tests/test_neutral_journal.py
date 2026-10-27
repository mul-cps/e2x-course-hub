"""Mixed journal versions retain original SQL snapshots and permanent evidence."""
import copy
import hashlib
import uuid
import test_hub_journal as original
from cps_compute.native_groups import canonical_sha256 as digest
from cps_compute.native_pool_neutral_effects import control_scopes,runtime_scopes


def neutral_evidence(source):
    body=dict(workspace='operator-qualification-a',course='course',term='ws26',group='operators',server='rtc',namespace='jupyterhub',pod='jupyter-operator')
    owner='cps-ws-'+hashlib.sha256(b'cps\0operator-qualification-a').hexdigest()[:32]
    authority=dict(service=dict(id=1,name='cps-admin'),kernel_group=dict(id=2,name='cps-workspace-kernel-accounts'),kernel_role=dict(id=3,name='cps-workspace-kernel'))
    before=dict(owner=None,course_group=None,control_role=None,runtime_role=None,registry=None,custody=None,authority=authority)
    intent=dict(console='cps',actor='course-admin',hub_api=original.HUB,
        request=dict(action='workspace-account-create',descriptor=body,binding=dict(source='local',owner=owner,canonical_members=[original.DB])),
        before=before,source_revision=digest(source))
    inc='11111111-1111-4111-8111-111111111111'
    receipt=dict(version=1,kind='hub_neutral_creation',source='cps',workspace=body['workspace'],owner=owner,
        descriptor_hash=digest(dict(source='cps',**body)).removeprefix('sha256:'),group=authority['kernel_group']['name'],role=authority['kernel_role']['name'],
        identity=dict(id=4,name=owner,created='2026-10-27T00:00:00Z'))
    controls=dict(owner=owner,hub_service_id=1,hub_role_id=5,incarnation=inc,hub_runtime_role_id=6,runtime_incarnation=inc)
    group=dict(id=7,name='operators',users=[],roles=[],properties={'cps_workspace_incarnation':inc},
        custody=dict(name='operators',source='cps',course='course',term='ws26',hub_group_id=7,incarnation=inc))
    after=dict(receipt=receipt,authority=authority,course_group=group,controls=controls,
        control_role=dict(id=5,name='cps-workspace-control-'+owner,scopes=control_scopes(owner,body),description='Permanent Source custody '+inc,users=[],groups=[],services=[1],managed_by_auth=False),
        runtime_role=dict(id=6,name='cps-workspace-runtime-'+owner,scopes=runtime_scopes(owner,body),description='Permanent kernel runtime '+inc,users=[4],groups=[],services=[],managed_by_auth=False),
        owner=dict(id=4,name=owner,admin=False,roles=[3,6],groups=[2]))
    return intent,after


def test_reopened_mixed_v1_v2_journal_retains_exact_originals():
    case=original.HubJournalTests();case.setUp()
    try:
        journal=case.provision();journal.prepare(**case.args);case.arm();case.complete()
        old=journal.read(original.OP,case.input)
        source=journal.capture_source();intent,after=neutral_evidence(source)
        op=str(uuid.uuid4());ih=digest(dict(operation='admin.hub',payload=intent))
        ticket=dict(case.ticket,transition_id=op,input_sha256=ih)
        record=journal.prepare(operation_id=op,input_sha256=ih,intent=intent,source_preimage=source,
            hub_preimage=intent['before'],actor='course-admin',ticket=ticket)
        effect=dict(case.effect,protocol='cps-native-hub-effect/v2',version=2,transition_id=op,input_sha256=ih,intent=intent)
        journal.arm(op,ih,effect=effect,effect_sha256=digest(effect))
        receipt=dict(case.receipt,transition_id=op,input_sha256=ih,effect_sha256=digest(effect),
            result=dict(action='workspace-account-create',before=intent['before'],after=after))
        journal=case.reopen()  # crash before receiver reply delivery
        completed=journal.complete(op,ih,receipt=receipt)
        journal=case.reopen()
        assert journal.complete(op,ih,receipt=receipt)==completed
        assert journal.read(original.OP,case.input)==old
        assert journal.capture_source()==source
        altered=copy.deepcopy(receipt);altered['result']['after']['runtime_role']['scopes']=[]
        import pytest
        with pytest.raises(ValueError):journal.complete(op,ih,receipt=altered)
    finally:case.doCleanups()
