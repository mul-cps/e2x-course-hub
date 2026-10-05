"""First-start empty local foundation; never overwrite administrator configuration."""
import json
import os
from pathlib import Path

def ensure_initial_config(path):
    path=Path(path)
    if path.exists():return False
    path.parent.mkdir(parents=True,exist_ok=True)
    (path.parent/'courses').mkdir(exist_ok=True)
    (path.parent/'profiles').mkdir(exist_ok=True)
    mounts=path.parent/'cps-empty-mounts.yaml'
    try:
        with mounts.open('x') as stream:stream.write('{}\n')
    except FileExistsError:pass
    payload={'course_config_dir':'courses','profile_dir':'profiles',
             'mount_definitions_file':mounts.name,'roles':{}}
    # O_EXCL handles racing first starts without replacing the other writer's file.
    try:
        fd=os.open(str(path),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:return False
    with os.fdopen(fd,'w') as stream:
        stream.write(json.dumps(payload,indent=2)+'\n')
        stream.flush();os.fsync(stream.fileno())
    return True
