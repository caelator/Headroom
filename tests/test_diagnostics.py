import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('diagnostics',Path(__file__).resolve().parents[1]/'scripts/diagnostics.py')
diagnostics=importlib.util.module_from_spec(spec);spec.loader.exec_module(diagnostics)

class DiagnosticsTests(unittest.TestCase):
    def test_runtime_environment_never_leaves_filtered_diagnostics(self):
        with patch.object(diagnostics.subprocess,'run') as run:
            run.return_value.returncode=0
            run.return_value.stdout=b'state = running\npid = 123\ninherited environment = {\n MODEL_API_KEY => PRIVATE_TEST_KEY\n}\n'
            result=diagnostics.agent_status('com.xxx.aiusage')
        self.assertTrue(result['running']);self.assertEqual(result['pid'],123)
        self.assertNotIn('PRIVATE_TEST_KEY',json.dumps(result));self.assertNotIn('MODEL_API_KEY',json.dumps(result))
    def test_unregistered_status(self):
        with patch.object(diagnostics.subprocess,'run') as run:
            run.return_value.returncode=1;result=diagnostics.agent_status('service')
        self.assertFalse(result['registered'])
    def test_probe_reports_only_allowlisted_fields(self):
        class Fake:
            def __init__(self,store):self.store=store
            def close(self):pass
            def fetch(self,provider):self.store.save_provider({'id':'codex','status':'partial','source':'codex_oauth',
                'windows':[],'account_ref':'private-identity','access_token':'private-token',
                'profile':{'email':'private@example.invalid'}})
        result=diagnostics.probe('codex',factory=Fake)
        self.assertEqual(len(result['providers']),1)
        self.assertEqual(result['providers'][0]['source'],'codex_oauth')
        self.assertEqual(set(result['providers'][0]),{'id','status','source','window_count','fresh_window_count','error_code'})
        self.assertNotIn('private-',json.dumps(result));self.assertNotIn('private@example.invalid',json.dumps(result))
    def test_probe_failure_never_echoes_exception(self):
        class Fake:
            def __init__(self,store):self.store=store
            def close(self):pass
            def fetch(self,provider):self.store.save_provider({'id':provider,'status':'error','windows':[],
                'error':{'code':'collector_error','message':'private-token'}})
        result=diagnostics.probe('codex',factory=Fake)
        self.assertEqual(result['providers'][0]['error_code'],'collector_error')
        self.assertNotIn('private-token',json.dumps(result))

