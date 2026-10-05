"""Build-only reviewed SDK verification; never substitute an index SDK package."""
import hashlib
import re
import subprocess
import sys
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def verify(name, digest, directory):
    if not name:
        if digest:
            raise ValueError('COMPUTE_WHEEL_SHA256 requires COMPUTE_WHEEL')
        return None
    if not re.fullmatch(r'cps_compute-[A-Za-z0-9_.+-]+\.whl', name):
        raise ValueError('COMPUTE_WHEEL must be a cps_compute wheel basename')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('Reviewed COMPUTE_WHEEL_SHA256 is required')
    wheel = directory / name
    if wheel.is_symlink() or not wheel.is_file():
        raise ValueError('Reviewed compute wheel is missing or not a regular file')
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != digest.lower():
        raise ValueError('Reviewed compute wheel SHA256 mismatch')
    with ZipFile(wheel) as archive:
        metadata = [path for path in archive.namelist() if path.endswith('.dist-info/METADATA')]
        if len(metadata) != 1:
            raise ValueError('Compute wheel must contain exactly one project metadata file')
        project = BytesParser().parsebytes(archive.read(metadata[0]))['Name']
        if not project or re.sub(r'[-_.]+', '-', project).lower() != 'cps-compute':
            raise ValueError('Reviewed wheel is not the cps-compute SDK')
    return wheel


if __name__ == '__main__':
    wheel = verify(sys.argv[1], sys.argv[2], Path(__file__).resolve().parent)
    if wheel is not None:
        subprocess.check_call([sys.executable, '-m', 'pip', 'wheel',
                               '--wheel-dir', '/wheels', str(wheel)])
