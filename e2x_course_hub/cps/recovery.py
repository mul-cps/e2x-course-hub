"""Compose private CPS/CIT backups and check an isolated, connected restore.

This module never starts a server, changes a database, invokes kubectl, or
creates credentials. The operator supplies existing backups and an isolated
restore. Network verification uses authenticated GETs on loopback only.
"""
import argparse
import base64
from contextlib import closing
import datetime as dt
import hashlib
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

SOURCES = {'cps': 'jupyterhub', 'cit': 'cit-jhub'}
IMAGE = re.compile(r'[^\s@]+@sha256:[a-f0-9]{64}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
SOURCE_FIELDS = {'console_bundle', 'config', 'server_config', 'hub_fingerprint',
                 'storage', 'actor', 'owner', 'server', 'course_id', 'notebook_image'}
TARGET_FIELDS = {'namespace', 'origin', 'cookie_jar', 'console_database', 'config',
                 'server_config', 'hub_fingerprint', 'pod', 'pvc', 'pv', 'hub_pod', 'console_pod'}
STORAGE_FIELDS = {'pvc_uid', 'pv_uid', 'nfs_server', 'nfs_path', 'mount_path',
                  'sentinel_path', 'sentinel_sha256'}
LIMITATIONS = ['Off-host recovery and a complete NFS data backup are separate gates',
               'Connected checks are an observed restore exercise, not production qualification']


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('Valid JSON required') from error


def _encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _closed(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Missing or unknown recovery fields')


def _path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlink recovery paths are forbidden')
    if any((p / '.git').exists() for p in (path, *path.parents)):
        raise ValueError('Credential-bearing recovery files must remain outside Git')
    return path


def _directory(path):
    path = _path(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('Operator-owned mode 0700 directory required')
    return path


def _read(path):
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise ValueError('Operator-owned mode 0600 regular file required')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(descriptor)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or path.lstat().st_ino != after.st_ino:
            raise ValueError('Recovery file changed while reading')
        return raw
    finally:
        os.close(descriptor)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('UTC capture timestamp required')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('UTC capture timestamp required') from error
    if parsed.utcoffset() != dt.timedelta(0):
        raise ValueError('UTC capture timestamp required')
    return parsed


def _canonical_path(value):
    if (not isinstance(value, str) or not value.startswith('/') or
            any(p in ('', '.', '..') for p in value.split('/')[1:])):
        raise ValueError('Canonical absolute storage path required')
    return value


def _fingerprint(raw, namespace):
    value = _json(raw)
    _closed(value, {'namespace', 'captured_at', 'fingerprints'})
    if value['namespace'] != namespace:
        raise ValueError('Foreign Hub fingerprint scope')
    _stamp(value['captured_at'])
    fingerprints = value['fingerprints']
    _closed(fingerprints, {'schema', 'tables', 'roles', 'role_members', 'acls'})
    if not isinstance(fingerprints['schema'], str) or not re.fullmatch(r'[a-f0-9]+:[a-f0-9]{64}', fingerprints['schema']):
        raise ValueError('Hub schema revision and structural SHA256 required')
    tables = fingerprints['tables']
    if not isinstance(tables, dict) or not {'users', 'alembic_version'} <= set(tables):
        raise ValueError('All Hub table fingerprints, including schema, required')
    if any(not isinstance(v, str) or not SHA.fullmatch(v) for v in
           [*tables.values(), fingerprints['roles'], fingerprints['role_members'], fingerprints['acls']]):
        raise ValueError('Full SHA256 PostgreSQL fingerprints required')
    return value


def _running_image(raw):
    images = set()
    for line in raw.decode().splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith('hub-'):
            continue
        for field in fields[1:]:
            name, separator, image = field.partition('=')
            if name == 'hub' and separator:
                if image.startswith('docker-pullable://'):
                    image = image[len('docker-pullable://'):]
                if not IMAGE.fullmatch(image):
                    raise ValueError('Actual immutable Hub image required')
                images.add(image)
    if len(images) != 1:
        raise ValueError('One captured actual Hub image per source required')
    return images.pop()


def _sqlite(path, source, course_id):
    # The composed bundle contains the online-backup output, with no live WAL.
    # Targets must likewise be operator-taken online snapshots, not live files.
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-journal')):
        raise ValueError('Use an online SQLite backup rather than a live database')
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Console SQLite integrity check failed')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name,tbl_name').fetchall()
        tables = sorted(row[1] for row in schema if row[0] == 'table')
        if version != 6 or not {'records', 'console_owner', 'email_links', 'reviewed_account_aliases', 'audit'} <= set(tables):
            raise ValueError('Matching console schema version 6 required')
        owners = {r[0] for r in db.execute('SELECT owner FROM console_owner')}
        for table in ('records', 'email_links', 'reviewed_account_aliases', 'audit'):
            owners.update(r[0] for r in db.execute('SELECT DISTINCT console FROM ' + table))
        if owners != {source}:
            raise ValueError('Foreign or unowned console database')
        fingerprints = {}
        for table in tables:
            identifier = '"' + table.replace('"', '""') + '"'
            rows = db.execute('SELECT * FROM ' + identifier).fetchall()
            def canonical(row):
                return [({'blob': base64.b64encode(v).decode()} if isinstance(v, bytes) else v) for v in row]
            serialized = sorted(_encode(canonical(row)).decode() for row in rows)
            fingerprints[table] = _sha(_encode(serialized))
        course = db.execute('SELECT payload FROM records WHERE console=? AND kind=? AND id=?',
                            (source, 'courses', course_id)).fetchone()
        if course is None:
            raise ValueError('Expected restored course must exist in captured console')
        course = _json(course[0])
        return {'user_version': version, 'schema': _sha(_encode(schema)),
                'tables': fingerprints, 'course': _sha(_encode(course))}


def _write(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(files, output):
    output = _path(output)
    parent = _directory(output.parent)
    if output.exists():
        raise ValueError('New recovery destination required')
    staging = Path(tempfile.mkdtemp(prefix='.recovery-partial-', dir=parent))
    try:
        for name, raw in files.items():
            _write(staging / name, raw)
        fd = os.open(staging, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # No overwrite of an existing operator result, including an empty dir.
        if output.exists():
            raise ValueError('Recovery destination appeared during capture')
        staging.rename(output)
        fd = os.open(parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _writer_window(value, stamps):
    """Bind two raw zero-writer observations around sequential captures.

    Unchanged Deployment UID/generation/spec excludes a scale/rollout between
    observations. This is application writer exclusion, never global database
    or storage atomicity; exclusive operator custody remains an operating gate.
    """
    _closed(value, {'before', 'after'})
    raw = {phase: _read(value[phase]) for phase in ('before', 'after')}
    snapshots = {phase: _json(body) for phase, body in raw.items()}
    expected = {'cps_hub': ('jupyterhub', 'hub'), 'cit_hub': ('cit-jhub', 'hub'),
                'cps_console': ('cps-compute', 'cps-admin'), 'cit_console': ('cps-compute', 'cit-admin')}
    identities = {}
    for phase, snapshot in snapshots.items():
        _closed(snapshot, {'captured_at', 'writers'})
        _closed(snapshot['writers'], set(expected))
        identities[phase] = {}
        for writer, (namespace, name) in expected.items():
            record = snapshot['writers'][writer]
            _closed(record, {'deployment', 'pods'})
            deployment, pods = record['deployment'], record['pods']
            meta = deployment.get('metadata', {}); spec = deployment.get('spec', {}); status = deployment.get('status', {})
            selector = spec.get('selector', {})
            if (deployment.get('apiVersion') != 'apps/v1' or deployment.get('kind') != 'Deployment' or
                    meta.get('namespace') != namespace or meta.get('name') != name or not meta.get('uid') or
                    not isinstance(meta.get('generation'), int) or spec.get('replicas') != 0 or
                    status.get('replicas', 0) != 0 or status.get('readyReplicas', 0) != 0 or
                    status.get('observedGeneration') != meta['generation'] or
                    set(selector) != {'matchLabels'} or not selector['matchLabels']):
                raise ValueError('Exact paused source writer Deployment required')
            if pods.get('apiVersion') != 'v1' or pods.get('kind') not in ('List', 'PodList') or not isinstance(pods.get('items'), list):
                raise ValueError('Actual namespace Pod inventory required')
            for pod in pods['items']:
                pod_meta = pod.get('metadata', {})
                if pod_meta.get('namespace') != namespace:
                    raise ValueError('Foreign writer Pod inventory namespace')
                if all(pod_meta.get('labels', {}).get(k) == v for k, v in selector['matchLabels'].items()):
                    raise ValueError('Source writer Pod remains present')
            identities[phase][writer] = (meta['uid'], meta['generation'], spec)
    before = _stamp(snapshots['before']['captured_at']); after = _stamp(snapshots['after']['captured_at'])
    if (identities['before'] != identities['after'] or not dt.timedelta(0) < after - before <= dt.timedelta(minutes=10) or
            any(not before <= stamp <= after for stamp in stamps)):
        raise ValueError('Stable bounded writer window must bracket every capture')
    return {'writer-before.json': raw['before'], 'writer-after.json': raw['after']}


def capture(inputs, output, hub_validator):
    """Bind existing Hub/console backups without inventing atomicity or proof."""
    _closed(inputs, {'version', 'capture_id', 'hub_bundle', 'sources'} |
            ({'writer_window'} if isinstance(inputs, dict) and 'writer_window' in inputs else set()))
    if inputs['version'] != 1 or str(uuid.UUID(inputs['capture_id'])) != inputs['capture_id']:
        raise ValueError('Version 1 and canonical capture UUID required')
    _closed(inputs['sources'], set(SOURCES))
    hub = _directory(inputs['hub_bundle'])
    plans = hub_validator(hub)  # Existing reviewed restore validator, no process/network.
    _closed(plans, set(SOURCES.values()))
    hub_raw = _read(hub / 'manifest.json')
    hub_manifest = _json(hub_raw)
    stamps = [_stamp(hub_manifest['created_at'])]
    files = {'hub-capture-manifest.json': hub_raw}
    for name, pin in hub_manifest['files'].items():
        if Path(name).name != name or name in ('.', '..', 'manifest.json'):
            raise ValueError('Invalid Hub backup filename')
        raw = _read(hub / name)
        if _sha(raw) != pin['sha256'] or len(raw) != pin['bytes']:
            raise ValueError('Hub capture checksum mismatch')
        files[name] = raw
    sources = {}
    # SQLite validation uses private temporary copies of the already read bytes.
    with tempfile.TemporaryDirectory(prefix='cps-recovery-', dir=_directory(_path(output).parent)) as temporary:
        for source, namespace in SOURCES.items():
            spec = inputs['sources'][source]
            _closed(spec, SOURCE_FIELDS)
            _closed(spec['storage'], STORAGE_FIELDS)
            for field in ('actor', 'owner', 'server', 'course_id'):
                if not isinstance(spec[field], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', spec[field]):
                    raise ValueError('Explicit safe source actor and fixture identifiers required')
            if not IMAGE.fullmatch(spec['notebook_image']):
                raise ValueError('Immutable notebook image required')
            storage = spec['storage']
            if not SHA.fullmatch(storage['sentinel_sha256']) or not all(isinstance(v, str) and v for v in storage.values()):
                raise ValueError('Complete source storage identity required')
            _canonical_path(storage['nfs_path']); _canonical_path(storage['mount_path'])
            sentinel = storage['sentinel_path']
            if sentinel.startswith('/') or any(p in ('', '.', '..') for p in sentinel.split('/')):
                raise ValueError('Relative sentinel path required')
            console = _directory(spec['console_bundle'])
            manifest_raw = _read(console / 'manifest.json')
            manifest = _json(manifest_raw)
            stamps.append(_stamp(manifest['created_at']))
            raw = _read(console / 'console.sqlite')
            if (manifest['owner'] != source or manifest['user_version'] != 6 or
                    _sha(raw) != manifest['sha256']['console.sqlite'] or
                    not IMAGE.fullmatch(manifest['application_image']) or
                    not re.fullmatch(r'sha256:[a-f0-9]{64}', manifest['policy_hash'])):
                raise ValueError('Console backup owner, version, provenance or checksum mismatch')
            snapshot = Path(temporary) / (source + '.sqlite')
            _write(snapshot, raw)
            semantic = _sqlite(snapshot, source, spec['course_id'])
            source_files = {'console.sqlite': raw, 'console-manifest.json': manifest_raw,
                            'config.py': _read(spec['config']), 'config.yml': _read(spec['server_config']),
                            'postgres.json': _read(spec['hub_fingerprint'])}
            fingerprint = _fingerprint(source_files['postgres.json'], namespace)
            stamps.append(_stamp(fingerprint['captured_at']))
            for name, body in source_files.items():
                output_name = source + '-' + name
                if output_name in files:
                    raise ValueError('Recovery filename collision')
                files[output_name] = body
            sources[source] = {k: spec[k] for k in ('storage', 'actor', 'owner', 'server', 'course_id', 'notebook_image')}
            sources[source].update(namespace=namespace, sqlite=semantic,
                                   postgres=fingerprint['fingerprints'],
                                   application_image=manifest['application_image'], policy_hash=manifest['policy_hash'],
                                   database_image=plans[namespace]['image'],
                                   hub_image=_running_image(files[namespace + '-running-images.txt']))
    coordinated = 'writer_window' in inputs
    if coordinated:
        files.update(_writer_window(inputs['writer_window'], stamps))
    report = {'version': 1, 'capture_id': inputs['capture_id'],
              'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'coordinated_capture': coordinated, 'connected_restore_passed': False,
              'productionQualified': False, 'sources': sources,
              'limitations': [('Sequential captures bracketed by unchanged paused application writers; '
                               'continuous exclusive operator custody required; no global atomic snapshot') if coordinated else
                              'Input captures are sequential; no global atomic snapshot or writer exclusion asserted', *LIMITATIONS],
              'files': {name: {'sha256': _sha(raw), 'bytes': len(raw)} for name, raw in files.items()}}
    files['manifest.json'] = _encode(report)
    _publish(files, output)
    return report


def _bundle(path):
    root = _directory(path)
    manifest = _json(_read(root / 'manifest.json'))
    if manifest.get('version') != 1 or set(manifest.get('sources', {})) != set(SOURCES):
        raise ValueError('Composed CPS/CIT recovery bundle required')
    files = {}
    for name, pin in manifest['files'].items():
        if Path(name).name != name or name in ('.', '..', 'manifest.json'):
            raise ValueError('Invalid recovery filename')
        raw = _read(root / name)
        if _sha(raw) != pin['sha256'] or len(raw) != pin['bytes']:
            raise ValueError('Composed recovery checksum mismatch')
        files[name] = raw
    return root, manifest, files


def _origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', '::1') or
            parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment or
            parsed.port is None):
        raise ValueError('Explicit isolated loopback HTTP(S) origin required')
    return value


def _application_image(path, namespace, prefix, container_name, expected):
    pod = _json(_read(path)); meta = pod.get('metadata', {}); status = pod.get('status', {})
    containers = [c for c in pod.get('spec', {}).get('containers', []) if c.get('name') == container_name]
    observed = [c for c in status.get('containerStatuses', []) if c.get('name') == container_name]
    image_id = observed[0].get('imageID', '') if len(observed) == 1 else ''
    if image_id.startswith('docker-pullable://'):
        image_id = image_id[len('docker-pullable://'):]
    if (pod.get('apiVersion') != 'v1' or pod.get('kind') != 'Pod' or meta.get('namespace') != namespace or
            not meta.get('uid') or not meta.get('name', '').startswith(prefix + '-') or
            len(containers) != 1 or containers[0].get('image') != expected or
            len(observed) != 1 or observed[0].get('ready') is not True or image_id != expected or
            status.get('phase') != 'Running' or not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', []))):
        raise ValueError('Pinned actual isolated application image and Ready Pod required')
    return meta['uid']


def _storage(target, spec):
    namespace = target['namespace']
    pod, pvc, pv = (_json(_read(target[key])) for key in ('pod', 'pvc', 'pv'))
    def metadata(obj, kind, namespaced=True):
        meta = obj.get('metadata', {})
        if (obj.get('apiVersion') != 'v1' or obj.get('kind') != kind or not meta.get('uid') or
                not meta.get('name') or (namespaced and meta.get('namespace') != namespace)):
            raise ValueError('Exact isolated Pod/PVC/PV binding required')
        return meta
    pm = metadata(pod, 'Pod'); cm = metadata(pvc, 'PersistentVolumeClaim'); vm = metadata(pv, 'PersistentVolume', False)
    if pm['name'] != 'jupyter-' + spec['owner'] + '--' + spec['server']:
        raise ValueError('Exact restored owner notebook identity required')
    if (cm['uid'] == spec['storage']['pvc_uid'] or vm['uid'] == spec['storage']['pv_uid'] or
            pvc.get('spec', {}).get('volumeName') != vm['name'] or
            pv.get('spec', {}).get('claimRef', {}).get('namespace') != namespace or
            pv['spec']['claimRef'].get('name') != cm['name'] or pv['spec']['claimRef'].get('uid') != cm['uid']):
        raise ValueError('New isolated PVC/PV identities and complete claim chain required')
    nfs = pv['spec'].get('nfs', {})
    original = _canonical_path(spec['storage']['nfs_path']); restored = _canonical_path(nfs.get('path'))
    if (nfs.get('server') != spec['storage']['nfs_server'] or restored == original or
            restored.startswith(original + '/') or original.startswith(restored + '/')):
        raise ValueError('Restored storage must use a separate NFS copy')
    ps = pod.get('spec', {})
    if ps.get('hostNetwork') or ps.get('hostPID') or ps.get('hostIPC'):
        raise ValueError('Isolated CPU notebook required')
    containers = ps.get('containers', [])
    notebook = [c for c in containers if c.get('name') == 'notebook']
    for container in [*containers, *ps.get('initContainers', [])]:
        resources = container.get('resources', {})
        keys = set(resources.get('limits', {})) | set(resources.get('requests', {}))
        if any(re.search(r'gpu|hami|amd\.com|nvidia\.com', key, re.I) for key in keys):
            raise ValueError('GPU resources forbidden in recovery exercise')
        if container.get('securityContext', {}).get('privileged'):
            raise ValueError('Privileged recovery notebook forbidden')
    if len(notebook) != 1 or notebook[0].get('image') != spec['notebook_image']:
        raise ValueError('Pinned restored notebook image required')
    status = pod.get('status', {})
    cs = [c for c in status.get('containerStatuses', []) if c.get('name') == 'notebook']
    image_id = cs[0].get('imageID', '') if len(cs) == 1 else ''
    if image_id.startswith('docker-pullable://'):
        image_id = image_id[len('docker-pullable://'):]
    if (status.get('phase') != 'Running' or not any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', [])) or
            len(cs) != 1 or cs[0].get('ready') is not True or
            image_id != spec['notebook_image']):
        raise ValueError('Actual Running/Ready notebook and image digest required')
    volumes = ps.get('volumes', [])
    allowed = {'persistentVolumeClaim', 'emptyDir', 'secret', 'configMap', 'projected', 'downwardAPI'}
    names = []
    for volume in volumes:
        names.append(volume.get('name'))
        kind = set(volume) - {'name'}
        if len(kind) != 1 or not kind <= allowed or not volume.get('name'):
            raise ValueError('Unapproved recovery notebook volume')
        if 'persistentVolumeClaim' in volume and volume['persistentVolumeClaim'].get('claimName') != cm['name']:
            raise ValueError('Foreign source PVC volume forbidden')
    if len(names) != len(set(names)):
        raise ValueError('Duplicate recovery volume names forbidden')
    mounts = [m for m in notebook[0].get('volumeMounts', []) if m.get('mountPath') == spec['storage']['mount_path'] and m.get('readOnly') is False]
    if len(mounts) != 1 or mounts[0].get('subPath') or mounts[0].get('subPathExpr'):
        raise ValueError('Exact writable full restored store mount required')
    bound = [v for v in volumes if v.get('name') == mounts[0].get('name')]
    if len(bound) != 1 or bound[0].get('persistentVolumeClaim', {}).get('claimName') != cm['name']:
        raise ValueError('Notebook store mount must bind the isolated PVC')
    return {'pod_uid': pm['uid'], 'pvc_uid': cm['uid'], 'pv_uid': vm['uid']}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _client(origin, cookie_file):
    cookies = http.cookiejar.MozillaCookieJar()
    # Loading from a private frozen copy avoids cookie path races or updates.
    raw = _read(cookie_file)
    with tempfile.TemporaryDirectory(prefix='cps-recovery-cookie-', dir=_directory(_path(cookie_file).parent)) as temporary:
        path = Path(temporary) / 'cookies'
        _write(path, raw)
        cookies.load(str(path), ignore_discard=True)
    if not list(cookies):
        raise ValueError('Existing isolated browser login cookies required')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                        urllib.request.HTTPCookieProcessor(cookies), _NoRedirect())
    def get(path):
        try:
            with opener.open(urllib.request.Request(origin + path, method='GET'), timeout=15) as response:
                if response.status != 200 or response.headers.get_content_type() != 'application/json':
                    raise ValueError('Isolated authenticated JSON GET failed')
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError('Isolated response exceeded recovery bound')
                return _json(body)
        except (urllib.error.URLError, OSError, ValueError):
            # Never propagate response bodies, URL credentials, headers or cookies.
            raise ValueError('Isolated authenticated GET failed') from None
    return get


def verify(bundle, targets):
    """Check both isolated restored apps; only GETs occur after all local gates."""
    root, manifest, files = _bundle(bundle)
    _closed(targets, set(SOURCES))
    prepared = {}
    with tempfile.TemporaryDirectory(prefix='cps-recovery-check-', dir=root.parent) as temporary:
        for source, namespace in SOURCES.items():
            target = targets[source]; spec = manifest['sources'][source]
            _closed(target, TARGET_FIELDS)
            expected_namespace = 'restore-' + source + '-' + manifest['capture_id'][:8]
            if target['namespace'] != expected_namespace or spec['namespace'] != namespace:
                raise ValueError('Source-specific isolated restore namespace required')
            origin = _origin(target['origin'])
            for field, name in (('config', 'config.py'), ('server_config', 'config.yml')):
                if _read(target[field]) != files[source + '-' + name]:
                    raise ValueError('Restored mounted configuration differs from capture')
            raw = _read(target['console_database'])
            snapshot = Path(temporary) / (source + '.sqlite'); _write(snapshot, raw)
            if any(Path(target['console_database'] + suffix).exists() for suffix in ('-wal', '-journal')):
                raise ValueError('Restored console requires an online backup snapshot')
            if _sqlite(snapshot, source, spec['course_id']) != spec['sqlite']:
                raise ValueError('Restored console schema or rows differ from capture')
            postgres = _fingerprint(_read(target['hub_fingerprint']), namespace)
            if postgres['fingerprints'] != spec['postgres']:
                raise ValueError('Restored Hub rows, roles, memberships or ACLs differ from capture')
            storage = _storage(target, spec)
            storage['hub_pod_uid'] = _application_image(target['hub_pod'], target['namespace'], 'hub', 'hub', spec['hub_image'])
            storage['console_pod_uid'] = _application_image(target['console_pod'], target['namespace'], source + '-admin', 'console', spec['application_image'])
            prepared[source] = (spec, storage, _client(origin, target['cookie_jar']))
        if targets['cps']['origin'] == targets['cit']['origin']:
            raise ValueError('CPS and CIT require distinct isolated origins')
        results = {}
        for source, (spec, storage, get) in prepared.items():
            user = get('/hub/api/user')
            if user.get('name') != spec['actor'] or user.get('admin') is not True:
                raise ValueError('Actual isolated owning Hub administrator login required')
            records = get('/services/' + source + '-admin/api/local/courses').get('records', [])
            course = [r for r in records if isinstance(r, dict) and r.get('id') == spec['course_id']]
            if len(course) != 1 or _sha(_encode(course[0])) != spec['sqlite']['course']:
                raise ValueError('Connected restored console course differs from captured rows')
            owner = get('/hub/api/users/' + urllib.parse.quote(spec['owner'], safe=''))
            server = owner.get('servers', {}).get(spec['server'], {})
            server_url = '/user/' + spec['owner'] + '/' + spec['server'] + '/'
            if owner.get('name') != spec['owner'] or server.get('ready') is not True or server.get('pending') is not None or server.get('url') != server_url:
                raise ValueError('Normal isolated named server must be Running/Ready')
            sentinel = get(server_url + 'api/contents/' + urllib.parse.quote(spec['storage']['sentinel_path'], safe='/'))
            if sentinel.get('type') != 'file' or sentinel.get('format') not in ('text', 'base64'):
                raise ValueError('Saved store sentinel must be readable through notebook API')
            try:
                raw = (sentinel['content'].encode() if sentinel['format'] == 'text' else
                       base64.b64decode(sentinel['content'], validate=True))
            except (KeyError, TypeError, ValueError):
                raise ValueError('Saved sentinel content encoding invalid') from None
            if _sha(raw) != spec['storage']['sentinel_sha256']:
                raise ValueError('Restored mounted store sentinel differs from capture')
            results[source] = {**storage, 'login_passed': True, 'console_passed': True,
                               'running_server_passed': True, 'store_sentinel_passed': True,
                               'sqlite_rows_passed': True, 'hub_rows_roles_acl_passed': True}
    return {'version': 1, 'capture_id': manifest['capture_id'], 'bundle_sha256': _sha(_read(root / 'manifest.json')),
            'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'coordinated_capture': manifest['coordinated_capture'], 'connected_restore_passed': True,
            'productionQualified': False, 'sources': results, 'limitations': manifest['limitations']}


def _validator(path, expected_sha):
    # This imports only the reviewed existing validator. No default tool path,
    # subprocess or shell fallback exists; its main() is not invoked.
    path = Path(path)
    raw = path.read_bytes()
    if path.is_symlink() or not SHA.fullmatch(expected_sha) or _sha(raw) != expected_sha:
        raise ValueError('Pinned original Hub restore validator required')
    module_spec = importlib.util.spec_from_file_location('reviewed_hub_restore', path)
    module = importlib.util.module_from_spec(module_spec)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module.validate_bundle


def postgres_fingerprint_sql(source, bootstrap_role=None):
    """Return one fixed read-only psql input; output contains hashes, not rows.

    The original isolated restore creates exactly one random restore_<hex>
    bootstrap role. Only that explicitly named role may be excluded; source
    fingerprints exclude nothing. Object/role references use names, not OIDs.
    """
    if source not in SOURCES or (bootstrap_role is not None and
                                not re.fullmatch(r'restore_[a-f0-9]{16}', bootstrap_role)):
        raise ValueError('Exact source and original-tool bootstrap role required')
    excluded = "NULL::text" if bootstrap_role is None else "'" + bootstrap_role + "'::text"
    return """\\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL timezone = 'UTC';
WITH excluded AS (SELECT EXCLUDED AS name),
role_rows AS (
  SELECT jsonb_build_array(a.rolname,a.rolsuper,a.rolinherit,a.rolcreaterole,a.rolcreatedb,
    a.rolcanlogin,a.rolreplication,a.rolconnlimit,a.rolpassword,a.rolvaliduntil,a.rolbypassrls,r.rolconfig) AS row
  FROM pg_authid a JOIN pg_roles r ON r.oid=a.oid
  WHERE a.rolname IS DISTINCT FROM (SELECT name FROM excluded)
), member_rows AS (
  SELECT jsonb_build_array(r.rolname,m.rolname,g.rolname,a.admin_option,
    to_jsonb(a)->'inherit_option',to_jsonb(a)->'set_option') AS row
  FROM pg_auth_members a JOIN pg_roles r ON r.oid=a.roleid
    JOIN pg_roles m ON m.oid=a.member JOIN pg_roles g ON g.oid=a.grantor
  WHERE r.rolname IS DISTINCT FROM (SELECT name FROM excluded)
    AND m.rolname IS DISTINCT FROM (SELECT name FROM excluded)
), objects AS (
  SELECT 'database' AS kind,datname::text AS name,datdba AS owner,
    coalesce(datacl,acldefault('d',datdba)) AS acl FROM pg_database WHERE datname=current_database()
  UNION ALL SELECT 'schema',nspname,nspowner,coalesce(nspacl,acldefault('n',nspowner))
    FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
  UNION ALL SELECT 'relation',n.nspname||'.'||c.relname,c.relowner,
    coalesce(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 's' ELSE 'r' END::"char",c.relowner))
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
      AND c.relkind IN ('r','p','v','m','S','f')
  UNION ALL SELECT 'function',n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')',
    p.proowner,coalesce(p.proacl,acldefault('f',p.proowner)) FROM pg_proc p
    JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
  UNION ALL SELECT 'default:'||d.defaclobjtype::text,coalesce(n.nspname,'*'),d.defaclrole,d.defaclacl
    FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace
), acl_rows AS (
  SELECT jsonb_build_array(o.kind,o.name,r.rolname,coalesce(g.rolname,'PUBLIC'),
    coalesce(t.rolname,'PUBLIC'),a.privilege_type,a.is_grantable) AS row
  FROM objects o JOIN pg_roles r ON r.oid=o.owner LEFT JOIN LATERAL aclexplode(o.acl) a ON true
    LEFT JOIN pg_roles g ON g.oid=a.grantor LEFT JOIN pg_roles t ON t.oid=a.grantee
), schema_rows AS (
  SELECT jsonb_build_array('relation',n.nspname,c.relname,c.relkind,c.relpersistence,
    CASE WHEN c.relkind IN ('v','m') THEN pg_get_viewdef(c.oid,true) ELSE NULL END) AS row
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
    AND c.relkind IN ('r','p','v','m','S','f')
  UNION ALL SELECT jsonb_build_array('column',n.nspname,c.relname,a.attnum,a.attname,
    format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid))
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
  WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
    AND c.relkind IN ('r','p','v','m','S','f') AND a.attnum>0 AND NOT a.attisdropped
  UNION ALL SELECT jsonb_build_array('constraint',n.nspname,c.relname,k.conname,pg_get_constraintdef(k.oid,true))
  FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
  UNION ALL SELECT jsonb_build_array('index',n.nspname,c.relname,i.relname,pg_get_indexdef(i.oid))
  FROM pg_index x JOIN pg_class c ON c.oid=x.indrelid JOIN pg_class i ON i.oid=x.indexrelid
    JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
), table_rows AS (
  SELECT n.nspname||'.'||c.relname AS name,
    encode(sha256(convert_to(query_to_xml(format(
      CASE WHEN c.relkind='S' THEN 'SELECT jsonb_build_array(last_value,is_called) AS row FROM %I.%I'
      ELSE 'SELECT to_jsonb(t) AS row FROM %I.%I t ORDER BY to_jsonb(t)::text' END,n.nspname,c.relname),
      false,true,'')::text,'UTF8')),'hex') AS hash
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' AND c.relkind IN ('r','p','S')
)
SELECT jsonb_build_object('namespace','NAMESPACE','captured_at',
  to_char(transaction_timestamp(),'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),'fingerprints',jsonb_build_object(
  'schema',(SELECT version_num FROM public.alembic_version)||':'||
    (SELECT encode(sha256(convert_to(coalesce(jsonb_agg(row ORDER BY row::text),'[]'::jsonb)::text,'UTF8')),'hex') FROM schema_rows),
  'tables',(SELECT jsonb_object_agg(CASE WHEN name LIKE 'public.%' THEN substr(name,8) ELSE name END,hash) FROM table_rows),
  'roles',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(row ORDER BY row::text),'[]'::jsonb)::text,'UTF8')),'hex') FROM role_rows),
  'role_members',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(row ORDER BY row::text),'[]'::jsonb)::text,'UTF8')),'hex') FROM member_rows),
  'acls',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(row ORDER BY row::text),'[]'::jsonb)::text,'UTF8')),'hex') FROM acl_rows)));
COMMIT;
""".replace('EXCLUDED', excluded).replace('NAMESPACE', SOURCES[source])


class LogicalRestoreProbe:
    """Optional callback for the existing restore loop, never a second restore.

    The owner of restore-hub-backups.py calls this with its namespace, exact
    bootstrap role, and existing psql transport before fixture cleanup. With no
    contemporaneous source fingerprints it records logical-only observations.
    """
    def __init__(self, output, source_fingerprints=None):
        self.expected = None
        if source_fingerprints is not None:
            _closed(source_fingerprints, set(SOURCES))
            self.expected = {s: _fingerprint(_read(source_fingerprints[s]), ns)['fingerprints']
                             for s, ns in SOURCES.items()}
        self.output = _path(output)
        _directory(self.output.parent)
        if self.output.exists():
            raise ValueError('New logical fingerprint destination required')
        self.output.mkdir(mode=0o700)
        self.results = {}

    def __call__(self, namespace, bootstrap_role, execute_sql):
        source = next((s for s, ns in SOURCES.items() if ns == namespace), None)
        if source is None or source in self.results:
            raise ValueError('Exact source once per logical restore required')
        if not isinstance(bootstrap_role, str) or not re.fullmatch(r'restore_[a-f0-9]{16}', bootstrap_role):
            raise ValueError('Exact original logical restore bootstrap role required')
        query = postgres_fingerprint_sql(source, bootstrap_role).encode()
        raw = execute_sql(query)
        captured = _fingerprint(raw, namespace)
        _write(self.output / (source + '-restored-fingerprint.json'), raw)
        if self.expected is not None and captured['fingerprints'] != self.expected[source]:
            raise ValueError('Logical restore rows, schema, roles or ACLs differ from source')
        result = {'fingerprint_sha256': _sha(raw), 'query_sha256': _sha(query),
                  'source_comparison_passed': self.expected is not None}
        self.results[source] = result
        return result

    def finish(self):
        if set(self.results) != set(SOURCES):
            raise ValueError('Both logical source fingerprints required before receipt')
        report = {'logical_fingerprints_captured': True, 'sources': self.results,
                  'source_comparison_passed': self.expected is not None,
                  'coordinated_capture': False, 'connected_restore_passed': False,
                  'productionQualified': False,
                  'limitations': ['Logical restore only; actual source equality requires contemporaneous source fingerprints',
                                  'Connected OAuth/Start/store, coordinated capture and off-host recovery remain separate gates']}
        _write(self.output / 'report.json', _encode(report))
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('capture', 'verify', 'postgres-sql'))
    parser.add_argument('--execute', action='store_true', help='Explicitly enable local capture or loopback read-only checks')
    parser.add_argument('--input', help='Mode 0600 private capture inputs or restore targets JSON')
    parser.add_argument('--output', required=True, help='New private capture directory or mode 0600 verification receipt')
    parser.add_argument('--bundle', help='Composed capture directory, required for verify')
    parser.add_argument('--hub-validator', help='Existing reviewed restore-hub-backups.py, required for capture')
    parser.add_argument('--hub-validator-sha256', help='Raw reviewed validator SHA256, required for capture')
    parser.add_argument('--source', choices=tuple(SOURCES), help='Exact scope for postgres-sql')
    parser.add_argument('--bootstrap-role', help='Only the original restore tool exact random bootstrap role')
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error('--execute is required; no default capture or network operation')
    try:
        if args.action == 'postgres-sql':
            if not args.source or any((args.input, args.bundle, args.hub_validator, args.hub_validator_sha256)):
                raise ValueError('Only exact source and optional bootstrap role accepted for SQL')
            _directory(_path(args.output).parent)
            _write(_path(args.output), postgres_fingerprint_sql(args.source, args.bootstrap_role).encode())
            print('Read-only PostgreSQL fingerprint query prepared')
            return
        if not args.input or args.source or args.bootstrap_role:
            raise ValueError('Private input JSON and no SQL options required')
        inputs = _json(_read(args.input))
        if args.action == 'capture':
            if not args.hub_validator or not args.hub_validator_sha256 or args.bundle:
                raise ValueError('Explicit original validator path and SHA required for capture')
            report = capture(inputs, args.output, _validator(args.hub_validator, args.hub_validator_sha256))
        else:
            if not args.bundle or args.hub_validator or args.hub_validator_sha256:
                raise ValueError('Explicit composed bundle required for verify')
            _directory(_path(args.output).parent)
            if _path(args.output).exists():
                raise ValueError('New private acceptance receipt required')
            report = verify(args.bundle, inputs)
            _write(_path(args.output), _encode(report))
        # No names, rows, configuration, cookies, roles or credential paths.
        print(json.dumps({key: report[key] for key in ('coordinated_capture', 'connected_restore_passed', 'productionQualified')}))
    except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError):
        parser.exit(1, 'Recovery acceptance failed; private input or isolated connected check did not pass\n')


if __name__ == '__main__':
    main()