class InstalledIntegrityTests(unittest.TestCase):
    """Synthetic installed trees and fake command results only; no live data."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name);self.data=self.home/'.local/share/aiusage';self.data.mkdir(parents=True)
        self.files={'src/aiusage/server.py':'server-v1','web/index.html':'frontend-v1',
                    'pyproject.toml':'[project]\nversion="0.1.0"\n'}
        digest=hashlib.sha256();items=[]
        for name in sorted(self.files):
            data=self.files[name].encode();items.append({'path':name,'sha256':hashlib.sha256(data).hexdigest()})
            digest.update(name.encode()+b'\0'+str(len(data)).encode()+b'\0'+data)
        self.fingerprint=digest.hexdigest()
        self.payload=self.data/'releases'/self.fingerprint
        for name,data in self.files.items():
            path=self.payload/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(data)
        manifest={'schema':1,'api_version':1,'version':'0.1.0','source_fingerprint':self.fingerprint,'files':items}
        (self.payload/'headroom-release.json').write_text(json.dumps(manifest))
        self.bundle=self.home/'Applications/Headroom.app';binary=self.bundle/'Contents/MacOS/Headroom'
        binary.parent.mkdir(parents=True);binary.write_bytes(b'native-binary-v1');self.binary_hash=hashlib.sha256(b'native-binary-v1').hexdigest()
        info={'CFBundleIdentifier':'com.caelator.headroom','CFBundleShortVersionString':'0.1.0',
              'HeadroomSourceFingerprint':self.fingerprint,'HeadroomInstallID':'private-install-id'}
        (self.bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        receipt={'schema':1,'service':{'source_fingerprint':self.fingerprint,'payload':str(self.payload),
                   'version':'0.1.0','runtime':'/private/runtime','access_token':'private-token'},
                 'native':{'source_fingerprint':self.fingerprint,'binary_sha256':self.binary_hash,
                   'bundle_sha256':diagnostics._bundle_hash(self.bundle),
                   'bundle_identifier':'com.caelator.headroom','version':'0.1.0','cookie':'private-cookie'}}
        (self.data/'installation.json').write_text(json.dumps(receipt))
        self.now=1_700_000_000.0
        status={'updated_at':self.now-2,'pid':222,'source_fingerprint':self.fingerprint,'stale':False,
                'providers':6,'measured_providers':5,'panel_visible':True,'title':'private title','token':'private-token'}
        (self.data/'native-status.json').write_text(json.dumps(status))
        self.health={'service':{'name':'headroom','api_version':1,'version':'0.1.0',
                     'source_fingerprint':self.fingerprint,'pid':111,'access_token':'private-token'},
                     'providers':[]}
    def fake_run(self,argv,**kwargs):
        self.assertIn('111',argv)
        class Done:returncode=0;stdout=b'p111\nn127.0.0.1:8793\n'
        return Done()
    def report(self,**overrides):
        args={'home':self.home,'now':self.now,'runner':self.fake_run,
              'service_status':{'registered':True,'running':True,'pid':111},
              'menu_status':{'registered':True,'running':True,'pid':222},'health':self.health}
        args.update(overrides);return diagnostics.installed_report(**args)
    def test_installed_integrity_matches_all_fingerprints(self):
        result=self.report()
        self.assertTrue(result['payload']['manifest_valid']);self.assertTrue(result['payload']['matches_receipt'])
        self.assertTrue(result['payload']['directory_matches_fingerprint']);self.assertEqual(result['payload']['file_count'],3)
        self.assertTrue(result['service']['health_identity_valid']);self.assertTrue(result['service']['health_pid_matches_launchd'])
        self.assertTrue(result['service']['listen_owner_verified']);self.assertTrue(result['service']['matches_payload'])
        self.assertTrue(result['native']['binary_matches_receipt']);self.assertTrue(result['native']['bundle_identifier_valid'])
        self.assertTrue(result['native']['bundle_matches_payload']);self.assertTrue(result['native']['status_pid_matches_launchd'])
        self.assertTrue(result['native']['heartbeat_fresh']);self.assertEqual(result['native']['heartbeat_age_seconds'],2.0)
        self.assertEqual(len(result['native']['bundle_sha256']),64)
        self.assertTrue(result['native']['panel_visible'])
        self.assertEqual(result['native']['panel_visible_evidence'],'panel_is_visible_flag_only_not_unobscured_desktop_proof')
        self.assertTrue(result['fingerprints_match']);self.assertTrue(result['consistent'])
        encoded=json.dumps(result)
        for secret in ('private-token','private-cookie','private-install-id','private title','/private/runtime',str(self.payload),str(self.home)):
            self.assertNotIn(secret,encoded)
    def test_tampered_payload_fails_manifest_and_consistency(self):
        (self.payload/'web/index.html').write_text('tampered')
        result=self.report()
        self.assertFalse(result['payload']['manifest_valid']);self.assertFalse(result['consistent'])
    def test_unlisted_payload_code_fails_manifest(self):
        (self.payload/'src/aiusage/extra.py').write_text('unlisted-code')
        self.assertFalse(self.report()['consistent'])
    def test_stopped_agent_and_changed_bundle_are_not_consistent(self):
        self.assertFalse(self.report(menu_status={'registered':True,'running':False,'pid':222})['consistent'])
        (self.bundle/'Contents/Resources').mkdir()
        (self.bundle/'Contents/Resources/extra.txt').write_text('changed-bundle')
        self.assertFalse(self.report()['consistent'])
    def test_panel_visible_absent_is_unavailable_and_legacy_is_same_evidence(self):
        status=json.loads((self.data/'native-status.json').read_text());status.pop('panel_visible')
        (self.data/'native-status.json').write_text(json.dumps(status))
        result=self.report()
        self.assertIsNone(result['native']['panel_visible']);self.assertIsNone(result['native']['panel_visible_evidence'])
        status['widget_visible']=False;(self.data/'native-status.json').write_text(json.dumps(status))
        result=self.report()
        self.assertFalse(result['native']['panel_visible'])
        self.assertEqual(result['native']['panel_visible_evidence'],'panel_is_visible_flag_only_not_unobscured_desktop_proof')
    def test_stale_heartbeat_and_pid_mismatch_are_not_consistent(self):
        result=self.report(menu_status={'registered':True,'running':True,'pid':999},now=self.now+60)
        self.assertFalse(result['native']['status_pid_matches_launchd']);self.assertFalse(result['native']['heartbeat_fresh'])
        self.assertFalse(result['consistent'])
    def test_listen_owner_rejects_other_pid(self):
        def fake(argv,**kwargs):
            class Done:returncode=0;stdout=b'p999\nn127.0.0.1:8793\n'
            return Done()
        result=diagnostics.listen_status(111,runner=fake)
        self.assertFalse(result['listening']);self.assertFalse(result['pid_matches_service'])
    def test_receipt_outside_releases_is_not_trusted(self):
        outside=Path(self.tmp.name)/'outside';outside.mkdir()
        (outside/'headroom-release.json').write_text((self.payload/'headroom-release.json').read_text())
        receipt=json.loads((self.data/'installation.json').read_text());receipt['service']['payload']=str(outside)
        (self.data/'installation.json').write_text(json.dumps(receipt))
        # The managed releases/<fingerprint> fallback is used, not the outside path.
        result=self.report();self.assertTrue(result['payload']['manifest_valid'])

    def test_corrupt_archive_is_reported_and_preserved(self):
        archives=self.data/'archives';archives.mkdir()
        damaged=archives/'2020-01.sqlite3';damaged.write_bytes(b'private damaged history sentinel')
        with sqlite3.connect(self.data/'plans.sqlite3') as db:
            db.execute('CREATE TABLE observations(provider_id,account_ref,window_id,observed_at,remaining_pct,source)')
        before=damaged.read_bytes();result=diagnostics.storage_status(self.data)
        self.assertTrue(result['active_integrity']);self.assertEqual(result['invalid_archives'],1)
        self.assertEqual(damaged.read_bytes(),before);self.assertNotIn('sentinel',json.dumps(result))


if __name__=='__main__':unittest.main()
