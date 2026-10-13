import json
from pathlib import Path
import tempfile
import unittest
from aiusage.release import build_manifest,snapshot,verify_payload,MANIFEST


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.root=self.base/'source';self.root.mkdir()
        (self.root/'pyproject.toml').write_text('[project]\nversion="0.1.0"\n')
        for name,data in [('src/aiusage/server.py','server-v1'),('web/index.html','frontend-v1'),
                          ('native/MenuBar.swift','native-v1'),('scripts/install.py','installer-v1')]:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(data)
        self.releases=self.base/'releases'
    def tearDown(self):
        for path in self.base.rglob('*'):
            if not path.is_symlink():path.chmod(0o700 if path.is_dir() else 0o600)
    def test_payload_survives_checkout_edits_and_moves(self):
        payload,manifest=snapshot(self.root,self.releases)
        (self.root/'src/aiusage/server.py').write_text('edited-checkout')
        self.root.rename(self.base/'moved-source')
        self.assertEqual((payload/'src/aiusage/server.py').read_text(),'server-v1')
        self.assertEqual(verify_payload(payload),manifest)
        self.assertEqual(payload.stat().st_mode&0o777,0o555)
        self.assertEqual((payload/MANIFEST).stat().st_mode&0o777,0o444)
    def test_no_credentials_or_runtime_files_enter_payload(self):
        for name in ['.env','data/identity-salt','work/raw-response.json','src/aiusage/__pycache__/server.pyc','src/aiusage/auth.json','scripts/raw-response.json','.githooks/.env']:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private-sentinel')
        payload,manifest=snapshot(self.root,self.releases)
        self.assertNotIn('private-sentinel',json.dumps(manifest))
        self.assertFalse((payload/'.env').exists());self.assertFalse((payload/'data').exists())
        self.assertFalse((payload/'src/aiusage/auth.json').exists())
    def test_tampered_payload_is_rejected_and_never_overwritten(self):
        payload,_=snapshot(self.root,self.releases);file=payload/'web/index.html';file.chmod(0o600);file.write_text('tampered')
        with self.assertRaises(ValueError):verify_payload(payload)
        with self.assertRaises(ValueError):snapshot(self.root,self.releases)
        self.assertEqual(file.read_text(),'tampered')
    def test_new_release_keeps_previous_payload(self):
        old,first=snapshot(self.root,self.releases)
        (self.root/'web/index.html').write_text('frontend-v2')
        new,second=snapshot(self.root,self.releases)
        self.assertNotEqual(first['source_fingerprint'],second['source_fingerprint'])
        self.assertTrue(old.exists());self.assertTrue(new.exists())
    def test_symlink_source_cannot_copy_external_data(self):
        (self.root/'web/private.html').symlink_to(self.base/'private')
        with self.assertRaises(ValueError):build_manifest(self.root)


if __name__=='__main__':unittest.main()
