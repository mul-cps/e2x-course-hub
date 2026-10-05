import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

spec = importlib.util.spec_from_file_location('verify_compute_wheel',
    Path(__file__).resolve().parents[1]/'compute-artifacts/verify_wheel.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ComputeArtifactTests(unittest.TestCase):
    def test_generic_build_omits_sdk_without_substitution(self):
        self.assertIsNone(module.verify('', '', Path('/missing')))
        with self.assertRaises(ValueError):
            module.verify('', 'a'*64, Path('/missing'))

    def test_missing_checksum_traversal_and_tampering_fail_before_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            name='cps_compute-0.1.0-py3-none-any.whl'
            wheel=root/name
            wheel.write_bytes(b'tampered bytes')
            for filename,digest in [(name,''),('../'+name,'a'*64),
                                    (name,'a'*64),('cps_compute-missing.whl','a'*64)]:
                with self.assertRaises(ValueError):module.verify(filename,digest,root)

    def test_reviewed_bytes_must_name_the_compute_sdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);name='cps_compute-0.1.0-py3-none-any.whl';wheel=root/name
            with ZipFile(wheel,'w') as archive:
                archive.writestr('cps_compute-0.1.0.dist-info/METADATA','Name: other-project\nVersion: 0.1.0\n')
            with self.assertRaisesRegex(ValueError,'not the cps-compute'):
                module.verify(name,hashlib.sha256(wheel.read_bytes()).hexdigest(),root)
            with ZipFile(wheel,'w') as archive:
                archive.writestr('cps_compute-0.1.0.dist-info/METADATA','Name: cps-compute\nVersion: 0.1.0\n')
            self.assertEqual(module.verify(name,hashlib.sha256(wheel.read_bytes()).hexdigest(),root),wheel)
